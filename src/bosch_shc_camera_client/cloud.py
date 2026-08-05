"""Shared HTTP mechanics for the Bosch cloud API (residential.cbs.boschsecurity.com).

`cloud_put_json` is the low-level PUT mechanics: build Bearer headers, apply
a timeout, classify the HTTP status, optionally parse the response body.

Layered on top of it, `cloud_set_privacy_mode`/`cloud_set_camera_light`/
`cloud_set_notifications`/`cloud_set_pan` are the PURE per-setter request
builders for the source integration's 4 named cloud write operations
(privacy mode, camera light on/off, notifications, pan) -- they know the
exact endpoint URL and JSON body shape for each write and hand the result
back as a typed dataclass. They take an already-open aiohttp session and a
KNOWN-VALID bearer token; they do not touch HA, a coordinator, or any
token-refresh/retry-on-401 policy.

The 401-detection + refresh-and-retry-once orchestration, the multi-tier
fallback cascade (cloud -> local RCP -> SHC local API), coordinator cache
writes, and notification side effects all stay in the source integration
(`shc.py`), which calls these functions once (and, only for privacy mode,
retries once after a refreshed token) -- same design as `rcp.py`'s
already-thin wrappers around this module's session/protocol primitives.

`async_cloud_set_light_component` (the more complex front/wallwasher/
intensity setter, whose Gen2 wallwasher path depends on multiple
coordinator-owned caches to build its request body) is intentionally NOT
extracted here -- it isn't one of the 4 named setters and its body-building
isn't stateless.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Bosch's cloud API returns any of these for a successful write.
_OK_STATUSES = (200, 201, 204)

# Matches the source integration's own CLOUD_API constant
# (custom_components/bosch_shc_camera/shc.py) byte-for-byte.
CLOUD_API = "https://residential.cbs.boschsecurity.com"


@dataclass
class CloudPutResult:
    """Outcome of a single `cloud_put_json` call."""

    ok: bool
    status: int | None
    body: dict[str, Any] | None = None
    text: str | None = None


async def cloud_put_json(
    session: aiohttp.ClientSession,
    token: str,
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> CloudPutResult:
    """PUT `body` as JSON to `url` with Bearer auth.

    Returns a `CloudPutResult`: `ok=True` for HTTP 200/201/204, `status=None`
    if the request timed out or hit a network error (caught, not raised --
    same graceful-degradation contract every caller in the source integration
    already relied on). `body` is the parsed JSON response whenever the write
    succeeded and the response has a JSON payload -- attempted for any of
    200/201/204, since some Bosch endpoints return a body on 201 as well as
    200, and a 204's empty body simply fails to parse and falls back to
    `None` (matches every existing caller's expectation either way).
    `text` is the raw response text whenever a real HTTP response was
    received (any status, not just success) -- useful for logging the API's
    own error message on a non-2xx response (e.g. Bosch's 400 body explains
    exactly which field was rejected and why). `body` is only ever a `dict`
    or `None` -- a non-object JSON response (e.g. a bare array) is treated as
    unparsable and discarded, so callers can safely call `.get()` on it
    without an `isinstance` check of their own.

    Failures are logged at DEBUG here, not WARNING -- every caller in the
    source integration already logs its own WARNING with richer context
    (camera id, which of several endpoints, etc.) once it sees `ok=False`,
    so a second WARNING at this layer would just be duplicate noise for the
    same failure.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with asyncio.timeout(timeout):
            async with session.put(url, json=body, headers=headers) as resp:
                ok = resp.status in _OK_STATUSES
                parsed: dict[str, Any] | None = None
                text: str | None = None
                try:
                    text = await resp.text()
                except Exception:  # noqa: S110 # defensive text read; status already known, caller has a safe default
                    pass
                if ok:
                    try:
                        candidate = await resp.json()
                    except Exception:  # noqa: S110 # defensive JSON parse; write already sent, caller has a safe default
                        pass
                    else:
                        if isinstance(candidate, dict):
                            parsed = candidate
                if not ok:
                    _LOGGER.debug("cloud_put_json: HTTP %d for %s", resp.status, url)
                return CloudPutResult(ok=ok, status=resp.status, body=parsed, text=text)
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("cloud_put_json: error for %s: %s", url, err)
        return CloudPutResult(ok=False, status=None, body=None, text=None)


async def cloud_set_privacy_mode(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    enabled: bool,
) -> CloudPutResult:
    """PUT the privacy-mode write body to `/v11/video_inputs/{cam_id}/privacy`.

    Pure: builds the exact URL + body the source integration used inline
    before this extraction and hands back `cloud_put_json`'s result
    unmodified. Caller (`shc.py`) still owns the 401-retry-once, cache
    write, write-lock timestamp, and the privacy-off snapshot trigger.
    """
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy"
    body = {"privacyMode": "ON" if enabled else "OFF", "durationInSeconds": None}
    return await cloud_put_json(session, token, url, body)


@dataclass
class CameraLightSetResult:
    """Outcome of a single `cloud_set_camera_light` call.

    Gen2 issues two independent PUTs (front + topdown); `status`/`text` mirror
    the front-endpoint result (kept for the Gen1 single-PUT shape's
    `CloudPutResult`-like ergonomics), `topdown_status` is only populated on
    the Gen2 path.
    """

    ok: bool
    status: int | None
    text: str | None = None
    topdown_status: int | None = None
    topdown_text: str | None = None


async def cloud_set_camera_light(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    on: bool,
    *,
    gen2: bool,
    last_front_intensity: float = 1.0,
) -> CameraLightSetResult:
    """PUT the camera-light on/off write body(ies) for `cam_id`.

    Gen2: two endpoints, `/lighting/switch/front` and `/lighting/switch/topdown`,
    each with `{"enabled": on}`; overall `ok` is true if EITHER PUT succeeded
    (matches the source integration's `r1.ok or r2.ok`).
    Gen1: single `/lighting_override` endpoint. Turning ON sends
    `frontLightOn`/`wallwasherOn`=True plus `frontLightIntensity` (the
    caller's last-cached intensity, defaulting to 1.0); turning OFF omits
    `frontLightIntensity` entirely (Bosch rejects it with HTTP 400 when
    `frontLightOn` is False).

    Pure: caller (`shc.py`) still owns the cache writes, `light_set_at`
    write-lock timestamp, and the SHC-fallback tier -- this function has no
    401-retry (the source integration never retried this setter either).
    """
    if gen2:
        base = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting/switch"
        body_toggle = {"enabled": on}
        r1 = await cloud_put_json(session, token, f"{base}/front", body_toggle)
        r2 = await cloud_put_json(session, token, f"{base}/topdown", body_toggle)
        return CameraLightSetResult(
            ok=r1.ok or r2.ok,
            status=r1.status,
            text=r1.text,
            topdown_status=r2.status,
            topdown_text=r2.text,
        )

    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override"
    if on:
        body: dict[str, Any] = {
            "frontLightOn": True,
            "wallwasherOn": True,
            "frontLightIntensity": last_front_intensity,
        }
    else:
        body = {"frontLightOn": False, "wallwasherOn": False}
    result = await cloud_put_json(session, token, url, body)
    return CameraLightSetResult(ok=result.ok, status=result.status, text=result.text)


async def cloud_set_notifications(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    enabled: bool,
) -> CloudPutResult:
    """PUT the notifications write body to `/v11/video_inputs/{cam_id}/enable_notifications`.

    `enabled` maps to Bosch's own `FOLLOW_CAMERA_SCHEDULE`/`ALWAYS_OFF`
    status strings. Pure: caller (`shc.py`) still owns the cache write,
    `notif_set_at` write-lock timestamp, and the notification-on-total-
    failure side effect -- there is no local/SHC fallback for this endpoint.
    """
    status = "FOLLOW_CAMERA_SCHEDULE" if enabled else "ALWAYS_OFF"
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/enable_notifications"
    body = {"enabledNotificationsStatus": status}
    return await cloud_put_json(session, token, url, body)


@dataclass
class PanSetResult:
    """Outcome of a single `cloud_set_pan` call.

    `position`/`eta_ms` are only populated when the write succeeded:
    Bosch's HTTP 200 returns a JSON body with `currentAbsolutePosition` +
    `estimatedTimeToCompletion`; a 204 has no body, in which case `position`
    falls back to the originally requested value and `eta_ms` to 0 -- same
    fallback the source integration used inline before this extraction.
    """

    ok: bool
    status: int | None
    position: int | None = None
    eta_ms: int = 0


async def cloud_set_pan(
    session: aiohttp.ClientSession,
    token: str,
    cam_id: str,
    position: int,
) -> PanSetResult:
    """PUT the pan write body to `/v11/video_inputs/{cam_id}/pan`.

    Pure: caller (`shc.py`) still owns the privacy-mode pre-check (pan is
    blocked while privacy mode is ON), the `pan_cache` write, and listener
    notification.
    """
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan"
    result = await cloud_put_json(session, token, url, {"absolutePosition": position})
    if not result.ok:
        return PanSetResult(ok=False, status=result.status)
    actual = position
    eta = 0
    if result.body is not None:
        actual = result.body.get("currentAbsolutePosition", position)
        eta = result.body.get("estimatedTimeToCompletion", 0)
    return PanSetResult(ok=True, status=result.status, position=actual, eta_ms=eta)
