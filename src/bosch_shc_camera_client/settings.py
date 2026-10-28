"""Read and change the per-camera settings of the Bosch cloud API.

Every function takes an open aiohttp session and a valid access token. Reads
go through `fetch_json`; writes classify the HTTP status the same way:
401/403 raise `BoschCameraAuthError`, network trouble raises
`BoschCameraConnectionError`, an unexpected payload raises
`BoschCameraInvalidResponseError`. Endpoints that need the complete object on
every PUT are written as GET, change the given fields, PUT.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

import aiohttp

from .cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
    fetch_json,
)
from .cloud import CLOUD_API

_E = TypeVar("_E", bound=StrEnum)

WRITE_TIMEOUT = 10
_OK_STATUSES = (200, 201, 204)
_PRIVACY_STATUS = 443
MOTION_LIGHT_SENSITIVITY_RANGE = (1, 5)
LEVEL_RANGE = (0, 100)
READ_TIMEOUT = 8
ERROR_TEXT_LIMIT = 200
# Only these endpoint names may be passed to the raw helpers, so no caller
# can build an arbitrary path.
ENDPOINT_PATTERN = re.compile(r"[A-Za-z_]+(?:/[A-Za-z_]+)?")
CAMERA_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class BoschCameraPrivacyModeError(BoschCameraConnectionError):
    """The camera refused the request because privacy mode is on (HTTP 443)."""


class ScheduleStatus(StrEnum):
    """Lighting mode of a Gen1 Eyes Outdoor (`scheduleStatus`)."""

    FOLLOW_SCHEDULE = "FOLLOW_SCHEDULE"
    AMBIENT_LIGHT = "AMBIENT_LIGHT"
    AMBIENT_LIGHT_AND_SCHEDULE = "AMBIENT_LIGHT_AND_SCHEDULE"
    ALWAYS_OFF = "ALWAYS_OFF"


class DetectionMode(StrEnum):
    """Detection mode of the intrusion detection config."""

    ALL_MOTIONS = "ALL_MOTIONS"
    ONLY_HUMANS = "ONLY_HUMANS"
    ZONES = "ZONES"


@dataclass(frozen=True, slots=True)
class SettingWriteResult:
    """Outcome of a successful write: HTTP status, response object, sent body."""

    status: int
    response: dict[str, Any] | None
    sent: dict[str, Any] | list[Any]


@dataclass(frozen=True, slots=True)
class RawResponse:
    """HTTP status (0 on connection error or timeout) and parsed JSON body or None."""

    status: int
    body: Any


@dataclass(frozen=True, slots=True)
class LightingOverride:
    """Manual light state of a Gen1 camera."""

    front_light_on: bool
    wallwasher_on: bool
    front_light_intensity: float | None


@dataclass(frozen=True, slots=True)
class LightingOptions:
    """Lighting options of a Gen1 Eyes Outdoor; `raw` is the full object."""

    schedule_status: ScheduleStatus | None
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class MotionLighting:
    """Motion light settings; `raw` is the full object."""

    sensitivity: int | None
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class AmbientLighting:
    """Ambient light settings; `raw` is the full object."""

    enabled: bool | None
    schedule_type: str | None
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class AudioSettings:
    """Microphone and speaker settings; `raw` is the full object."""

    audio_enabled: bool | None
    microphone_level: int | None
    speaker_level: int | None
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class IntrusionDetectionConfig:
    """Intrusion detection settings; `raw` is the full object."""

    detection_mode: DetectionMode | None
    sensitivity: int | None
    distance: int | None
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class AlarmStatus:
    """Alarm state of an indoor camera, upper-cased; None when absent."""

    alarm_type: str | None
    intrusion_system: str | None


@dataclass(frozen=True, slots=True)
class Region:
    """Rectangle in normalized image coordinates (0.0 to 1.0)."""

    x: float
    y: float
    w: float
    h: float


def _number(value: Any) -> float | None:
    """Finite int/float as float; bool, NaN, inf and anything else are missing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else round(number)


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _enum(kind: type[_E], value: Any) -> _E | None:
    text = _text(value)
    try:
        return None if text is None else kind(text)
    except ValueError:
        return None


