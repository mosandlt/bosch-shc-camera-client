"""List the cameras of a Bosch Smart Home Camera account from the cloud API.

The caller owns the aiohttp session and the OAuth2 access token; this module
only builds the request and validates the response. It performs no token
refresh and no retries, so an expired token surfaces as
`BoschCameraAuthError` for the caller to handle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp

from .cloud import CLOUD_API

LIST_TIMEOUT = 15


class BoschCameraError(Exception):
    """Base class for errors raised by the camera client."""


class BoschCameraAuthError(BoschCameraError):
    """The access token was rejected."""


class BoschCameraConnectionError(BoschCameraError):
    """The cloud API could not be reached or answered with an error status."""


class BoschCameraInvalidResponseError(BoschCameraError):
    """The cloud API answered with a payload of an unexpected shape."""


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
