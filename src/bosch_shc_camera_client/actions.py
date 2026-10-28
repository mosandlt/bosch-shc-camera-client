"""Camera actions of the Bosch cloud API: ping, soft/hard reset, panic alarm.

The caller owns the aiohttp session and a valid token. A rejected token raises
`BoschCameraAuthError`; a network failure raises `BoschCameraConnectionError`.
Any other HTTP rejection is reported through the result, not raised.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp

from .cameras import (
    BoschCameraAuthError,
    BoschCameraConnectionError,
    BoschCameraInvalidResponseError,
)
from .cloud import CLOUD_API

ACTION_TIMEOUT = 10
PING_TIMEOUT = 5
_OK_STATUSES = (200, 201, 204)
SESSION_LIMIT_HTTP_STATUS = 444
SESSION_LIMIT = "SESSION_LIMIT"


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one camera action."""

    ok: bool
    status: int


def _video_input_url(api_url: str, camera_id: str, action: str) -> str:
    return f"{api_url}/v11/video_inputs/{camera_id}/{action}"


def _check_auth(status: int) -> None:
    if status in (401, 403):
        raise BoschCameraAuthError(f"Token rejected (HTTP {status})")


async def _put_action(
    session: aiohttp.ClientSession,
    token: str,
    url: str,
    body: dict[str, str] | None,
) -> ActionResult:
    """PUT to `url`; `body=None` sends no bytes, which the reset endpoints need."""
    kwargs = {"data": ""} if body is None else {"json": body}
    try:
        async with asyncio.timeout(ACTION_TIMEOUT):
            async with session.put(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                **kwargs,
            ) as resp:
                _check_auth(resp.status)
                return ActionResult(ok=resp.status in _OK_STATUSES, status=resp.status)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err


async def soft_reset(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> ActionResult:
    """Reboot the camera; it is briefly offline afterwards."""
    return await _put_action(
        session, token, _video_input_url(api_url, camera_id, "soft_reset"), None
    )


async def hard_reset(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> ActionResult:
    """Factory-reset the camera; it must be commissioned again afterwards."""
    return await _put_action(
        session, token, _video_input_url(api_url, camera_id, "hard_reset"), None
    )


async def panic_alarm(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    enabled: bool,
    *,
    api_url: str = CLOUD_API,
) -> ActionResult:
    """Start (`enabled`) or stop the siren of a Gen2 camera."""
    return await _put_action(
        session,
        token,
        _video_input_url(api_url, camera_id, "panic_alarm"),
        {"status": "ON" if enabled else "OFF"},
    )


async def ping(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    api_url: str = CLOUD_API,
) -> str:
    """Return the camera's cloud status, e.g. ONLINE, OFFLINE, UPDATING or SESSION_LIMIT."""
    try:
        async with asyncio.timeout(PING_TIMEOUT):
            async with session.get(
                _video_input_url(api_url, camera_id, "ping"),
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                _check_auth(resp.status)
                if resp.status == SESSION_LIMIT_HTTP_STATUS:
                    return SESSION_LIMIT
                if resp.status != 200:
                    raise BoschCameraConnectionError(
                        f"Ping returned HTTP {resp.status}"
                    )
                text = (await resp.text()).strip().strip('"')
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BoschCameraConnectionError(str(err) or type(err).__name__) from err
    if not text:
        raise BoschCameraInvalidResponseError("Ping answered without a status")
    return "UPDATING" if text.startswith("UPDATING") else text
