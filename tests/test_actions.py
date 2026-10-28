"""Tests for actions.py — camera actions (fake data only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.actions import (
    ActionResult,
    hard_reset,
    panic_alarm,
    ping,
    soft_reset,
)
from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)

pytestmark = pytest.mark.asyncio

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _session(status: int = 204, text: str = "", raises: Exception | None = None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.put = MagicMock(return_value=cm, side_effect=raises)
    session.get = MagicMock(return_value=cm, side_effect=raises)
    return session


@pytest.mark.parametrize(
    ("action", "name"), [(soft_reset, "soft_reset"), (hard_reset, "hard_reset")]
)
async def test_reset_sends_empty_body(action, name: str) -> None:
    session = _session()
    result = await action(session, "tok", CAM_ID)
    assert result == ActionResult(ok=True, status=204)
    assert session.put.call_args.args[0].endswith(f"/v11/video_inputs/{CAM_ID}/{name}")
    assert session.put.call_args.kwargs["data"] == ""
    assert "json" not in session.put.call_args.kwargs


@pytest.mark.parametrize(("enabled", "status"), [(True, "ON"), (False, "OFF")])
async def test_panic_alarm_body(enabled: bool, status: str) -> None:
    session = _session()
    result = await panic_alarm(session, "tok", CAM_ID, enabled)
    assert result.ok is True
    assert session.put.call_args.kwargs["json"] == {"status": status}
    assert session.put.call_args.args[0].endswith("/panic_alarm")


async def test_action_rejected_is_reported_not_raised() -> None:
    assert await soft_reset(_session(status=404), "tok", CAM_ID) == ActionResult(
        ok=False, status=404
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_action_auth_error(status: int) -> None:
    with pytest.raises(BoschCameraAuthError):
        await hard_reset(_session(status=status), "tok", CAM_ID)


async def test_action_network_error() -> None:
    with pytest.raises(BoschCameraConnectionError):
        await soft_reset(_session(raises=aiohttp.ClientError("boom")), "tok", CAM_ID)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"ONLINE"\n', "ONLINE"),
        ("OFFLINE", "OFFLINE"),
        ("UPDATING_FW_9", "UPDATING"),
    ],
)
async def test_ping_status(text: str, expected: str) -> None:
    assert await ping(_session(status=200, text=text), "tok", CAM_ID) == expected


async def test_ping_session_limit() -> None:
    assert await ping(_session(status=444), "tok", CAM_ID) == "SESSION_LIMIT"


async def test_ping_errors() -> None:
    with pytest.raises(BoschCameraAuthError):
        await ping(_session(status=401), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await ping(_session(status=500), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await ping(_session(raises=TimeoutError()), "tok", CAM_ID)
    with pytest.raises(BoschCameraInvalidResponseError):
        await ping(_session(status=200, text='""'), "tok", CAM_ID)
