"""List the cameras of a Bosch Smart Home Camera account from the cloud API.

The caller owns the aiohttp session and the OAuth2 access token; this module
only builds the request and validates the response. It performs no token
refresh and no retries, so an expired token surfaces as
`BoschCameraAuthError` for the caller to handle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .cloud import CLOUD_API
from .media_transfer import is_safe_bosch_url

LIST_TIMEOUT = 15
EVENTS_TIMEOUT = 15
IMAGE_TIMEOUT = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
MAX_ACCOUNT_ID_LENGTH = 128


class BoschCameraError(Exception):
    """Base class for errors raised by the camera client."""


class BoschCameraAuthError(BoschCameraError):
    """The access token was rejected."""


class BoschCameraConnectionError(BoschCameraError):
    """The cloud API could not be reached or answered with an error status."""


class BoschCameraInvalidResponseError(BoschCameraError):
    """The cloud API answered with a payload of an unexpected shape."""


def account_id_from_token(access_token: str) -> str:
    """Return the account identifier (JWT ``sub`` claim) of an access token.

    The token comes straight from the token endpoint over TLS, so the claim is
    read without verifying the signature again.

    Raises BoschCameraAuthError if the token is not a JWT or has no usable
    ``sub`` claim.
    """
    try:
        _header, payload, _signature = access_token.split(".")
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        )
        account_id = claims["sub"]
    except (ValueError, KeyError, TypeError, binascii.Error) as err:
        raise BoschCameraAuthError("access token has no readable subject") from err
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or len(account_id) > MAX_ACCOUNT_ID_LENGTH
    ):
        raise BoschCameraAuthError("access token has no usable subject")
    return account_id


class CameraModel(StrEnum):
    """Camera models of the Bosch Smart Home Camera lineup."""

    EYES_OUTDOOR = "eyes_outdoor"
    INDOOR_360 = "indoor_360"
    EYES_OUTDOOR_II = "eyes_outdoor_ii"
    EYES_INDOOR_II = "eyes_indoor_ii"
    UNKNOWN = "unknown"


_MODEL_BY_HARDWARE: dict[str, CameraModel] = {
    "CAMERA_EYES": CameraModel.EYES_OUTDOOR,
    "OUTDOOR": CameraModel.EYES_OUTDOOR,
    "CAMERA_360": CameraModel.INDOOR_360,
    "INDOOR": CameraModel.INDOOR_360,
    "HOME_Eyes_Outdoor": CameraModel.EYES_OUTDOOR_II,
    "HOME_Eyes_Indoor": CameraModel.EYES_INDOOR_II,
}


@dataclass(frozen=True, slots=True)
class Camera:
    """A camera of the account."""

    id: str
    title: str
    hardware_version: str
    model: CameraModel
    firmware_version: str | None


def _parse_camera(item: Any) -> Camera:
    """Build a `Camera` from one entry of the list response."""
    if not isinstance(item, dict):
        raise BoschCameraInvalidResponseError("Camera entry is not an object")
    camera_id = item.get("id")
    if not isinstance(camera_id, str) or not camera_id:
        raise BoschCameraInvalidResponseError("Camera entry has no id")
    title = item.get("title")
    hardware = item.get("hardwareVersion")
    firmware = item.get("firmwareVersion")
    hardware_version = hardware if isinstance(hardware, str) else ""
    return Camera(
        id=camera_id,
        title=title if isinstance(title, str) and title else camera_id,
        hardware_version=hardware_version,
        model=_MODEL_BY_HARDWARE.get(hardware_version, CameraModel.UNKNOWN),
        firmware_version=firmware if isinstance(firmware, str) and firmware else None,
    )


async def list_cameras(
    session: aiohttp.ClientSession, token: str, *, api_url: str = CLOUD_API
) -> list[Camera]:
    """Return the cameras of the account that owns `token`."""
    try:
        async with asyncio.timeout(LIST_TIMEOUT):
            async with session.get(
                f"{api_url}/v11/video_inputs",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status in (401, 403):
                    raise BoschCameraAuthError(f"Token rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise BoschCameraConnectionError(
                        f"Camera list returned HTTP {resp.status}"
                    )
                payload = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err
    except ValueError as err:
        raise BoschCameraInvalidResponseError("Response is not JSON") from err
    if not isinstance(payload, list):
        raise BoschCameraInvalidResponseError("Camera list is not an array")
    return [_parse_camera(item) for item in payload]


async def get_latest_event_image(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> bytes | None:
    """Return the image of the newest event of a camera that has one.

    Events come newest first. An event without an image, with an image URL
    outside the Bosch domains, or whose image cannot be downloaded is skipped.
    Returns None when no event has a usable image.
    """
    try:
        async with asyncio.timeout(EVENTS_TIMEOUT):
            async with session.get(
                f"{api_url}/v11/events",
                params={"videoInputId": camera_id},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status in (401, 403):
                    raise BoschCameraAuthError(f"Token rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise BoschCameraConnectionError(
                        f"Event list returned HTTP {resp.status}"
                    )
                payload = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err
    except ValueError as err:
        raise BoschCameraInvalidResponseError("Response is not JSON") from err
    if not isinstance(payload, list):
        raise BoschCameraInvalidResponseError("Event list is not an array")
    for event in payload:
        image_url = event.get("imageUrl") if isinstance(event, dict) else None
        if not isinstance(image_url, str) or not _is_downloadable(image_url):
            continue
        image = await _download_image(session, token, image_url)
        if image is not None:
            return image
    return None


def _is_downloadable(url: str) -> bool:
    """Bosch HTTPS URL without user info, which aiohttp rejects next to a token."""
    return is_safe_bosch_url(url) and "@" not in urlparse(url).netloc


async def _download_image(
    session: aiohttp.ClientSession, token: str, url: str
) -> bytes | None:
    """Download one event image, or None when it is unavailable or too large."""
    try:
        async with asyncio.timeout(IMAGE_TIMEOUT):
            # No redirects: only `url` itself was checked against the allowlist.
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return None
                image = bytearray()
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    image += chunk
                    if len(image) > MAX_IMAGE_BYTES:
                        return None
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None
    return bytes(image) or None
