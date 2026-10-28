"""Tests for live.py -- LOCAL live connection (fake data only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)
from bosch_shc_camera_client.live import LiveConnection, open_local_live_connection

pytestmark = pytest.mark.asyncio

CAM_ID = "11111111-1111-1111-1111-111111111111"
GOOD: dict[str, Any] = {
    "user": "cbs-user",
    "password": "p@ss/word",
    "urls": ["10.0.0.5:443"],
}


def _session(status: int = 200, payload: Any = None, raises: Exception | None = None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.request = MagicMock(return_value=cm, side_effect=raises)
    return session


async def test_open_returns_connection() -> None:
    session = _session(payload={**GOOD, "bufferingTime": 250})
    conn = await open_local_live_connection(session, "tok", CAM_ID, high_quality=False)
    assert conn == LiveConnection(
        connection_type="LOCAL",
        host="10.0.0.5",
        port=443,
        username="cbs-user",
        password="p@ss/word",
        snapshot_url="https://10.0.0.5:443/snap.jpg?JpegSize=1206",
        max_session_seconds=3600,
        buffering_ms=250,
    )
    args = session.request.call_args
    assert args.args[0] == "PUT"
    assert args.args[1].endswith(f"/v11/video_inputs/{CAM_ID}/connection")
    assert args.kwargs["json"] == {"type": "LOCAL", "highQualityVideo": False}
    assert "p@ss" not in repr(conn)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -5, "x", True])
async def test_bad_buffering_uses_default(value: Any) -> None:
    session = _session(payload={**GOOD, "bufferingTime": value})
    conn = await open_local_live_connection(session, "tok", CAM_ID)
    assert conn.buffering_ms == 500


async def test_stream_url_quotes_credentials() -> None:
    conn = await open_local_live_connection(_session(payload=GOOD), "tok", CAM_ID)
    assert conn.stream_url(5000) == (
        "rtsp://cbs-user:p%40ss%2Fword@127.0.0.1:5000/rtsp_tunnel"
        "?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600"
    )
    assert "inst=2&enableaudio=0" in conn.stream_url(
        5000, quality_instance=2, audio=False, proxy_host="127.0.0.2"
    )


@pytest.mark.parametrize("inst", [0, 5, True, 1.0])
async def test_stream_url_rejects_quality_instance(inst: int) -> None:
    conn = await open_local_live_connection(_session(payload=GOOD), "tok", CAM_ID)
    with pytest.raises(ValueError):
        conn.stream_url(5000, quality_instance=inst)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {**GOOD, "user": ""},
        {**GOOD, "user": 5},
        {**GOOD, "password": ""},
        {**GOOD, "password": None},
        {**GOOD, "urls": []},
        {**GOOD, "urls": [5]},
        {**GOOD, "urls": ["10.0.0.5"]},
        {**GOOD, "urls": ["10.0.0.5:abc"]},
        {**GOOD, "urls": ["10.0.0.5:0"]},
        {**GOOD, "urls": ["10.0.0.5:70000"]},
        {**GOOD, "urls": ["bad host:443"]},
        {**GOOD, "urls": [":443"]},
        {**GOOD, "urls": [".:443"]},
        {**GOOD, "urls": ["-a:443"]},
        {**GOOD, "urls": ["a..b:443"]},
        {**GOOD, "urls": ["10.0.0.5:" + "1" * 5000]},
    ],
)
async def test_invalid_payload(payload: Any) -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await open_local_live_connection(_session(payload=payload), "tok", CAM_ID)


@pytest.mark.parametrize("status", [401, 403])
async def test_token_rejected(status: int) -> None:
    with pytest.raises(BoschCameraAuthError):
        await open_local_live_connection(_session(status=status), "tok", CAM_ID)


async def test_http_error() -> None:
    with pytest.raises(BoschCameraConnectionError) as err:
        await open_local_live_connection(_session(status=500), "tok", CAM_ID)
    assert err.value.status == 500


async def test_network_error() -> None:
    with pytest.raises(BoschCameraConnectionError):
        await open_local_live_connection(_session(raises=TimeoutError()), "tok", CAM_ID)


async def test_stream_url_brackets_ipv6_proxy_host() -> None:
    conn = await open_local_live_connection(_session(payload=GOOD), "tok", CAM_ID)
    assert "@[::1]:5000/" in conn.stream_url(5000, proxy_host="::1")
