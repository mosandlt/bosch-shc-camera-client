"""Shared HTTP mechanics for the Bosch cloud API (residential.cbs.boschsecurity.com).

This is deliberately the ONLY thing extracted from the source integration's
cloud-setter functions (privacy/light/notifications/pan) — the actual
multi-tier fallback orchestration (cloud -> local RCP -> SHC local API),
coordinator cache writes, and notification side effects stay in the
integration, where that coordinator-specific state genuinely lives. This
module only removes the duplicated "PUT a JSON body with Bearer auth, apply
a timeout, classify the status, optionally parse the response body" pattern
that's byte-for-byte repeated across every setter.
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
    if the request timed out or hit a network error (caught, not raised —
    same graceful-degradation contract every caller in the source integration
    already relied on). `body` is the parsed JSON response whenever the write
    succeeded and the response has a JSON payload — attempted for any of
    200/201/204, since some Bosch endpoints return a body on 201 as well as
    200, and a 204's empty body simply fails to parse and falls back to
    `None` (matches every existing caller's expectation either way).
    `text` is the raw response text whenever a real HTTP response was
    received (any status, not just success) — useful for logging the API's
    own error message on a non-2xx response (e.g. Bosch's 400 body explains
    exactly which field was rejected and why).
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
                        parsed = await resp.json()
                    except Exception:  # noqa: S110 # defensive JSON parse; write already sent, caller has a safe default
                        pass
                if not ok:
                    _LOGGER.warning("cloud_put_json: HTTP %d for %s", resp.status, url)
                return CloudPutResult(ok=ok, status=resp.status, body=parsed, text=text)
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("cloud_put_json: error for %s: %s", url, err)
        return CloudPutResult(ok=False, status=None, body=None, text=None)
