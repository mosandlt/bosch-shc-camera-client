"""Read the current state of a camera from the cloud API.

The caller owns the aiohttp session and the OAuth2 access token; this module
only builds the requests and validates the responses. It performs no token
refresh and no retries. Errors map as in `cameras`: `BoschCameraAuthError` for
a rejected token, `BoschCameraConnectionError` for transport problems or error
statuses, `BoschCameraInvalidResponseError` for payloads of an unexpected
shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp

from .cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)
from .cloud import CLOUD_API

STATE_TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class CameraAccountStates:
    """Switch states that the camera list reports for one camera.

    `privacy_mode` is None when the list entry carries no usable value.
    `notifications_status` is the raw status string (for example
    ``FOLLOW_CAMERA_SCHEDULE`` or ``ALWAYS_OFF``) or None.
    """

    privacy_mode: bool | None
    notifications_status: str | None

    @property
    def notifications_enabled(self) -> bool | None:
        """True unless notifications are ``ALWAYS_OFF``; None when unknown."""
        if self.notifications_status is None:
            return None
        return self.notifications_status != "ALWAYS_OFF"


@dataclass(frozen=True, slots=True)
class WifiInfo:
    """Wi-Fi link quality of a camera."""

    signal_strength: int | None


@dataclass(frozen=True, slots=True)
class MotionSettings:
    """Motion detection settings of a camera."""

    enabled: bool
    sensitivity: str | None


@dataclass(frozen=True, slots=True)
class LightState:
    """Manual light override of a camera (front light and wall washer)."""

    front_light_on: bool
    wallwasher_on: bool
    front_light_intensity: float | None

    @property
    def any_on(self) -> bool:
        """True when either light is on."""
        return self.front_light_on or self.wallwasher_on


@dataclass(frozen=True, slots=True)
class FirmwareStatus:
    """Firmware update status of a camera."""

    current: str | None
    update: str | None
    up_to_date: bool | None
    updating: bool


async def _get_json(
    session: aiohttp.ClientSession, token: str, url: str, what: str
) -> Any:
    """GET `url` with the bearer token and return the decoded JSON body."""
    try:
        async with asyncio.timeout(STATE_TIMEOUT):
            async with session.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status in (401, 403):
                    raise BoschCameraAuthError(f"Token rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise BoschCameraConnectionError(
                        f"{what} returned HTTP {resp.status}"
                    )
                return await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err
    except ValueError as err:
        raise BoschCameraInvalidResponseError("Response is not JSON") from err


async def _get_object(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    endpoint: str,
    api_url: str,
) -> dict[str, Any]:
    """GET a per-camera endpoint whose body must be a JSON object."""
    payload = await _get_json(
        session,
        token,
        f"{api_url}/v11/video_inputs/{camera_id}/{endpoint}",
        endpoint,
    )
    if not isinstance(payload, dict):
        raise BoschCameraInvalidResponseError(f"{endpoint} is not an object")
    return payload


async def get_camera_account_states(
    session: aiohttp.ClientSession, token: str, *, api_url: str = CLOUD_API
) -> dict[str, CameraAccountStates]:
    """Return privacy and notification state per camera id from the camera list."""
    payload = await _get_json(
        session, token, f"{api_url}/v11/video_inputs", "Camera list"
    )
    if not isinstance(payload, list):
        raise BoschCameraInvalidResponseError("Camera list is not an array")
    states: dict[str, CameraAccountStates] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        if not isinstance(camera_id, str) or not camera_id:
            continue
        privacy = item.get("privacyMode")
        notifications = item.get("notificationsEnabledStatus")
        states[camera_id] = CameraAccountStates(
            privacy_mode=(
                privacy.upper() == "ON"
                if isinstance(privacy, str) and privacy.upper() in ("ON", "OFF")
                else None
            ),
            notifications_status=(
                notifications
                if isinstance(notifications, str) and notifications
                else None
            ),
        )
    return states


async def get_wifi_info(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> WifiInfo:
    """Return the Wi-Fi signal strength (percent) of a camera."""
    body = await _get_object(session, token, camera_id, "wifiinfo", api_url)
    signal = body.get("signalStrength")
    if isinstance(signal, bool) or not isinstance(signal, (int, float)):
        return WifiInfo(signal_strength=None)
    return WifiInfo(signal_strength=int(signal))


async def get_motion_settings(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> MotionSettings:
    """Return whether motion detection is on and its sensitivity level."""
    body = await _get_object(session, token, camera_id, "motion", api_url)
    sensitivity = body.get("motionAlarmConfiguration")
    return MotionSettings(
        enabled=body.get("enabled") is True,
        sensitivity=sensitivity
        if isinstance(sensitivity, str) and sensitivity
        else None,
    )


async def get_notification_types(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> dict[str, bool]:
    """Return the per-type notification toggles (type name -> enabled)."""
    body = await _get_object(session, token, camera_id, "notifications", api_url)
    return {key: value for key, value in body.items() if isinstance(value, bool)}


async def get_light_state(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> LightState:
    """Return the manual light override state of a camera.

    Raises BoschCameraInvalidResponseError when the body lacks boolean
    ``frontLightOn`` and ``wallwasherOn`` values.
    """
    body = await _get_object(session, token, camera_id, "lighting_override", api_url)
    front = body.get("frontLightOn")
    wall = body.get("wallwasherOn")
    if not isinstance(front, bool) or not isinstance(wall, bool):
        raise BoschCameraInvalidResponseError("lighting_override has no light state")
    intensity = body.get("frontLightIntensity")
    return LightState(
        front_light_on=front,
        wallwasher_on=wall,
        front_light_intensity=(
            float(intensity)
            if isinstance(intensity, (int, float)) and not isinstance(intensity, bool)
            else None
        ),
    )


async def get_firmware_status(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> FirmwareStatus:
    """Return the installed firmware version and update status of a camera."""
    body = await _get_object(session, token, camera_id, "firmware", api_url)
    current = body.get("current")
    update = body.get("update")
    up_to_date = body.get("upToDate")
    return FirmwareStatus(
        current=current if isinstance(current, str) and current else None,
        update=update if isinstance(update, str) and update else None,
        up_to_date=up_to_date if isinstance(up_to_date, bool) else None,
        updating=body.get("updating") is True,
    )
