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
