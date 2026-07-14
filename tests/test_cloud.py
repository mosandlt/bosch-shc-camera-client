"""Tests for cloud.py — shared Bosch cloud-API PUT mechanics.

cloud_put_json is the single extracted piece of the source integration's
5 cloud-setter functions (privacy/light/light_component/notifications/pan):
build Bearer headers, PUT JSON with a timeout, classify the HTTP status,
optionally parse the response body. Everything else (fallback tiers,
coordinator caches, notifications) stays in the source integration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cloud import CloudPutResult, cloud_put_json

URL = "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam1/privacy"


def _make_session(
    status: int, json_data: Any | None = None, text: str = ""
) -> MagicMock:
    """Build a mock session. `json_data=None` simulates a response with no
    parseable JSON body (e.g. a real HTTP 204's empty body) -- `resp.json()`
    raises, matching aiohttp's own behavior for an empty payload."""
    resp = MagicMock()
    resp.status = status
    if json_data is None:
        resp.json = AsyncMock(side_effect=ValueError("no JSON body"))
    else:
        resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=text)
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
        assert result == CloudPutResult(ok=True, status=204, body=None, text="")

    @pytest.mark.asyncio
    async def test_201_with_no_body_falls_back_to_none(self):
        session = _make_session(201)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.status == 201
        assert result.body is None

    @pytest.mark.asyncio
    async def test_201_with_json_body_is_parsed(self):
        """Some Bosch endpoints (e.g. lighting/switch) return a JSON body on
        201, not just 200 -- the parse attempt covers every ok status."""
        session = _make_session(201, {"frontLightSettings": {"brightness": 50}})
        result = await cloud_put_json(session, "tok", URL, {"enabled": True})
        assert result.ok is True
        assert result.body == {"frontLightSettings": {"brightness": 50}}

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
    async def test_200_with_unparsable_body_falls_back_to_none(self):
        session = _make_session(200)
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.status == 200
        assert result.body is None

    @pytest.mark.asyncio
    async def test_200_with_non_dict_json_falls_back_to_none(self):
        """A bare JSON array (or any non-object payload) is treated as
        unparsable, not stored as-is -- callers rely on `.body` being a
        dict-or-None so they can call `.get()` without their own isinstance
        check."""
        session = _make_session(200, ["unexpected", "array"])
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.body is None

    @pytest.mark.asyncio
    async def test_request_headers_include_bearer_token(self):
        session = _make_session(204)
        await cloud_put_json(session, "my-token-123", URL, {"a": 1})
        _, kwargs = session.put.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer my-token-123"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["json"] == {"a": 1}


class TestCloudPutJsonTextCapture:
    @pytest.mark.asyncio
    async def test_text_captured_on_success(self):
        session = _make_session(204, text="")
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.text == ""

    @pytest.mark.asyncio
    async def test_text_captured_on_400_for_diagnostics(self):
        """Bosch's own error body explains exactly which field was rejected
        (e.g. 'frontIlluminatorIntensity must not be set if frontLightOn is
        false') -- callers need this even on a non-2xx response."""
        session = _make_session(
            400, text='{"error":"frontIlluminatorIntensity must not be set"}'
        )
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is False
        assert result.text == '{"error":"frontIlluminatorIntensity must not be set"}'

    @pytest.mark.asyncio
    async def test_unreadable_text_falls_back_to_none(self):
        session = _make_session(204)
        session.put.return_value.__aenter__.return_value.text = AsyncMock(
            side_effect=ValueError("stream already consumed")
        )
        result = await cloud_put_json(session, "tok", URL, {})
        assert result.ok is True
        assert result.text is None


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
        assert result == CloudPutResult(ok=False, status=None, body=None, text=None)

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
