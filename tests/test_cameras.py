"""Tests for cameras.py — camera list from the cloud API (fake data only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from bosch_shc_camera_client.cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraError,
    BoschCameraInvalidResponseError,
    Camera,
    CameraModel,
    list_cameras,
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


async def test_lists_cameras_and_sends_bearer_header() -> None:
    session = _session(
        payload=[
            {
                "id": CAM_ID,
                "title": "Garden",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.25",
                "macAddress": "aa:bb:cc:dd:ee:ff",
            }
        ]
    )
    cameras = await list_cameras(session, "tok")
    assert cameras == [
        Camera(
            CAM_ID,
            "Garden",
            "HOME_Eyes_Outdoor",
            CameraModel.EYES_OUTDOOR_II,
            "9.40.25",
        )
    ]
    url = session.get.call_args.args[0]
    assert url == "https://residential.cbs.boschsecurity.com/v11/video_inputs"
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    ("hardware", "model"),
    [
        ("CAMERA_EYES", CameraModel.EYES_OUTDOOR),
        ("OUTDOOR", CameraModel.EYES_OUTDOOR),
        ("CAMERA_360", CameraModel.INDOOR_360),
        ("INDOOR", CameraModel.INDOOR_360),
        ("HOME_Eyes_Outdoor", CameraModel.EYES_OUTDOOR_II),
        ("HOME_Eyes_Indoor", CameraModel.EYES_INDOOR_II),
        ("SOMETHING_NEW", CameraModel.UNKNOWN),
    ],
)
async def test_model_mapping(hardware: str, model: CameraModel) -> None:
    session = _session(payload=[{"id": CAM_ID, "hardwareVersion": hardware}])
    (camera,) = await list_cameras(session, "tok")
    assert camera.model is model
    assert camera.hardware_version == hardware


async def test_missing_optional_fields() -> None:
    session = _session(payload=[{"id": CAM_ID, "title": "", "firmwareVersion": 5}])
    (camera,) = await list_cameras(session, "tok")
    assert camera == Camera(CAM_ID, CAM_ID, "", CameraModel.UNKNOWN, None)


async def test_empty_list() -> None:
    assert await list_cameras(_session(payload=[]), "tok", api_url="https://x") == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error(status: int) -> None:
    with pytest.raises(BoschCameraAuthError):
        await list_cameras(_session(status), "tok")


async def test_http_error_status() -> None:
    with pytest.raises(BoschCameraConnectionError, match="HTTP 503"):
        await list_cameras(_session(503), "tok")


@pytest.mark.parametrize("err", [TimeoutError(), aiohttp.ClientConnectionError("down")])
async def test_connection_errors(err: Exception) -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=err)
    with pytest.raises(BoschCameraConnectionError):
        await list_cameras(session, "tok")


async def test_non_json_body() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await list_cameras(_session(raises=ValueError("bad")), "tok")


@pytest.mark.parametrize(
    "payload", [{"id": CAM_ID}, ["text"], [{"title": "no id"}], [{"id": ""}]]
)
async def test_invalid_payload(payload: Any) -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await list_cameras(_session(payload=payload), "tok")


async def test_error_hierarchy() -> None:
    for exc in (
        BoschCameraAuthError,
        BoschCameraConnectionError,
        BoschCameraInvalidResponseError,
    ):
        assert issubclass(exc, BoschCameraError)


# ── get_latest_event_image ───────────────────────────────────────────────────

from bosch_shc_camera_client.cameras import (  # noqa: E402
    MAX_IMAGE_BYTES,
    get_latest_event_image,
)

IMG_URL = "https://media.residential.cbs.boschsecurity.com/e/1.jpg"


async def _chunks(body: bytes, size: int):
    for start in range(0, len(body), size):
        yield body[start : start + size]


def _resp(status: int = 200, *, payload: Any = None, body: bytes = b"jpg", raises=None):
    resp = MagicMock()
    resp.status = status
    resp.json = (
        AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=payload)
    )
    resp.content.iter_chunked = MagicMock(side_effect=lambda size: _chunks(body, size))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _events_session(events_resp, *image_resps):
    session = MagicMock()
    session.get = MagicMock(side_effect=[events_resp, *image_resps])
    return session


async def test_latest_event_image_returns_newest_usable_image() -> None:
    session = _events_session(
        _resp(payload=[{"id": "a"}, {"imageUrl": IMG_URL}, {"imageUrl": IMG_URL}]),
        _resp(body=b"JPEG"),
    )
    assert await get_latest_event_image(session, "tok", CAM_ID) == b"JPEG"
    events_call, image_call = session.get.call_args_list
    assert events_call.kwargs["params"] == {"videoInputId": CAM_ID}
    assert events_call.kwargs["headers"]["Authorization"] == "Bearer tok"
    assert image_call.args == (IMG_URL,)
    assert image_call.kwargs["allow_redirects"] is False


async def test_latest_event_image_skips_unsafe_missing_and_failed_images() -> None:
    session = _events_session(
        _resp(
            payload=[
                "not-a-dict",
                {"imageUrl": 5},
                {"imageUrl": "https://evil.example/x.jpg"},
                {"imageUrl": "http://media.boschsecurity.com/x.jpg"},
                {"imageUrl": "https://user@media.boschsecurity.com/x.jpg"},
                {"imageUrl": IMG_URL},  # HTTP 404
                {"imageUrl": IMG_URL},  # empty body
                {"imageUrl": IMG_URL},  # too large
                {"imageUrl": IMG_URL},  # timeout
                {"imageUrl": IMG_URL},  # invalid header value
                {"imageUrl": IMG_URL},  # ok
            ]
        ),
        _resp(404),
        _resp(body=b""),
        _resp(body=b"x" * (MAX_IMAGE_BYTES + 1)),
        MagicMock(
            __aenter__=AsyncMock(side_effect=TimeoutError), __aexit__=AsyncMock()
        ),
        _resp(body=b"GOOD"),
    )
    assert await get_latest_event_image(session, "tok", CAM_ID) == b"GOOD"
    assert session.get.call_count == 6


async def test_latest_event_image_none_when_no_event_has_one() -> None:
    session = _events_session(_resp(payload=[]))
    assert await get_latest_event_image(session, "tok", CAM_ID) is None


@pytest.mark.parametrize("status", [401, 403])
async def test_latest_event_image_auth_error(status: int) -> None:
    session = _events_session(_resp(status))
    with pytest.raises(BoschCameraAuthError):
        await get_latest_event_image(session, "tok", CAM_ID)


async def test_latest_event_image_http_error() -> None:
    session = _events_session(_resp(503))
    with pytest.raises(BoschCameraConnectionError):
        await get_latest_event_image(session, "tok", CAM_ID)


@pytest.mark.parametrize("raises", [aiohttp.ClientError, TimeoutError])
async def test_latest_event_image_connection_error(raises) -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=raises)
    with pytest.raises(BoschCameraConnectionError):
        await get_latest_event_image(session, "tok", CAM_ID)


async def test_latest_event_image_invalid_payloads() -> None:
    with pytest.raises(BoschCameraInvalidResponseError):
        await get_latest_event_image(
            _events_session(_resp(payload={"a": 1})), "tok", CAM_ID
        )
    with pytest.raises(BoschCameraInvalidResponseError):
        await get_latest_event_image(
            _events_session(_resp(raises=ValueError)), "tok", CAM_ID
        )


# ---------------------------------------------------------------------------
# account_id_from_token
# ---------------------------------------------------------------------------


def _token(claims: object) -> str:
    import base64
    import json

    def part(value: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return ".".join([part({"alg": "none"}), part(claims), "sig"])


def test_account_id_from_token_returns_subject() -> None:
    from bosch_shc_camera_client.cameras import account_id_from_token

    assert account_id_from_token(_token({"sub": "fake-account"})) == "fake-account"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "a.!!!.c",
        "a.bm90LWpzb24.c",
        _token([]),
        _token({}),
        _token({"sub": 1}),
        _token({"sub": "  "}),
        _token({"sub": "x" * 129}),
    ],
)
def test_account_id_from_token_rejects_unusable_tokens(token: str) -> None:
    from bosch_shc_camera_client.cameras import (
        BoschCameraAuthError,
        account_id_from_token,
    )

    with pytest.raises(BoschCameraAuthError):
        account_id_from_token(token)
