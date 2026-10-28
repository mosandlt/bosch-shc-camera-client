"""Client object bundling an aiohttp session with a token source.

The token source is awaited on every call, so a caller that refreshes its
OAuth token (for example Home Assistant) always hands the library a valid
access token. The module-level functions stay available and unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp

from .cameras import Camera, get_latest_event_image, list_cameras
from .cloud import CLOUD_API, CloudPutResult, cloud_set_privacy_mode
from .live import LiveConnection, open_local_live_connection
from .states import CameraAccountStates, get_camera_account_states

TokenSource = Callable[[], Awaitable[str]]


class BoschCameraClient:
    """Cloud client holding the HTTP session and an async token source."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_source: TokenSource,
        *,
        api_url: str = CLOUD_API,
    ) -> None:
        """Store the session, the async `() -> str` token source and API base."""
        self._session = session
        self._token_source = token_source
        self._api_url = api_url

    async def list_cameras(self) -> list[Camera]:
        """Return the cameras of the account."""
        return await list_cameras(
            self._session, await self._token_source(), api_url=self._api_url
        )

    async def get_latest_event_image(self, camera_id: str) -> bytes | None:
        """Return the image of the newest event of a camera, if any."""
        return await get_latest_event_image(
            self._session,
            await self._token_source(),
            camera_id,
            api_url=self._api_url,
        )

    async def get_camera_account_states(self) -> dict[str, CameraAccountStates]:
        """Return privacy and notification state per camera id."""
        return await get_camera_account_states(
            self._session, await self._token_source(), api_url=self._api_url
        )

    async def set_privacy_mode(self, camera_id: str, enabled: bool) -> CloudPutResult:
        """Switch privacy mode of a camera on or off."""
        return await cloud_set_privacy_mode(
            self._session, await self._token_source(), camera_id, enabled
        )

    async def open_local_live_connection(
        self, camera_id: str, *, high_quality: bool = True
    ) -> LiveConnection:
        """Open a LOCAL live session; this rotates the Digest credentials."""
        return await open_local_live_connection(
            self._session,
            await self._token_source(),
            camera_id,
            high_quality=high_quality,
            api_url=self._api_url,
        )
