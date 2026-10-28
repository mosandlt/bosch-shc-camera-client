"""Tests for states.py — camera state reads from the cloud API (fake data only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)
from bosch_shc_camera_client.states import (
    CameraAccountStates,
    FirmwareStatus,
    LightState,
    MotionSettings,
    WifiInfo,
    get_camera_account_states,
    get_firmware_status,
    get_light_state,
    get_motion_settings,
    get_notification_types,
    get_wifi_info,
)

pytestmark = pytest.mark.asyncio

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _session(status: int = 200, payload: Any = None, raises: Exception | None = None):
    resp = MagicMock()
    resp.status = status
    if raises is not None:
        resp.json = AsyncMock(side_effect=raises)
    else:
        resp.json = AsyncMock(return_value=payload)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


def _failing_session(exc: Exception):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


GETTERS = [
    get_wifi_info,
    get_motion_settings,
    get_notification_types,
    get_light_state,
    get_firmware_status,
]


@pytest.mark.parametrize("getter", GETTERS)
async def test_error_mapping(getter: Any) -> None:
    with pytest.raises(BoschCameraAuthError):
        await getter(_session(401), "tok", CAM_ID)
    with pytest.raises(BoschCameraAuthError):
        await getter(_session(403), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await getter(_session(500), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await getter(_failing_session(TimeoutError()), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await getter(_failing_session(aiohttp.ClientError("boom")), "tok", CAM_ID)
    with pytest.raises(BoschCameraInvalidResponseError):
        await getter(_session(raises=ValueError("bad")), "tok", CAM_ID)
    with pytest.raises(BoschCameraInvalidResponseError):
        await getter(_session(payload=[1]), "tok", CAM_ID)


async def test_request_url_and_header() -> None:
    session = _session(payload={"signalStrength": 80})
    await get_wifi_info(session, "tok", CAM_ID, api_url="https://x.test")
    args, kwargs = session.get.call_args
    assert args[0] == f"https://x.test/v11/video_inputs/{CAM_ID}/wifiinfo"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


async def test_wifi_info() -> None:
    assert await get_wifi_info(
        _session(payload={"signalStrength": 72.0}), "t", CAM_ID
    ) == WifiInfo(72)
    for body in ({}, {"signalStrength": True}, {"signalStrength": "x"}):
        assert (
            await get_wifi_info(_session(payload=body), "t", CAM_ID)
        ).signal_strength is None


async def test_motion_settings() -> None:
    body = {"enabled": True, "motionAlarmConfiguration": "HIGH"}
    assert await get_motion_settings(_session(payload=body), "t", CAM_ID) == (
        MotionSettings(True, "HIGH")
    )
    assert await get_motion_settings(
        _session(payload={"enabled": "yes"}), "t", CAM_ID
    ) == (MotionSettings(False, None))


async def test_notification_types() -> None:
    body = {"cameraAlarm": True, "troubleEmail": False, "other": "x", "n": 1}
    assert await get_notification_types(_session(payload=body), "t", CAM_ID) == {
        "cameraAlarm": True,
        "troubleEmail": False,
    }


async def test_light_state() -> None:
    body = {"frontLightOn": True, "wallwasherOn": False, "frontLightIntensity": 1}
    state = await get_light_state(_session(payload=body), "t", CAM_ID)
    assert state == LightState(True, False, 1.0)
    assert state.any_on
    off = await get_light_state(
        _session(
            payload={
                "frontLightOn": False,
                "wallwasherOn": False,
                "frontLightIntensity": True,
            }
        ),
        "t",
        CAM_ID,
    )
    assert off == LightState(False, False, None)
    assert not off.any_on
    with pytest.raises(BoschCameraInvalidResponseError):
        await get_light_state(_session(payload={"frontLightOn": True}), "t", CAM_ID)


async def test_firmware_status() -> None:
    body = {
        "current": "9.40.25",
        "update": "9.40.30",
        "upToDate": False,
        "updating": True,
    }
    assert await get_firmware_status(_session(payload=body), "t", CAM_ID) == (
        FirmwareStatus("9.40.25", "9.40.30", False, True)
    )
    assert await get_firmware_status(
        _session(payload={"current": "", "upToDate": 1}), "t", CAM_ID
    ) == (FirmwareStatus(None, None, None, False))


async def test_account_states() -> None:
    payload = [
        {"id": CAM_ID, "privacyMode": "on", "notificationsEnabledStatus": "ALWAYS_OFF"},
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "privacyMode": "OFF",
            "notificationsEnabledStatus": "FOLLOW_CAMERA_SCHEDULE",
        },
        {"id": "33333333-3333-3333-3333-333333333333", "privacyMode": 5},
        {"id": ""},
        {"title": "no id"},
        "junk",
    ]
    states = await get_camera_account_states(_session(payload=payload), "t")
    assert states[CAM_ID] == CameraAccountStates(True, "ALWAYS_OFF")
    assert states[CAM_ID].notifications_enabled is False
    second = states["22222222-2222-2222-2222-222222222222"]
    assert second.privacy_mode is False
    assert second.notifications_enabled is True
    third = states["33333333-3333-3333-3333-333333333333"]
    assert third == CameraAccountStates(None, None)
    assert third.notifications_enabled is None
    assert len(states) == 3


async def test_account_states_not_array() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await get_camera_account_states(_session(payload={}), "t")
