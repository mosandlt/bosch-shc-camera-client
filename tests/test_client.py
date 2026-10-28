"""Tests for client.py -- delegation with a fresh token per call (fake data)."""

from __future__ import annotations

from itertools import count
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bosch_shc_camera_client.client import BoschCameraClient

pytestmark = pytest.mark.asyncio

CAM_ID = "11111111-1111-1111-1111-111111111111"
API = "https://api.example.invalid"


def _make(api_url: str | None = None) -> tuple[BoschCameraClient, MagicMock]:
    session = MagicMock()
    counter = count(1)

    async def source() -> str:
        return f"token-{next(counter)}"

    if api_url is None:
        return BoschCameraClient(session, source), session
    return BoschCameraClient(session, source, api_url=api_url), session


async def test_list_cameras_uses_fresh_token() -> None:
    client, session = _make(API)
    with patch(
        "bosch_shc_camera_client.client.list_cameras", AsyncMock(return_value=[])
    ) as fn:
        assert await client.list_cameras() == []
        await client.list_cameras()
    assert fn.await_args_list[0].args == (session, "token-1")
    assert fn.await_args_list[1].args == (session, "token-2")
    assert fn.await_args_list[0].kwargs == {"api_url": API}


async def test_default_api_url_is_cloud_api() -> None:
    from bosch_shc_camera_client.cloud import CLOUD_API

    client, _ = _make()
    with patch(
        "bosch_shc_camera_client.client.get_camera_account_states",
        AsyncMock(return_value={}),
    ) as fn:
        assert await client.get_camera_account_states() == {}
    assert fn.await_args.kwargs == {"api_url": CLOUD_API}


async def test_get_latest_event_image() -> None:
    client, session = _make()
    with patch(
        "bosch_shc_camera_client.client.get_latest_event_image",
        AsyncMock(return_value=b"img"),
    ) as fn:
        assert await client.get_latest_event_image(CAM_ID) == b"img"
        await client.get_latest_event_image(CAM_ID)
    assert fn.await_args_list[0].args == (session, "token-1", CAM_ID)
    assert fn.await_args_list[1].args == (session, "token-2", CAM_ID)


async def test_get_camera_account_states_token_changes() -> None:
    client, session = _make()
    with patch(
        "bosch_shc_camera_client.client.get_camera_account_states",
        AsyncMock(return_value={}),
    ) as fn:
        await client.get_camera_account_states()
        await client.get_camera_account_states()
    assert [c.args for c in fn.await_args_list] == [
        (session, "token-1"),
        (session, "token-2"),
    ]


async def test_set_privacy_mode() -> None:
    client, session = _make()
    sentinel = MagicMock()
    with patch(
        "bosch_shc_camera_client.client.cloud_set_privacy_mode",
        AsyncMock(return_value=sentinel),
    ) as fn:
        assert await client.set_privacy_mode(CAM_ID, True) is sentinel
        await client.set_privacy_mode(CAM_ID, False)
    assert fn.await_args_list[0].args == (session, "token-1", CAM_ID, True)
    assert fn.await_args_list[1].args == (session, "token-2", CAM_ID, False)


async def test_open_local_live_connection() -> None:
    client, session = _make(API)
    sentinel = MagicMock()
    with patch(
        "bosch_shc_camera_client.client.open_local_live_connection",
        AsyncMock(return_value=sentinel),
    ) as fn:
        assert await client.open_local_live_connection(CAM_ID) is sentinel
        await client.open_local_live_connection(CAM_ID, high_quality=False)
    assert fn.await_args_list[0].args == (session, "token-1", CAM_ID)
    assert fn.await_args_list[0].kwargs == {"high_quality": True, "api_url": API}
    assert fn.await_args_list[1].args == (session, "token-2", CAM_ID)
    assert fn.await_args_list[1].kwargs == {"high_quality": False, "api_url": API}