def _url(api_url: str, cam_id: str, endpoint: str) -> str:
    return f"{api_url}/v11/video_inputs/{cam_id}/{endpoint}"


async def _get_object(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    endpoint: str,
    api_url: str,
) -> dict[str, Any]:
    payload = await fetch_json(
        session, token, _url(api_url, cam_id, endpoint), endpoint
    )
    if not isinstance(payload, dict):
        raise BoschCameraInvalidResponseError(f"{endpoint} is not an object")
    return payload


async def _get_regions(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    endpoint: str,
    api_url: str,
) -> list[Region]:
    payload = await fetch_json(
        session, token, _url(api_url, cam_id, endpoint), endpoint
    )
    if not isinstance(payload, list):
        raise BoschCameraInvalidResponseError(f"{endpoint} is not an array")
    regions: list[Region] = []
    for item in payload:
        values = (
            [_number(item.get(key)) for key in "xywh"] if isinstance(item, dict) else []
        )
        if len(values) != 4 or None in values:
            raise BoschCameraInvalidResponseError(f"{endpoint} entry is malformed")
        regions.append(Region(*(v for v in values if v is not None)))
    return regions


async def _send(
    session: aiohttp.ClientSession,
    token: str,
    method: str,
    url: str,
    body: dict[str, Any] | list[Any],
    what: str,
    *,
    error_detail: bool = False,
) -> SettingWriteResult:
    """Send `body` as JSON and classify the answer; optionally quote the error text."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with asyncio.timeout(WRITE_TIMEOUT):
            async with session.request(method, url, json=body, headers=headers) as resp:
                if resp.status in (401, 403):
                    raise BoschCameraAuthError(f"Token rejected (HTTP {resp.status})")
                if resp.status == _PRIVACY_STATUS:
                    raise BoschCameraPrivacyModeError(f"{what} blocked by privacy mode")
                if resp.status not in _OK_STATUSES:
                    message = f"{what} returned HTTP {resp.status}"
                    if error_detail:
                        text = await resp.text(errors="replace")
                        message += f": {text[:ERROR_TEXT_LIMIT]}"
                    raise BoschCameraConnectionError(message)
                try:
                    parsed = await resp.json(content_type=None)
                except ValueError:
                    parsed = None
                return SettingWriteResult(
                    status=resp.status,
                    response=parsed if isinstance(parsed, dict) else None,
                    sent=body,
                )
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err


async def _merge_put(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    endpoint: str,
    changes: Mapping[str, Any],
    api_url: str,
) -> SettingWriteResult:
    """GET the complete object, apply `changes`, PUT it back."""
    changes = dict(changes)
    if any(isinstance(v, float) and not math.isfinite(v) for v in changes.values()):
        raise ValueError("changes must not contain NaN or infinity")
    current = await _get_object(session, token, cam_id, endpoint, api_url)
    if not current:
        raise BoschCameraInvalidResponseError(f"{endpoint} is empty")
    return await _send(
        session,
        token,
        "PUT",
        _url(api_url, cam_id, endpoint),
        {**current, **changes},
        endpoint,
    )


def _check_endpoint(endpoint: str, cam_id: str) -> None:
    if not CAMERA_ID_PATTERN.fullmatch(cam_id):
        raise ValueError("invalid camera id")
    if not ENDPOINT_PATTERN.fullmatch(endpoint):
        raise ValueError(f"unknown settings endpoint: {endpoint}")


async def get_setting_raw(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    endpoint: str,
    *,
    timeout: float = READ_TIMEOUT,
    api_url: str = CLOUD_API,
) -> RawResponse:
    """GET a known endpoint; HTTP errors and malformed bodies are returned, not raised."""
    _check_endpoint(endpoint, cam_id)
    try:
        async with asyncio.timeout(timeout):
            async with session.get(
                _url(api_url, cam_id, endpoint),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    return RawResponse(resp.status, None)
                try:
                    return RawResponse(200, await resp.json(content_type=None))
                except ValueError:
                    return RawResponse(200, None)
    except (TimeoutError, aiohttp.ClientError):
        return RawResponse(0, None)


async def put_setting(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    endpoint: str,
    body: dict[str, Any] | list[Any],
    *,
    method: str = "PUT",
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Send `body` as is to a known endpoint; errors quote the first 200 characters."""
    _check_endpoint(endpoint, cam_id)
    if method not in ("PUT", "POST"):
        raise ValueError("method must be PUT or POST")
    return await _send(
        session,
        token,
        method,
        _url(api_url, cam_id, endpoint),
        body,
        endpoint,
        error_detail=True,
    )


