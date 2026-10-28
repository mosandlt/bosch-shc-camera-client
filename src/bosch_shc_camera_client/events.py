"""Event list and account feature flags from the Bosch cloud API.

Only metadata is parsed here: image and clip URLs are returned as given and
never downloaded. The caller owns the aiohttp session and a valid token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .cameras import BoschCameraInvalidResponseError, fetch_json
from .cloud import CLOUD_API

DEFAULT_EVENT_LIMIT = 20
EVENTS_TIMEOUT = 15
FLAGS_TIMEOUT = 5


@dataclass(frozen=True)
class Event:
    """One camera event; `timestamp` is UTC, or None if the API value is unparsable."""

    id: str
    camera_id: str
    timestamp: datetime | None
    event_type: str
    tags: tuple[str, ...] = ()
    image_url: str | None = None
    clip_url: str | None = None
    clip_status: str | None = None
    is_read: bool = False


@dataclass(frozen=True)
class FeatureFlags:
    """Account-level feature flags as name -> enabled."""

    flags: dict[str, bool]

    @property
    def enabled(self) -> tuple[str, ...]:
        """Sorted names of the enabled flags."""
        return tuple(sorted(name for name, on in self.flags.items() if on))


def parse_event_timestamp(value: object) -> datetime | None:
    """Parse an API timestamp into UTC, honoring its offset; None if unusable.

    The API appends an RFC 9557 zone such as ``[Europe/Berlin]`` after the
    offset, which `fromisoformat` rejects, so it is stripped first. A naive
    value is taken as UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.split("[", 1)[0])
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_event(item: Any, camera_id: str) -> Event:
    if not isinstance(item, dict):
        raise BoschCameraInvalidResponseError("Event is not an object")
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise BoschCameraInvalidResponseError("Event has no id")
    tags = item.get("eventTags")
    return Event(
        id=event_id,
        camera_id=camera_id,
        timestamp=parse_event_timestamp(item.get("timestamp")),
        event_type=_text(item.get("eventType")) or "UNKNOWN",
        tags=tuple(t for t in tags if isinstance(t, str))
        if isinstance(tags, list)
        else (),
        image_url=_text(item.get("imageUrl")),
        clip_url=_text(item.get("videoClipUrl")),
        clip_status=_text(item.get("videoClipUploadStatus")),
        is_read=item.get("isRead") is True,
    )


async def list_events(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    limit: int = DEFAULT_EVENT_LIMIT,
    api_url: str = CLOUD_API,
) -> list[Event]:
    """Return the newest events of a camera, newest first."""
    payload = await fetch_json(
        session,
        token,
        f"{api_url}/v11/events?videoInputId={camera_id}&limit={limit}",
        "Event list",
        EVENTS_TIMEOUT,
    )
    if not isinstance(payload, list):
        raise BoschCameraInvalidResponseError("Event list is not an array")
    return [_parse_event(item, camera_id) for item in payload]


async def get_feature_flags(
    session: aiohttp.ClientSession, token: str, *, api_url: str = CLOUD_API
) -> FeatureFlags:
    """Return the account's feature flags."""
    payload = await fetch_json(
        session, token, f"{api_url}/v11/feature_flags", "Feature flags", FLAGS_TIMEOUT
    )
    if not isinstance(payload, dict) or not all(
        isinstance(v, bool) for v in payload.values()
    ):
        raise BoschCameraInvalidResponseError("Feature flags are not a bool map")
    return FeatureFlags(flags=dict(payload))
