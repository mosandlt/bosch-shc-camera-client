"""Tests for cloud.py — shared Bosch cloud-API PUT mechanics + pure setters.

cloud_put_json is the low-level PUT mechanics: build Bearer headers, PUT
JSON with a timeout, classify the HTTP status, optionally parse the response
body.

cloud_set_privacy_mode / cloud_set_camera_light / cloud_set_notifications /
cloud_set_pan are the pure per-setter request builders for the source
integration's 4 named cloud write operations — they build the exact
endpoint URL + JSON body and delegate to cloud_put_json. Everything else
(401-retry-once, fallback tiers, coordinator caches, notifications) stays in
the source integration. async_cloud_set_light_component is NOT extracted —
its body-building depends on multiple coordinator-owned caches, so it stays
in the source integration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cloud import (
    CameraLightSetResult,
    CloudPutResult,
    PanSetResult,
    cloud_put_json,
    cloud_set_camera_light,
    cloud_set_notifications,
    cloud_set_pan,
    cloud_set_privacy_mode,
)

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


class TestCloudSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_on_builds_exact_url_and_body(self):
        session = _make_session(204)
        result = await cloud_set_privacy_mode(session, "tok", "cam-1", True)
        assert result == CloudPutResult(ok=True, status=204, body=None, text="")
        args, kwargs = session.put.call_args
        assert (
            args[0]
            == "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/privacy"
        )
        assert kwargs["json"] == {"privacyMode": "ON", "durationInSeconds": None}

    @pytest.mark.asyncio
    async def test_off_builds_exact_body(self):
        session = _make_session(204)
        await cloud_set_privacy_mode(session, "tok", "cam-1", False)
        _, kwargs = session.put.call_args
        assert kwargs["json"] == {"privacyMode": "OFF", "durationInSeconds": None}

    @pytest.mark.asyncio
    async def test_401_propagates_as_not_ok(self):
        session = _make_session(401)
        result = await cloud_set_privacy_mode(session, "tok", "cam-1", True)
        assert result.ok is False
        assert result.status == 401


class TestCloudSetCameraLightGen1:
    @pytest.mark.asyncio
    async def test_on_sends_intensity(self):
        session = _make_session(204)
        result = await cloud_set_camera_light(
            session, "tok", "cam-1", True, gen2=False, last_front_intensity=0.7
        )
        assert result == CameraLightSetResult(ok=True, status=204, text="")
        args, kwargs = session.put.call_args
        assert args[0] == (
            "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/lighting_override"
        )
        assert kwargs["json"] == {
            "frontLightOn": True,
            "wallwasherOn": True,
            "frontLightIntensity": 0.7,
        }

    @pytest.mark.asyncio
    async def test_off_omits_intensity(self):
        session = _make_session(204)
        result = await cloud_set_camera_light(
            session, "tok", "cam-1", False, gen2=False
        )
        assert result.ok is True
        _, kwargs = session.put.call_args
        assert kwargs["json"] == {"frontLightOn": False, "wallwasherOn": False}
        assert "frontLightIntensity" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_default_intensity_is_1_0(self):
        session = _make_session(204)
        await cloud_set_camera_light(session, "tok", "cam-1", True, gen2=False)
        _, kwargs = session.put.call_args
        assert kwargs["json"]["frontLightIntensity"] == 1.0

    @pytest.mark.asyncio
    async def test_failure_status_propagates(self):
        session = _make_session(500)
        result = await cloud_set_camera_light(session, "tok", "cam-1", True, gen2=False)
        assert result.ok is False
        assert result.status == 500
        assert result.topdown_status is None


class TestCloudSetCameraLightGen2:
    @pytest.mark.asyncio
    async def test_on_calls_front_and_topdown_endpoints(self):
        session = MagicMock()
        session.put.side_effect = [
            _make_session(204).put.return_value,
            _make_session(204).put.return_value,
        ]
        result = await cloud_set_camera_light(session, "tok", "cam-1", True, gen2=True)
        assert result.ok is True
        assert session.put.call_count == 2
        first_url = session.put.call_args_list[0][0][0]
        second_url = session.put.call_args_list[1][0][0]
        assert first_url == (
            "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/lighting/switch/front"
        )
        assert second_url == (
            "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/lighting/switch/topdown"
        )
        first_body = session.put.call_args_list[0][1]["json"]
        second_body = session.put.call_args_list[1][1]["json"]
        assert first_body == {"enabled": True}
        assert second_body == {"enabled": True}

    @pytest.mark.asyncio
    async def test_ok_if_either_endpoint_succeeds(self):
        session = MagicMock()
        session.put.side_effect = [
            _make_session(500).put.return_value,
            _make_session(204).put.return_value,
        ]
        result = await cloud_set_camera_light(session, "tok", "cam-1", False, gen2=True)
        assert result.ok is True
        assert result.status == 500
        assert result.topdown_status == 204

    @pytest.mark.asyncio
    async def test_not_ok_if_both_endpoints_fail(self):
        session = MagicMock()
        session.put.side_effect = [
            _make_session(500).put.return_value,
            _make_session(503).put.return_value,
        ]
        result = await cloud_set_camera_light(session, "tok", "cam-1", True, gen2=True)
        assert result.ok is False
        assert result.status == 500
        assert result.topdown_status == 503


class TestCloudSetNotifications:
    @pytest.mark.asyncio
    async def test_enabled_maps_to_follow_camera_schedule(self):
        session = _make_session(204)
        result = await cloud_set_notifications(session, "tok", "cam-1", True)
        assert result == CloudPutResult(ok=True, status=204, body=None, text="")
        args, kwargs = session.put.call_args
        assert args[0] == (
            "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/enable_notifications"
        )
        assert kwargs["json"] == {
            "enabledNotificationsStatus": "FOLLOW_CAMERA_SCHEDULE"
        }

    @pytest.mark.asyncio
    async def test_disabled_maps_to_always_off(self):
        session = _make_session(204)
        await cloud_set_notifications(session, "tok", "cam-1", False)
        _, kwargs = session.put.call_args
        assert kwargs["json"] == {"enabledNotificationsStatus": "ALWAYS_OFF"}

    @pytest.mark.asyncio
    async def test_failure_status_propagates(self):
        session = _make_session(403)
        result = await cloud_set_notifications(session, "tok", "cam-1", True)
        assert result.ok is False
        assert result.status == 403


class TestCloudSetPan:
    @pytest.mark.asyncio
    async def test_200_with_body_uses_response_position_and_eta(self):
        session = _make_session(
            200, {"currentAbsolutePosition": 30, "estimatedTimeToCompletion": 900}
        )
        result = await cloud_set_pan(session, "tok", "cam-1", 45)
        assert result == PanSetResult(ok=True, status=200, position=30, eta_ms=900)
        args, kwargs = session.put.call_args
        assert (
            args[0]
            == "https://residential.cbs.boschsecurity.com/v11/video_inputs/cam-1/pan"
        )
        assert kwargs["json"] == {"absolutePosition": 45}

    @pytest.mark.asyncio
    async def test_204_with_no_body_falls_back_to_requested_position(self):
        session = _make_session(204)
        result = await cloud_set_pan(session, "tok", "cam-1", -45)
        assert result == PanSetResult(ok=True, status=204, position=-45, eta_ms=0)

    @pytest.mark.asyncio
    async def test_failure_returns_no_position(self):
        session = _make_session(500)
        result = await cloud_set_pan(session, "tok", "cam-1", 10)
        assert result == PanSetResult(ok=False, status=500, position=None, eta_ms=0)
