"""Tests for events.py — event list and feature flags (fake data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)
from bosch_shc_camera_client.events import (
    FeatureFlags,
    get_feature_flags,
    list_events,
    parse_event_timestamp,
)

pytestmark = pytest.mark.asyncio

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _session(status: int = 200, payload: Any = None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


async def test_list_events_full_event() -> None:
    session = _session(
        payload=[
            {
                "id": "e1",
                "timestamp": "2026-06-18T06:06:30.499+02:00[Europe/Berlin]",
                "eventType": "MOVEMENT",
                "eventTags": ["PERSON", 5],
                "imageUrl": "https://example.invalid/i.jpg",
                "videoClipUrl": "https://example.invalid/c.mp4",
                "videoClipUploadStatus": "DONE",
                "isRead": True,
            }
        ]
    )
    [event] = await list_events(session, "tok", CAM_ID, limit=5)
    assert event.id == "e1"
    assert event.camera_id == CAM_ID
    assert event.timestamp == datetime(2026, 6, 18, 4, 6, 30, 499000, tzinfo=UTC)
    assert event.event_type == "MOVEMENT"
    assert event.tags == ("PERSON",)
    assert event.image_url == "https://example.invalid/i.jpg"
    assert event.clip_url == "https://example.invalid/c.mp4"
    assert event.clip_status == "DONE"
    assert event.is_read is True
    assert session.get.call_args.args[0].endswith(
        f"/v11/events?videoInputId={CAM_ID}&limit=5"
    )


async def test_list_events_minimal_event_defaults() -> None:
    session = _session(payload=[{"id": "e2", "eventTags": None, "isRead": "yes"}])
    [event] = await list_events(session, "tok", CAM_ID)
    assert event.timestamp is None
    assert event.event_type == "UNKNOWN"
    assert event.tags == ()
    assert event.image_url is None
    assert event.is_read is False


@pytest.mark.parametrize(
    "payload", [{"a": 1}, ["x"], [{"timestamp": "2026-01-01T00:00:00Z"}], [{"id": ""}]]
)
async def test_list_events_invalid_payload(payload: Any) -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await list_events(_session(payload=payload), "tok", CAM_ID)


async def test_list_events_auth_and_status() -> None:
    with pytest.raises(BoschCameraAuthError):
        await list_events(_session(status=401), "tok", CAM_ID)
    with pytest.raises(BoschCameraConnectionError):
        await list_events(_session(status=500), "tok", CAM_ID)


async def test_list_events_network_error() -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(BoschCameraConnectionError):
        await list_events(session, "tok", CAM_ID)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-22T14:30:00.000Z", datetime(2026, 3, 22, 14, 30, tzinfo=UTC)),
        ("2026-03-19T09:32:08", datetime(2026, 3, 19, 9, 32, 8, tzinfo=UTC)),
        ("garbage", None),
        ("", None),
        (None, None),
        (5, None),
    ],
)
async def test_parse_event_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_event_timestamp(value) == expected


async def test_get_feature_flags() -> None:
    session = _session(payload={"b": True, "a": True, "c": False})
    flags = await get_feature_flags(session, "tok")
    assert flags.flags == {"b": True, "a": True, "c": False}
    assert flags.enabled == ("a", "b")
    assert session.get.call_args.args[0].endswith("/v11/feature_flags")


async def test_get_feature_flags_empty() -> None:
    flags = await get_feature_flags(_session(payload={}), "tok")
    assert flags == FeatureFlags(flags={})
    assert flags.enabled == ()


@pytest.mark.parametrize("payload", [[], {"a": "yes"}, {"a": 1}])
async def test_get_feature_flags_invalid(payload: Any) -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await get_feature_flags(_session(payload=payload), "tok")


def test_timestamp_overflow_is_none() -> None:
    assert parse_event_timestamp("0001-01-01T00:00:00+01:00") is None