def _check_range(name: str, value: int, bounds: tuple[int, int]) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not bounds[0] <= value <= bounds[1]
    ):
        raise ValueError(f"{name} must be between {bounds[0]} and {bounds[1]}")


def _check_regions(regions: list[Region]) -> list[dict[str, float]]:
    body = []
    for region in regions:
        values = {"x": region.x, "y": region.y, "w": region.w, "h": region.h}
        if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values.values()):
            raise ValueError("region values must be between 0.0 and 1.0")
        body.append(values)
    return body


# --- Gen1 lighting -------------------------------------------------------


async def get_lighting_override(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> LightingOverride:
    """Return the manual light state; both switches must be booleans."""
    data = await _get_object(session, token, cam_id, "lighting_override", api_url)
    front = _flag(data.get("frontLightOn"))
    wall = _flag(data.get("wallwasherOn"))
    if front is None or wall is None:
        raise BoschCameraInvalidResponseError("lighting_override has no light state")
    return LightingOverride(front, wall, _number(data.get("frontLightIntensity")))


async def set_lighting_override(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    front_light_on: bool,
    wallwasher_on: bool,
    front_light_intensity: float | None = None,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Set the manual light state; the intensity is only sent with the front light on."""
    body: dict[str, Any] = {
        "frontLightOn": front_light_on,
        "wallwasherOn": wallwasher_on,
    }
    if front_light_on and front_light_intensity is not None:
        if not math.isfinite(front_light_intensity) or not (
            0.0 <= front_light_intensity <= 1.0
        ):
            raise ValueError("front_light_intensity must be between 0.0 and 1.0")
        body["frontLightIntensity"] = front_light_intensity
    return await _send(
        session,
        token,
        "PUT",
        _url(api_url, cam_id, "lighting_override"),
        body,
        "lighting_override",
    )


async def get_lighting_options(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> LightingOptions:
    """Return the lighting options incl. the lighting mode."""
    data = await _get_object(session, token, cam_id, "lighting_options", api_url)
    return LightingOptions(_enum(ScheduleStatus, data.get("scheduleStatus")), data)


async def set_schedule_status(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    status: ScheduleStatus,
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Change only the lighting mode; the rest of the object is kept."""
    return await _merge_put(
        session,
        token,
        cam_id,
        "lighting_options",
        {"scheduleStatus": ScheduleStatus(status).value},
        api_url,
    )


# --- Gen2 lighting -------------------------------------------------------


async def get_motion_lighting(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> MotionLighting:
    """Return the motion light settings."""
    data = await _get_object(session, token, cam_id, "lighting/motion", api_url)
    return MotionLighting(_integer(data.get("motionLightSensitivity")), data)


async def set_motion_light_sensitivity(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    sensitivity: int,
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Set the motion light sensitivity (1 low to 5 high)."""
    _check_range("sensitivity", sensitivity, MOTION_LIGHT_SENSITIVITY_RANGE)
    return await _merge_put(
        session,
        token,
        cam_id,
        "lighting/motion",
        {"motionLightSensitivity": sensitivity},
        api_url,
    )


async def get_ambient_lighting(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> AmbientLighting:
    """Return the ambient light settings; the schedule is a string or an object."""
    data = await _get_object(session, token, cam_id, "lighting/ambient", api_url)
    schedule = data.get("ambientLightSchedule")
    if isinstance(schedule, dict):
        schedule = schedule.get("type")
    return AmbientLighting(
        _flag(data.get("ambientLightEnabled")), _text(schedule), data
    )


# --- Audio ---------------------------------------------------------------


async def get_audio_settings(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> AudioSettings:
    """Return the microphone and speaker settings."""
    data = await _get_object(session, token, cam_id, "audio", api_url)
    return AudioSettings(
        _flag(data.get("audioEnabled")),
        _integer(data.get("microphoneLevel")),
        _integer(data.get("speakerLevel")),
        data,
    )


async def set_audio_settings(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    audio_enabled: bool | None = None,
    microphone_level: int | None = None,
    speaker_level: int | None = None,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Change the given audio fields (levels 0 to 100); the rest is kept."""
    changes: dict[str, Any] = {}
    if audio_enabled is not None:
        changes["audioEnabled"] = audio_enabled
    for key, level in (
        ("microphoneLevel", microphone_level),
        ("speakerLevel", speaker_level),
    ):
        if level is not None:
            _check_range(key, level, LEVEL_RANGE)
            changes[key] = level
    return await _merge_put(session, token, cam_id, "audio", changes, api_url)


# --- Sensors -------------------------------------------------------------


async def get_ambient_light_sensor_level(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> float | None:
    """Return the ambient light sensor level, None when missing or not finite."""
    data = await _get_object(
        session, token, cam_id, "ambient_light_sensor_level", api_url
    )
    return _number(data.get("ambientLightSensorLevel"))


async def get_lens_elevation(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> float | None:
    """Return the lens elevation, None when missing or not finite."""
    data = await _get_object(session, token, cam_id, "lens_elevation", api_url)
    return _number(data.get("elevation"))


async def set_lens_elevation(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    elevation: float,
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Set the lens elevation."""
    if not math.isfinite(elevation):
        raise ValueError("elevation must be finite")
    return await _send(
        session,
        token,
        "PUT",
        _url(api_url, cam_id, "lens_elevation"),
        {"elevation": round(elevation, 2)},
        "lens_elevation",
    )


# --- Intrusion detection and alarm ---------------------------------------


async def get_intrusion_detection_config(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> IntrusionDetectionConfig:
    """Return the intrusion detection settings."""
    data = await _get_object(
        session, token, cam_id, "intrusionDetectionConfig", api_url
    )
    return IntrusionDetectionConfig(
        _enum(DetectionMode, data.get("detectionMode")),
        _integer(data.get("sensitivity")),
        _integer(data.get("distance")),
        data,
    )


async def set_intrusion_detection_config(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    changes: Mapping[str, Any],
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Change the given keys of the intrusion detection object; the rest is kept."""
    if not changes:
        raise ValueError("changes must not be empty")
    return await _merge_put(
        session, token, cam_id, "intrusionDetectionConfig", changes, api_url
    )


async def get_alarm_status(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> AlarmStatus:
    """Return the alarm state of an indoor camera."""
    data = await _get_object(session, token, cam_id, "alarmStatus", api_url)
    return AlarmStatus(_text(data.get("alarmType")), _text(data.get("intrusionSystem")))


# --- Motion zones and privacy masks --------------------------------------


async def get_motion_zones(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> list[Region]:
    """Return the motion sensitive areas of a Gen1 camera."""
    return await _get_regions(session, token, cam_id, "motion_sensitive_areas", api_url)


async def set_motion_zones(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    zones: list[Region],
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Replace the motion sensitive areas."""
    return await _send(
        session,
        token,
        "POST",
        _url(api_url, cam_id, "motion_sensitive_areas"),
        _check_regions(zones),
        "motion_sensitive_areas",
    )


async def get_privacy_masks(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    *,
    api_url: str = CLOUD_API,
) -> list[Region]:
    """Return the privacy masks of a Gen1 camera."""
    return await _get_regions(session, token, cam_id, "privacy_masks", api_url)


async def set_privacy_masks(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    masks: list[Region],
    *,
    api_url: str = CLOUD_API,
) -> SettingWriteResult:
    """Replace the privacy masks."""
    return await _send(
        session,
        token,
        "POST",
        _url(api_url, cam_id, "privacy_masks"),
        _check_regions(masks),
        "privacy_masks",
    )
