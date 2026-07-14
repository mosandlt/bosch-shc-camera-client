"""Tests for cloud.py — shared Bosch cloud-API PUT mechanics.

cloud_put_json is the single extracted piece of the source integration's
5 cloud-setter functions (privacy/light/light_component/notifications/pan):
build Bearer headers, PUT JSON with a timeout, classify the HTTP status,
optionally parse a 200 response body. Everything else (fallback tiers,
coordinator caches, notifications) stays in the source integration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cloud import CloudPutResult, cloud_put_json

URL = "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam1/privacy"


def _make_session(status: int, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.put = MagicMock(return_value=cm)
    return session


class TestCloudPutJsonSuccess:
    @pytest.mark.asyncio
    async def test_204_is_ok_with_no_body(self):
        session = _make_session(204)
        result = await cloud_put_json(session, "tok", URL, {"privacyMode": "ON"})
        assert result == CloudPutResult(ok=True, status=204, body=None)

    @pytest.mark.asyncio
    async def test_201_is_ok_with_no_body(self):
        session = _make_session(201)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.status == 201
        assert result.body is None

    @pytest.mark.asyncio
    async def test_200_with_json_body_is_parsed(self):
        session = _make_session(
            200, {"currentAbsolutePosition": 45, "estimatedTimeToCompletion": 1200}
        )
        result = await cloud_put_json(session, "tok", URL, {"absolutePosition": 45})
        assert result.ok is True
        assert result.status == 200
        assert result.body == {
            "currentAbsolutePosition": 45,
            "estimatedTimeToCompletion": 1200,
        }

    @pytest.mark.asyncio
    async def test_200_with_unparseable_body_falls_back_to_none(self):
        session = _make_session(200)
        session.put.return_value.__aenter__.return_value.json = AsyncMock(
            side_effect=ValueError("not json")
        )
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.status == 200
        assert result.body is None

    @pytest.mark.asyncio
    async def test_request_headers_include_bearer_token(self):
        session = _make_session(204)
        await cloud_put_json(session, "my-token-123", URL, {"a": 1})
        _, kwargs = session.put.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer my-token-123"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["json"] == {"a": 1}


class TestCloudPutJsonFailure:
    @pytest.mark.asyncio
    async def test_401_is_not_ok(self):
        session = _make_session(401)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is False
        assert result.status == 401
        assert result.body is None

    @pytest.mark.asyncio
    async def test_444_is_not_ok(self):
        session = _make_session(444)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is False
        assert result.status == 444

    @pytest.mark.asyncio
    async def test_500_is_not_ok(self):
        session = _make_session(500)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is False
        assert result.status == 500

    @pytest.mark.asyncio
    async def test_timeout_returns_ok_false_status_none(self):
        session = MagicMock()

        async def _raise(*a, **kw):
            raise TimeoutError

        cm = MagicMock()
        cm.__aenter__ = _raise
        session.put = MagicMock(return_value=cm)

        result = await cloud_put_json(session, "tok", URL, {})
        assert result == CloudPutResult(ok=False, status=None, body=None)

    @pytest.mark.asyncio
    async def test_client_error_returns_ok_false_status_none(self):
        session = MagicMock()

        async def _raise(*a, **kw):
            raise aiohttp.ClientError("connection reset")

        cm = MagicMock()
        cm.__aenter__ = _raise
        session.put = MagicMock(return_value=cm)

        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is False
        assert result.status is None
