"""Client object bundling an aiohttp session with a token source.

The token source is awaited on every call, so a caller that refreshes its
OAuth token (for example Home Assistant) always hands the library a valid
access token. The module-level functions stay available and unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aiohttp

from .cameras import Camera, get_latest_event_image, list_cameras
from .cloud import (
    CLOUD_API,
    CameraLightSetResult,
    CloudPutResult,
    cloud_set_camera_light,
    cloud_set_notifications,
    cloud_set_privacy_mode,
)
from .live import LiveConnection, open_local_live_connection
from .settings import (
    AudioSettings,
    LightGroup,
    LightingSwitch,
    LightSwitchGroup,
    SettingWriteResult,
    get_audio_settings,
    get_lighting_switch,
    set_audio_settings,
    set_light_switch_enabled,
    set_lighting_switch,
    set_motion_enabled,
)
from .states import (
    CameraAccountStates,
    LightState,
    MotionSettings,
    get_camera_account_states,
    get_light_state,
    get_motion_settings,
)

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
        self._write_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _write_lock(self, camera_id: str, endpoint: str) -> asyncio.Lock:
        """Lock that serializes the read-modify-write calls of one endpoint."""
        return self._write_locks.setdefault((camera_id, endpoint), asyncio.Lock())

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

    async def set_notifications(self, camera_id: str, enabled: bool) -> CloudPutResult:
        """Switch the push notifications of a camera on or off."""
        return await cloud_set_notifications(
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

    async def get_motion_settings(self, camera_id: str) -> MotionSettings:
        """Return whether motion detection is on and its sensitivity."""
        return await get_motion_settings(
            self._session,
            await self._token_source(),
            camera_id,
            api_url=self._api_url,
        )

    async def set_motion_enabled(
        self, camera_id: str, enabled: bool
    ) -> SettingWriteResult:
        """Switch motion detection on or off; the sensitivity is kept."""
        async with self._write_lock(camera_id, "motion"):
            return await set_motion_enabled(
                self._session,
                await self._token_source(),
                camera_id,
                enabled,
                api_url=self._api_url,
            )

    async def get_audio_settings(self, camera_id: str) -> AudioSettings:
        """Return the microphone and speaker settings of a camera."""
        return await get_audio_settings(
            self._session,
            await self._token_source(),
            camera_id,
            api_url=self._api_url,
        )

    async def set_audio_settings(
        self,
        camera_id: str,
        *,
        audio_enabled: bool | None = None,
        microphone_level: int | None = None,
        speaker_level: int | None = None,
    ) -> SettingWriteResult:
        """Change the given audio fields (levels 0 to 100); the rest is kept."""
        async with self._write_lock(camera_id, "audio"):
            return await set_audio_settings(
                self._session,
                await self._token_source(),
                camera_id,
                audio_enabled=audio_enabled,
                microphone_level=microphone_level,
                speaker_level=speaker_level,
                api_url=self._api_url,
            )

    async def get_light_state(self, camera_id: str) -> LightState:
        """Return the front light and wall washer state of an Eyes Outdoor."""
        return await get_light_state(
            self._session,
            await self._token_source(),
            camera_id,
            api_url=self._api_url,
        )

    async def set_camera_light(
        self,
        camera_id: str,
        on: bool,
        *,
        gen2: bool,
        last_front_intensity: float = 1.0,
    ) -> CameraLightSetResult:
        """Switch all lights of a camera on or off."""
        async with self._write_lock(camera_id, "lighting/switch"):
            return await cloud_set_camera_light(
                self._session,
                await self._token_source(),
                camera_id,
                on,
                gen2=gen2,
                last_front_intensity=last_front_intensity,
            )

    async def get_lighting_switch(self, camera_id: str) -> LightingSwitch:
        """Return the front, top and bottom LED groups of an Eyes Outdoor II."""
        return await get_lighting_switch(
            self._session,
            await self._token_source(),
            camera_id,
            api_url=self._api_url,
        )

    async def set_lighting_switch(
        self,
        camera_id: str,
        *,
        front: LightGroup | None = None,
        top_led: LightGroup | None = None,
        bottom_led: LightGroup | None = None,
    ) -> SettingWriteResult:
        """Replace the given LED groups of an Eyes Outdoor II; the rest is kept."""
        async with self._write_lock(camera_id, "lighting/switch"):
            return await set_lighting_switch(
                self._session,
                await self._token_source(),
                camera_id,
                front=front,
                top_led=top_led,
                bottom_led=bottom_led,
                api_url=self._api_url,
            )

    async def set_light_switch_enabled(
        self, camera_id: str, group: LightSwitchGroup, enabled: bool
    ) -> SettingWriteResult:
        """Toggle the front light or the top and bottom LEDs of an Eyes Outdoor II."""
        return await set_light_switch_enabled(
            self._session,
            await self._token_source(),
            camera_id,
            group,
            enabled,
            api_url=self._api_url,
        )
