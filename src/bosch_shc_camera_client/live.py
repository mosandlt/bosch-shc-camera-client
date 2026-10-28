"""Open a live-stream session on the camera's local interface.

`PUT /v11/video_inputs/{id}/connection` with type LOCAL answers with the
camera's LAN address and short-lived Digest credentials. The stream is RTSPS
with a self-signed certificate, so a player reaches it through the local TCP
to TLS proxy of `tls_proxy.start_tls_proxy`; `LiveConnection.stream_url`
builds the plain RTSP address of that proxy.

Every successful call issues new Digest credentials, and on some camera
firmware that invalidates the running session. Open a session once per
stream and do not repeat the call as a keepalive. Sessions use session
management of up to 60 minutes.

The caller owns the aiohttp session and a valid token. A rejected token raises
`BoschCameraAuthError`, a network failure or HTTP error raises
`BoschCameraConnectionError`, and an answer without usable credentials or
address raises `BoschCameraInvalidResponseError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from .cameras import BoschCameraInvalidResponseError
from .cloud import CLOUD_API
from .settings import _integer, put_setting

CONNECTION_TYPE_LOCAL = "LOCAL"
SESSION_SECONDS = 3600
DEFAULT_BUFFERING_MS = 500
QUALITY_INSTANCES = (1, 2, 3, 4)
_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOST_PATTERN = re.compile(rf"{_LABEL}(\.{_LABEL})*")
_MAX_PORT = 65535
_MAX_PORT_DIGITS = 5


@dataclass(frozen=True, slots=True)
class LiveConnection:
    """A live-stream session opened on the camera."""

    connection_type: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    snapshot_url: str
    max_session_seconds: int
    buffering_ms: int

    def stream_url(
        self,
        proxy_port: int,
        *,
        quality_instance: int = 1,
        audio: bool = True,
        proxy_host: str = "127.0.0.1",
    ) -> str:
        """Return the RTSP address of the local TLS proxy, credentials included."""
        if (
            not isinstance(quality_instance, int)
            or isinstance(quality_instance, bool)
            or quality_instance not in QUALITY_INSTANCES
        ):
            raise ValueError(f"quality_instance must be one of {QUALITY_INSTANCES}")
        host = f"[{proxy_host}]" if ":" in proxy_host else proxy_host
        return (
            f"rtsp://{quote(self.username, safe='')}:{quote(self.password, safe='')}"
            f"@{host}:{proxy_port}/rtsp_tunnel?inst={quality_instance}"
            f"&enableaudio={int(audio)}&fmtp=1"
            f"&maxSessionDuration={self.max_session_seconds}"
        )


def _address(response: dict[str, Any]) -> tuple[str, int]:
    urls = response.get("urls")
    address = urls[0] if isinstance(urls, list) and urls else None
    host, _, port_text = (
        address.partition(":") if isinstance(address, str) else ("",) * 3
    )
    port = (
        int(port_text)
        if port_text.isascii()
        and port_text.isdigit()
        and len(port_text) <= _MAX_PORT_DIGITS
        else 0
    )
    if not 0 < port <= _MAX_PORT or not _HOST_PATTERN.fullmatch(host):
        raise BoschCameraInvalidResponseError("Live connection has no camera address")
    return host, port


async def open_local_live_connection(
    session: aiohttp.ClientSession,
    token: str,
    camera_id: str,
    *,
    high_quality: bool = True,
    api_url: str = CLOUD_API,
) -> LiveConnection:
    """Open a LOCAL session for `camera_id`; this rotates the Digest credentials."""
    result = await put_setting(
        session,
        token,
        camera_id,
        "connection",
        {"type": CONNECTION_TYPE_LOCAL, "highQualityVideo": high_quality},
        api_url=api_url,
    )
    response = result.response
    if response is None:
        raise BoschCameraInvalidResponseError("Live connection answered without a body")
    username = response.get("user")
    password = response.get("password")
    if not (
        isinstance(username, str)
        and username
        and isinstance(password, str)
        and password
    ):
        raise BoschCameraInvalidResponseError("Live connection has no credentials")
    host, port = _address(response)
    buffering = _integer(response.get("bufferingTime"))
    return LiveConnection(
        connection_type=CONNECTION_TYPE_LOCAL,
        host=host,
        port=port,
        username=username,
        password=password,
        snapshot_url=f"https://{host}:{port}/snap.jpg?JpegSize=1206",
        max_session_seconds=SESSION_SECONDS,
        buffering_ms=(
            DEFAULT_BUFFERING_MS if buffering is None or buffering < 0 else buffering
        ),
    )
