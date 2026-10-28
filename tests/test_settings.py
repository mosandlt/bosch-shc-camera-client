"""Tests for settings.py -- per-camera cloud settings (fake data only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client import settings as s
from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)

pytestmark = pytest.mark.asyncio

CAM = "11111111-1111-1111-1111-111111111111"
BASE = f"https://residential.cbs.boschsecurity.com/v11/video_inputs/{CAM}"


def _cm(status: int, payload: Any, raises: Exception | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = (
        AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=payload)
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _session(
    get: Any = None, status: int = 200, put: Any = None, raises: Exception | None = None
) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=_cm(200, get))
    session.request = MagicMock(return_value=_cm(status, put, raises))
    return session


def _sent(session: MagicMock) -> tuple[str, str, Any]:
    call = session.request.call_args
    return call.args[0], call.args[1], call.kwargs["json"]


# --- helpers ---------------------------------------------------------------


async def test_number_helpers() -> None:
    assert s._number(True) is None
    assert s._number("1") is None
    assert s._number(float("nan")) is None
    assert s._number(float("inf")) is None
    assert s._number(3) == 3.0
    assert s._integer(2.6) == 3
    assert s._integer(None) is None
    assert s._flag(1) is None
    assert s._text("  ") is None
    assert s._text(" on ") == "ON"
    assert s._enum(s.ScheduleStatus, "bogus") is None
    assert s._enum(s.ScheduleStatus, None) is None
    assert s._enum(s.ScheduleStatus, "always_off") is s.ScheduleStatus.ALWAYS_OFF


async def test_get_rejects_non_object_and_auth() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await s.get_audio_settings(_session(get=[1]), "t", CAM)
    session = _session()
    session.get = MagicMock(return_value=_cm(401, None))
    with pytest.raises(BoschCameraAuthError):
        await s.get_audio_settings(session, "t", CAM)


# --- reads -----------------------------------------------------------------


async def test_get_lighting_override() -> None:
    body = {"frontLightOn": True, "wallwasherOn": False, "frontLightIntensity": 0.5}
    result = await s.get_lighting_override(_session(get=body), "t", CAM)
    assert result == s.LightingOverride(True, False, 0.5)
    body = {"frontLightOn": False, "wallwasherOn": False}
    assert (
        await s.get_lighting_override(_session(get=body), "t", CAM)
    ).front_light_intensity is None


async def test_get_lighting_override_requires_booleans() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await s.get_lighting_override(_session(get={"frontLightOn": 1}), "t", CAM)


async def test_get_lighting_options_and_url() -> None:
    session = _session(get={"scheduleStatus": "always_off", "x": 1})
    result = await s.get_lighting_options(session, "t", CAM)
    assert result.schedule_status is s.ScheduleStatus.ALWAYS_OFF
    assert result.raw == {"scheduleStatus": "always_off", "x": 1}
    assert session.get.call_args.args[0] == f"{BASE}/lighting_options"
    unknown = await s.get_lighting_options(
        _session(get={"scheduleStatus": "?"}), "t", CAM
    )
    assert unknown.schedule_status is None


async def test_get_motion_and_ambient_lighting() -> None:
    motion = await s.get_motion_lighting(
        _session(get={"motionLightSensitivity": 3}), "t", CAM
    )
    assert motion.sensitivity == 3
    for schedule, expected in (
        ("environment", "ENVIRONMENT"),
        ({"type": "manual"}, "MANUAL"),
        (None, None),
    ):
        body = {"ambientLightEnabled": True, "ambientLightSchedule": schedule}
        ambient = await s.get_ambient_lighting(_session(get=body), "t", CAM)
        assert (ambient.enabled, ambient.schedule_type) == (True, expected)


async def test_get_audio_and_sensors() -> None:
    body = {"audioEnabled": True, "microphoneLevel": 60, "speakerLevel": float("nan")}
    audio = await s.get_audio_settings(_session(get=body), "t", CAM)
    assert (audio.audio_enabled, audio.microphone_level, audio.speaker_level) == (
        True,
        60,
        None,
    )
    level = await s.get_ambient_light_sensor_level(
        _session(get={"ambientLightSensorLevel": 0.25}), "t", CAM
    )
    assert level == 0.25
    assert await s.get_ambient_light_sensor_level(_session(get={}), "t", CAM) is None
    assert await s.get_lens_elevation(_session(get={"elevation": 2.0}), "t", CAM) == 2.0
    nan = {"elevation": float("inf")}
    assert await s.get_lens_elevation(_session(get=nan), "t", CAM) is None


async def test_get_intrusion_config_and_alarm_status() -> None:
    body = {"detectionMode": "ONLY_HUMANS", "sensitivity": 3, "distance": 5}
    cfg = await s.get_intrusion_detection_config(_session(get=body), "t", CAM)
    assert (cfg.detection_mode, cfg.sensitivity, cfg.distance) == (
        s.DetectionMode.ONLY_HUMANS,
        3,
        5,
    )
    alarm = await s.get_alarm_status(
        _session(get={"alarmType": "none", "intrusionSystem": "Active"}), "t", CAM
    )
    assert alarm == s.AlarmStatus("NONE", "ACTIVE")
    assert await s.get_alarm_status(_session(get={}), "t", CAM) == s.AlarmStatus(
        None, None
    )


async def test_regions() -> None:
    body = [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]
    assert await s.get_motion_zones(_session(get=body), "t", CAM) == [
        s.Region(0.1, 0.2, 0.3, 0.4)
    ]
    assert await s.get_privacy_masks(_session(get=[]), "t", CAM) == []
    for bad in ({"x": 1}, "x", {"x": 0, "y": 0, "w": 0, "h": float("nan")}):
        with pytest.raises(BoschCameraInvalidResponseError):
            await s.get_privacy_masks(_session(get=[bad]), "t", CAM)
    with pytest.raises(BoschCameraInvalidResponseError):
        await s.get_motion_zones(_session(get={}), "t", CAM)


# --- writes ----------------------------------------------------------------


async def test_set_lighting_override_bodies() -> None:
    session = _session(put={"ok": 1})
    result = await s.set_lighting_override(
        session,
        "t",
        CAM,
        front_light_on=True,
        wallwasher_on=True,
        front_light_intensity=0.7,
    )
    assert _sent(session) == (
        "PUT",
        f"{BASE}/lighting_override",
        {"frontLightOn": True, "wallwasherOn": True, "frontLightIntensity": 0.7},
    )
    assert result.status == 200 and result.response == {"ok": 1}
    session = _session(status=204, raises=ValueError("empty"))
    result = await s.set_lighting_override(
        session,
        "t",
        CAM,
        front_light_on=False,
        wallwasher_on=False,
        front_light_intensity=0.7,
    )
    assert _sent(session)[2] == {"frontLightOn": False, "wallwasherOn": False}
    assert result.response is None
    session = _session(put=[1])
    assert (
        await s.set_lighting_override(
            session, "t", CAM, front_light_on=True, wallwasher_on=False
        )
    ).response is None
    assert "frontLightIntensity" not in _sent(session)[2]


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan")])
async def test_set_lighting_override_rejects_intensity(bad: float) -> None:
    with pytest.raises(ValueError):
        await s.set_lighting_override(
            _session(),
            "t",
            CAM,
            front_light_on=True,
            wallwasher_on=True,
            front_light_intensity=bad,
        )


async def test_set_schedule_status_merges() -> None:
    session = _session(get={"scheduleStatus": "FOLLOW_SCHEDULE", "threshold": 4})
    await s.set_schedule_status(session, "t", CAM, s.ScheduleStatus.ALWAYS_OFF)
    assert _sent(session) == (
        "PUT",
        f"{BASE}/lighting_options",
        {"scheduleStatus": "ALWAYS_OFF", "threshold": 4},
    )


async def test_merge_put_rejects_empty_object() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await s.set_motion_light_sensitivity(_session(get={}), "t", CAM, 3)


async def test_set_motion_light_sensitivity() -> None:
    session = _session(get={"motionLightSensitivity": 1, "k": "v"})
    await s.set_motion_light_sensitivity(session, "t", CAM, 5)
    assert _sent(session)[2] == {"motionLightSensitivity": 5, "k": "v"}
    for bad in (0, 6, True):
        with pytest.raises(ValueError):
            await s.set_motion_light_sensitivity(session, "t", CAM, bad)


async def test_set_audio_settings() -> None:
    session = _session(
        get={"audioEnabled": True, "microphoneLevel": 60, "speakerLevel": 80}
    )
    await s.set_audio_settings(
        session, "t", CAM, microphone_level=40, audio_enabled=False
    )
    assert _sent(session)[2] == {
        "audioEnabled": False,
        "microphoneLevel": 40,
        "speakerLevel": 80,
    }
    await s.set_audio_settings(session, "t", CAM, speaker_level=0)
    assert _sent(session)[2]["speakerLevel"] == 0
    with pytest.raises(ValueError):
        await s.set_audio_settings(session, "t", CAM, speaker_level=101)


async def test_set_lens_elevation() -> None:
    session = _session()
    await s.set_lens_elevation(session, "t", CAM, 2.456)
    assert _sent(session) == ("PUT", f"{BASE}/lens_elevation", {"elevation": 2.46})
    with pytest.raises(ValueError):
        await s.set_lens_elevation(session, "t", CAM, float("nan"))


async def test_set_intrusion_detection_config() -> None:
    session = _session(get={"detectionMode": "ALL_MOTIONS", "sensitivity": 2})
    await s.set_intrusion_detection_config(
        session, "t", CAM, {"detectionMode": "ZONES"}
    )
    assert _sent(session)[2] == {"detectionMode": "ZONES", "sensitivity": 2}
    with pytest.raises(ValueError):
        await s.set_intrusion_detection_config(session, "t", CAM, {})


async def test_set_regions_use_post_and_validate() -> None:
    session = _session()
    await s.set_motion_zones(session, "t", CAM, [s.Region(0.0, 0.5, 1.0, 0.25)])
    assert _sent(session) == (
        "POST",
        f"{BASE}/motion_sensitive_areas",
        [{"x": 0.0, "y": 0.5, "w": 1.0, "h": 0.25}],
    )
    await s.set_privacy_masks(session, "t", CAM, [])
    assert _sent(session) == ("POST", f"{BASE}/privacy_masks", [])
    for bad in (s.Region(1.1, 0, 0, 0), s.Region(0, 0, 0, float("nan"))):
        with pytest.raises(ValueError):
            await s.set_privacy_masks(session, "t", CAM, [bad])


# --- write error classification ---------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_write_auth_error(status: int) -> None:
    with pytest.raises(BoschCameraAuthError):
        await s.set_lens_elevation(_session(status=status), "t", CAM, 1.0)


async def test_write_privacy_mode_error() -> None:
    with pytest.raises(s.BoschCameraPrivacyModeError):
        await s.set_lens_elevation(_session(status=443), "t", CAM, 1.0)


async def test_write_http_error() -> None:
    with pytest.raises(BoschCameraConnectionError, match="HTTP 500"):
        await s.set_lens_elevation(_session(status=500), "t", CAM, 1.0)


@pytest.mark.parametrize("err", [TimeoutError(), aiohttp.ClientError("boom")])
async def test_write_connection_error(err: Exception) -> None:
    session = _session()
    session.request = MagicMock(side_effect=err)
    with pytest.raises(BoschCameraConnectionError):
        await s.set_lens_elevation(session, "t", CAM, 1.0)


def test_number_overflow_is_missing() -> None:
    assert s._number(10**400) is None


def test_check_range_rejects_fraction() -> None:
    with pytest.raises(ValueError):
        s._check_range("x", 2.5, (1, 5))  # type: ignore[arg-type]


async def test_merge_put_rejects_nan_changes() -> None:
    with pytest.raises(ValueError):
        await s._merge_put(
            None,
            "t",
            "c",
            "audio",
            {"a": float("nan")},
            "http://x",  # type: ignore[arg-type]
        )


# --- raw read / write ------------------------------------------------------


def _raw_session(status: int, payload: Any = None, raises: Exception | None = None):
    session = MagicMock()
    cm = _cm(status, payload, raises)
    session.get = MagicMock(return_value=cm)
    return session


async def test_get_setting_raw_ok() -> None:
    session = _raw_session(200, {"a": 1})
    assert await s.get_setting_raw(session, "t", CAM, "audio") == s.RawResponse(
        200, {"a": 1}
    )
    assert session.get.call_args.args[0] == f"{BASE}/audio"


@pytest.mark.parametrize("status", [401, 403, 442, 500])
async def test_get_setting_raw_status_not_raised(status: int) -> None:
    session = _raw_session(status, {"x": 1})
    assert await s.get_setting_raw(session, "t", CAM, "audio") == s.RawResponse(
        status, None
    )


async def test_get_setting_raw_malformed_body() -> None:
    session = _raw_session(200, None, ValueError("bad"))
    assert await s.get_setting_raw(session, "t", CAM, "audio") == s.RawResponse(
        200, None
    )


@pytest.mark.parametrize("err", [TimeoutError(), aiohttp.ClientError("x")])
async def test_get_setting_raw_connection_error(err: Exception) -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=err)
    assert await s.get_setting_raw(session, "t", CAM, "audio") == s.RawResponse(0, None)


async def test_raw_helpers_reject_unknown_endpoint() -> None:
    session = MagicMock()
    with pytest.raises(ValueError):
        await s.get_setting_raw(session, "t", CAM, "../x")
    with pytest.raises(ValueError):
        await s.put_setting(session, "t", CAM, "wifi info?x=1", {})
    with pytest.raises(ValueError):
        await s.put_setting(session, "t", CAM, "audio", {}, method="DELETE")
    with pytest.raises(ValueError):
        await s.get_setting_raw(session, "t", "../../x", "audio")


async def test_put_setting_sends_body_without_get() -> None:
    session = _session(put={"ok": True})
    result = await s.put_setting(session, "t", CAM, "lighting/motion", {"k": 1})
    assert result.status == 200
    assert _sent(session) == ("PUT", f"{BASE}/lighting/motion", {"k": 1})
    session.get.assert_not_called()


async def test_put_setting_post() -> None:
    session = _session(put=None)
    await s.put_setting(session, "t", CAM, "privacy_masks", [], method="POST")
    assert _sent(session)[0] == "POST"


async def test_put_setting_error_quotes_text() -> None:
    session = _session(status=500)
    session.request.return_value.__aenter__.return_value.text = AsyncMock(
        return_value="e" * 300
    )
    with pytest.raises(BoschCameraConnectionError) as info:
        await s.put_setting(session, "t", CAM, "audio", {})
    assert str(info.value) == "audio returned HTTP 500: " + "e" * 200


@pytest.mark.parametrize("status", [401, 403])
async def test_put_setting_auth(status: int) -> None:
    with pytest.raises(BoschCameraAuthError):
        await s.put_setting(_session(status=status), "t", CAM, "audio", {})
