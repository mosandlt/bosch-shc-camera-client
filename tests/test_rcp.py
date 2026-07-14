"""Tests for rcp.py — RCP protocol helpers and binary payload parsers.

rcp.py provides:
  get_cached_rcp_session       — 5-min TTL session cache with eviction on expiry
  rcp_session                  — cloud-proxy RCP handshake (2-step session open)
  rcp_read / rcp_local_read    — RCP command read via cloud proxy / direct LAN
  rcp_local_write               — RCP command write via direct LAN
  rcp_local_read_privacy       — 0x0d00 byte[1] decode -> bool
  rcp_local_write_privacy      — bool -> 0x0d00 4-byte payload
  rcp_local_write_front_light  — Gen2 LAN-fallback front-light brightness writer
  _parse_alarm_catalog         — UTF-16-BE blob -> typed alarm dicts
  _parse_motion_zones          — 5 x 28B struct -> zone dicts
  _parse_motion_coords         — 8B per zone, 0-10000 -> 0-100% coords
  _parse_network_services      — null-separated ASCII -> service list
  _parse_iva_catalog           — 65 x 6B TLV -> module dicts
  _parse_tls_cert              — DER cert bytes -> info dict (cryptography, with
                                 raw_hex fallback if unavailable/unparsable)
  _is_xml_envelope              — detects cloud-proxy XML-leak responses
  _drop_cached_session (inner) — invoked by rcp_read on 401/403/0x0c0d

This is a byte-for-byte port of the RCP protocol layer out of the Home
Assistant integration (custom_components/bosch_shc_camera/rcp.py). The
signatures differ from the HA original: instead of taking the HA core
object and resolving a session/ssl-context internally via HA helpers, every
function
here takes its `aiohttp.ClientSession` / `ssl.SSLContext` directly from the
caller — this module owns no HA-specific process-wide state. Tests below
build `MagicMock(spec=aiohttp.ClientSession)` / plain `ssl.SSLContext`
doubles and pass them straight in.

`async_update_rcp_data` (the coordinator-facing poll-all-fields orchestrator)
was NOT ported to this library — it is HA-coordinator specific and stays in
the integration repo. Everything below is pure protocol/parsing logic.

All pure-function / no-network tests. Async helpers that hit aiohttp are
covered via AsyncMock stubs.
"""

from __future__ import annotations

import ssl
import struct
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from bosch_shc_camera_client import rcp as rcp_module
from bosch_shc_camera_client.rcp import (
    RcpCameraData,
    _is_xml_envelope,
    _parse_alarm_catalog,
    _parse_iva_catalog,
    _parse_motion_coords,
    _parse_motion_zones,
    _parse_network_services,
    _parse_tls_cert,
    fetch_rcp_camera_data,
    get_cached_rcp_session,
    rcp_local_read,
    rcp_local_read_privacy,
    rcp_local_write,
    rcp_local_write_front_light,
    rcp_local_write_privacy,
    rcp_read,
    rcp_session,
)

MODULE = "bosch_shc_camera_client.rcp"
CAM_IP = "192.0.2.149"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"
RCP_BASE = f"https://{PROXY_HOST}/{PROXY_HASH}/rcp.xml"


def _fake_ssl_context() -> ssl.SSLContext:
    """A cheap, real SSLContext test double — RCP functions never actually
    open a socket with it in these tests (ClientSession/TCPConnector
    construction itself is patched), so a plain default context is enough."""
    return ssl.create_default_context()


def _make_session() -> MagicMock:
    """Return a MagicMock aiohttp.ClientSession double."""
    return MagicMock(spec=aiohttp.ClientSession)


def _fake_cryptography_modules(loader: MagicMock) -> dict[str, types.ModuleType]:
    """Build fake `cryptography` / `cryptography.x509` module objects.

    `cryptography` is an optional, lazily-imported dependency of
    `_parse_tls_cert` (not in [project.dependencies], guarded by
    try/except ImportError) — CI does not install it. `unittest.mock.patch`
    on a dotted path like "cryptography.x509.load_der_x509_certificate"
    requires the real module to already be importable to resolve the
    target, which would make these tests depend on an environment accident
    rather than being self-contained. Injecting fake modules directly into
    `sys.modules` (patched in for the duration of the `with` block) lets the
    `from cryptography import x509` import inside `_parse_tls_cert` succeed
    without the real package installed anywhere.
    """
    fake_x509 = types.ModuleType("cryptography.x509")
    fake_x509.load_der_x509_certificate = loader  # type: ignore[attr-defined]
    fake_cryptography = types.ModuleType("cryptography")
    fake_cryptography.x509 = fake_x509  # type: ignore[attr-defined]
    return {"cryptography": fake_cryptography, "cryptography.x509": fake_x509}


def _make_ha_resp(status: int, raw: bytes = b"") -> MagicMock:
    """Return a MagicMock mimicking an aiohttp.ClientResponse inside `async with`."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=raw)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_get_session(*responses: MagicMock) -> MagicMock:
    """Return a mock aiohttp session whose `.get()` yields responses in order."""
    session = _make_session()
    session.get = MagicMock(side_effect=list(responses))
    return session


def _mock_resp(status: int, text: str = "", body: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_client_session_double(*get_responses: MagicMock) -> MagicMock:
    """A double for the aiohttp.ClientSession *constructed inside* rcp_session
    (used as `async with aiohttp.ClientSession(...) as session:`)."""
    session = _make_session()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(side_effect=list(get_responses))
    return session


class TestGetCachedRcpSession:
    """Pin the 5-minute TTL cache contract for get_cached_rcp_session."""

    @pytest.mark.asyncio
    async def test_cache_miss_opens_new_session(self) -> None:
        """Empty cache -> rcp_session called, result stored with TTL."""
        cache: rcp_module.RcpSessionCache = {}
        with patch(
            f"{MODULE}.rcp_session",
            new=AsyncMock(return_value="session-ABC"),
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-ABC", (
            "Cache miss must return the newly opened session"
        )
        assert "hash123" in cache, "New session must be stored in the cache"
        sid, expires = cache["hash123"]
        assert sid == "session-ABC"
        assert expires > time.monotonic(), "Expiry must be in the future"
        assert expires < time.monotonic() + 305, "TTL must be <= 5 minutes"

    @pytest.mark.asyncio
    async def test_cache_hit_reuses_session(self) -> None:
        """Valid unexpired entry -> rcp_session NOT called."""
        future_expiry = time.monotonic() + 200.0
        cache: rcp_module.RcpSessionCache = {
            "hash123": ("session-CACHED", future_expiry)
        }

        with patch(
            f"{MODULE}.rcp_session",
            new=AsyncMock(return_value="session-NEW"),
        ) as mock_session:
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-CACHED", "Unexpired entry must be returned from cache"
        assert not mock_session.called, "rcp_session must NOT be called on a cache hit"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_without_new_session(self) -> None:
        """If the cache has a live entry, no network call should be made."""
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("cached-sid", time.monotonic() + 300)
        }

        with patch(
            f"{MODULE}.rcp_session",
            new_callable=AsyncMock,
        ) as mock_open:
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, PROXY_HOST, PROXY_HASH
            )

        assert result == "cached-sid"
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted_and_refreshed(self) -> None:
        """Expired entry -> removed, new session opened."""
        past_expiry = time.monotonic() - 1.0  # already expired
        cache: rcp_module.RcpSessionCache = {"hash123": ("session-OLD", past_expiry)}

        with patch(
            f"{MODULE}.rcp_session",
            new=AsyncMock(return_value="session-FRESH"),
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-FRESH", "Expired session must be replaced"
        sid, _ = cache["hash123"]
        assert sid == "session-FRESH", "Cache must be updated with the new session"

    @pytest.mark.asyncio
    async def test_expired_entry_opens_new_session(self) -> None:
        """An entry past its TTL must be evicted and a new handshake opened."""
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("old-sid", time.monotonic() - 1)
        }

        with patch(
            f"{MODULE}.rcp_session",
            new_callable=AsyncMock,
            return_value="fresh-sid",
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, PROXY_HOST, PROXY_HASH
            )

        assert result == "fresh-sid"
        assert PROXY_HASH in cache
        assert cache[PROXY_HASH][0] == "fresh-sid"

    @pytest.mark.asyncio
    async def test_fresh_session_ttl_is_300s(self) -> None:
        """New sessions must be cached with exactly 300 s TTL."""
        cache: rcp_module.RcpSessionCache = {}
        before = time.monotonic()

        with patch(
            f"{MODULE}.rcp_session",
            new_callable=AsyncMock,
            return_value="new-sid",
        ):
            await get_cached_rcp_session(
                _fake_ssl_context(), cache, PROXY_HOST, PROXY_HASH
            )

        after = time.monotonic()
        _, expires_at = cache[PROXY_HASH]
        ttl = expires_at - before
        assert 295 <= ttl <= 305, (
            f"Session TTL should be ~300 s, got {ttl:.1f} s — "
            "too short causes excessive re-handshakes, too long risks stale sessions"
        )
        assert after >= before

    @pytest.mark.asyncio
    async def test_failed_session_not_cached(self) -> None:
        """rcp_session returning None -> cache must NOT store a None entry."""
        cache: rcp_module.RcpSessionCache = {}
        with patch(
            f"{MODULE}.rcp_session",
            new=AsyncMock(return_value=None),
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, "proxy-10:42090", "hash123"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_failed_session_not_cached_shared_constants(self) -> None:
        """Same contract as above, exercised via the shared PROXY_HOST/PROXY_HASH
        module constants instead of inline literals — kept as a distinct test
        since it pins the shared-fixture path used by the rest of this file."""
        cache: rcp_module.RcpSessionCache = {}

        with patch(
            f"{MODULE}.rcp_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, PROXY_HOST, PROXY_HASH
            )

        assert result is None
        assert PROXY_HASH not in cache, (
            "Failed sessions must not be cached — next call must retry the handshake"
        )


class TestGetCachedRcpSessionConcurrency:
    """Regression: two concurrent openers for the same proxy_hash raced
    Bosch's cloud RCP proxy, which only tolerates one live session per
    proxy_hash — the loser got sessionid 0x00000000 ("proxy rejected").
    Passing a shared `session_locks` dict serializes same-proxy_hash opens
    so the second caller awaits the first's in-flight open and reads the
    cache instead of firing its own handshake.
    """

    @pytest.mark.asyncio
    async def test_concurrent_callers_same_hash_open_session_once(self) -> None:
        import asyncio

        cache: rcp_module.RcpSessionCache = {}
        locks: rcp_module.RcpSessionLocks = {}
        open_calls = 0

        async def _fake_rcp_session(
            ssl_context: ssl.SSLContext,
            session_cache: rcp_module.RcpSessionCache,
            proxy_host: str,
            proxy_hash: str,
        ) -> str:
            nonlocal open_calls
            open_calls += 1
            # Yield control so a real race would interleave here if unlocked.
            await asyncio.sleep(0.01)
            return "session-SHARED"

        with patch(f"{MODULE}.rcp_session", new=_fake_rcp_session):
            results = await asyncio.gather(
                get_cached_rcp_session(
                    _fake_ssl_context(), cache, "proxy-10:42090", "hash123", locks
                ),
                get_cached_rcp_session(
                    _fake_ssl_context(), cache, "proxy-10:42090", "hash123", locks
                ),
            )

        assert open_calls == 1, (
            "Second caller must await the first's in-flight open (via the "
            "shared lock) instead of firing its own concurrent handshake"
        )
        assert results == ["session-SHARED", "session-SHARED"]

    @pytest.mark.asyncio
    async def test_concurrent_callers_different_hash_not_serialized(self) -> None:
        """Locks are per-proxy_hash — different cameras must not block each other."""
        import asyncio

        cache: rcp_module.RcpSessionCache = {}
        locks: rcp_module.RcpSessionLocks = {}
        open_calls = 0

        async def _fake_rcp_session(
            ssl_context: ssl.SSLContext,
            session_cache: rcp_module.RcpSessionCache,
            proxy_host: str,
            proxy_hash: str,
        ) -> str:
            nonlocal open_calls
            open_calls += 1
            await asyncio.sleep(0.01)
            return f"session-{proxy_hash}"

        with patch(f"{MODULE}.rcp_session", new=_fake_rcp_session):
            results = await asyncio.gather(
                get_cached_rcp_session(
                    _fake_ssl_context(), cache, "proxy-10:42090", "hashA", locks
                ),
                get_cached_rcp_session(
                    _fake_ssl_context(), cache, "proxy-11:42090", "hashB", locks
                ),
            )

        assert open_calls == 2, "Distinct proxy_hash values must open independently"
        assert results == ["session-hashA", "session-hashB"]

    @pytest.mark.asyncio
    async def test_no_locks_arg_preserves_prior_unlocked_behavior(self) -> None:
        """Omitting session_locks (existing call sites) must still work —
        backward compatible, no forced serialization."""
        cache: rcp_module.RcpSessionCache = {}
        with patch(
            f"{MODULE}.rcp_session",
            new=AsyncMock(return_value="session-X"),
        ):
            result = await get_cached_rcp_session(
                _fake_ssl_context(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-X"
        assert cache["hash123"][0] == "session-X"


class TestRcpLocalPrivacy:
    """Pin the 4-byte payload contract for 0x0d00 privacy read/write."""

    @pytest.mark.asyncio
    async def test_read_privacy_on_returns_true(self) -> None:
        """byte[1]=1 -> privacy ON -> True."""
        payload = b"\x00\x01\x00\x00"  # byte[1]=1
        with patch(
            f"{MODULE}.rcp_local_read",
            new=AsyncMock(return_value=payload),
        ):
            result = await rcp_local_read_privacy(_make_session(), "10.0.0.1")

        assert result is True, "byte[1]=1 must decode to privacy ON"

    @pytest.mark.asyncio
    async def test_read_privacy_off_returns_false(self) -> None:
        """byte[1]=0 -> privacy OFF -> False."""
        payload = b"\x00\x00\x00\x00"  # byte[1]=0
        with patch(
            f"{MODULE}.rcp_local_read",
            new=AsyncMock(return_value=payload),
        ):
            result = await rcp_local_read_privacy(_make_session(), "10.0.0.1")

        assert result is False, "byte[1]=0 must decode to privacy OFF"

    @pytest.mark.asyncio
    async def test_read_privacy_none_when_rcp_fails(self) -> None:
        """rcp_local_read returning None -> None (camera offline)."""
        with patch(
            f"{MODULE}.rcp_local_read",
            new=AsyncMock(return_value=None),
        ):
            result = await rcp_local_read_privacy(_make_session(), "10.0.0.1")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_privacy_none_when_payload_too_short(self) -> None:
        """Payload shorter than 2 bytes -> None (can't read byte[1])."""
        with patch(
            f"{MODULE}.rcp_local_read",
            new=AsyncMock(return_value=b"\x01"),  # only 1 byte
        ):
            result = await rcp_local_read_privacy(_make_session(), "10.0.0.1")

        assert result is None

    @pytest.mark.asyncio
    async def test_write_privacy_on_sends_correct_payload(self) -> None:
        """enabled=True -> payload '00010000' (byte[1]=1)."""
        captured: dict[str, str] = {}

        async def _mock_write(
            session: aiohttp.ClientSession,
            cam_ip: str,
            command: str,
            payload_hex: str,
            type_: str = "P_OCTET",
            num: int = 0,
            *,
            user: str | None = None,
            password: str | None = None,
        ) -> bool:
            captured["payload"] = payload_hex
            captured["command"] = command
            return True

        with patch(f"{MODULE}.rcp_local_write", _mock_write):
            result = await rcp_local_write_privacy(_make_session(), "10.0.0.1", True)

        assert result is True
        assert captured["payload"] == "00010000", (
            "Privacy ON must send payload '00010000' (byte[1]=1)"
        )
        assert captured["command"] == "0x0d00"

    @pytest.mark.asyncio
    async def test_write_privacy_off_sends_correct_payload(self) -> None:
        """enabled=False -> payload '00000000' (all zero)."""
        captured: dict[str, str] = {}

        async def _mock_write(
            session: aiohttp.ClientSession,
            cam_ip: str,
            command: str,
            payload_hex: str,
            type_: str = "P_OCTET",
            num: int = 0,
            *,
            user: str | None = None,
            password: str | None = None,
        ) -> bool:
            captured["payload"] = payload_hex
            return True

        with patch(f"{MODULE}.rcp_local_write", _mock_write):
            await rcp_local_write_privacy(_make_session(), "10.0.0.1", False)

        assert captured["payload"] == "00000000", (
            "Privacy OFF must send all-zero payload"
        )


class TestRcpReadSessionInvalidation:
    """Pin the session cache invalidation paths inside rcp_read."""

    @pytest.mark.asyncio
    async def test_http_401_invalidates_cache(self) -> None:
        """HTTP 401 response -> cached session for the proxy_hash removed."""
        proxy_hash = "abc123def"
        cache: rcp_module.RcpSessionCache = {
            proxy_hash: ("session-OLD", time.monotonic() + 300)
        }
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        session = _make_get_session(_make_ha_resp(401))

        result = await rcp_read(
            session,
            rcp_base,
            "0x0c22",
            "session-OLD",
            session_cache=cache,
        )

        assert result is None
        assert proxy_hash not in cache, (
            "HTTP 401 must evict the cached session — dead sessions must not be replayed"
        )

    @pytest.mark.asyncio
    async def test_http_403_invalidates_cache(self) -> None:
        """HTTP 403 response -> cached session evicted."""
        proxy_hash = "abc123def"
        cache: rcp_module.RcpSessionCache = {
            proxy_hash: ("session-OLD", time.monotonic() + 300)
        }
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        session = _make_get_session(_make_ha_resp(403))

        await rcp_read(
            session,
            rcp_base,
            "0x0c22",
            "session-OLD",
            session_cache=cache,
        )

        assert proxy_hash not in cache, "HTTP 403 must evict the cached session"

    @pytest.mark.asyncio
    async def test_rcp_err_0x0c0d_invalidates_cache(self) -> None:
        """RCP <err>0x0c0d</err> (session closed by server) -> cache evicted."""
        proxy_hash = "abc123def"
        cache: rcp_module.RcpSessionCache = {
            proxy_hash: ("session-OLD", time.monotonic() + 300)
        }
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        xml = b"<rcp><err>0x0c0d</err></rcp>"
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session,
            rcp_base,
            "0x0c22",
            "session-OLD",
            session_cache=cache,
        )

        assert result is None
        assert proxy_hash not in cache, (
            "RCP err 0x0c0d (session closed) must evict the cached session"
        )

    @pytest.mark.asyncio
    async def test_other_rcp_error_does_not_invalidate_cache(self) -> None:
        """A non-0x0c0d RCP error (e.g. 0x90 = not supported) must NOT evict the cache."""
        proxy_hash = "abc123def"
        cache: rcp_module.RcpSessionCache = {
            proxy_hash: ("session-VALID", time.monotonic() + 300)
        }
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        xml = b"<rcp><err>0x0090</err></rcp>"
        session = _make_get_session(_make_ha_resp(200, xml))

        await rcp_read(
            session,
            rcp_base,
            "0x0c22",
            "session-VALID",
            session_cache=cache,
        )

        assert proxy_hash in cache, (
            "Non-session-close errors must not evict the cache — "
            "the session is still valid, the command just isn't supported"
        )

    @pytest.mark.asyncio
    async def test_success_returns_payload_bytes(self) -> None:
        """200 + <payload> hex -> bytes."""
        xml = b"<rcp><payload>0102030405</payload></rcp>"
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session,
            "https://proxy-10:42090/hash/rcp.xml",
            "0x0c22",
            "session-ID",
        )

        assert result == b"\x01\x02\x03\x04\x05", (
            "<payload> hex must be decoded to bytes"
        )

    @pytest.mark.asyncio
    async def test_str_tag_also_accepted(self) -> None:
        """200 + <str> hex -> bytes (some FW versions use <str> instead of <payload>)."""
        xml = b"<rcp><str>AABBCC</str></rcp>"
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session,
            "https://proxy-10:42090/hash/rcp.xml",
            "0x0c22",
            "session-ID",
        )

        assert result == b"\xaa\xbb\xcc"


class TestRcpReadHttpErrors:
    """rcp_read maps HTTP status to return value + session-cache side effects."""

    @pytest.mark.asyncio
    async def test_http_200_payload_tag(self) -> None:
        payload_hex = "deadbeef"
        xml = f"<rcp><payload>{payload_hex}</payload></rcp>".encode()
        cache: rcp_module.RcpSessionCache = {}
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session, RCP_BASE, "0x0c22", "sid1", session_cache=cache
        )

        assert result == bytes.fromhex(payload_hex), (
            "rcp_read must decode hex from <payload> tag and return bytes"
        )

    @pytest.mark.asyncio
    async def test_http_200_str_tag(self) -> None:
        """Bosch firmwares sometimes use <str> instead of <payload>."""
        payload_hex = "0a0a"
        xml = f"<rcp><str>{payload_hex}</str></rcp>".encode()
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(session, RCP_BASE, "0x0c22", "sid1")

        assert result == bytes.fromhex(payload_hex), (
            "rcp_read must accept <str> tag (some Bosch FW versions use this)"
        )

    @pytest.mark.asyncio
    async def test_http_200_raw_binary_fallback(self) -> None:
        """Non-XML binary payload (e.g. JPEG thumbnail) must be returned as-is."""
        raw = b"\xff\xd8\xff\xe0jpeg-data"  # starts with 0xFF (not <)
        session = _make_get_session(_make_ha_resp(200, raw))

        result = await rcp_read(session, RCP_BASE, "0x0901", "sid1")

        assert result == raw

    @pytest.mark.asyncio
    async def test_http_200_xml_no_payload_returns_none(self) -> None:
        """XML response with no <payload>/<str> and no binary data -> None."""
        xml = b"<rcp><status>ok</status></rcp>"
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(session, RCP_BASE, "0x0c22", "sid1")

        assert result is None

    @pytest.mark.asyncio
    async def test_http_401_returns_none_and_drops_cache(self) -> None:
        """401 on RCP read means the session ID is dead — must drop it from cache."""
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("old-session-id", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(401, b""))

        result = await rcp_read(
            session,
            RCP_BASE,
            "0x0c22",
            "sid1",
            session_cache=cache,
        )

        assert result is None
        assert PROXY_HASH not in cache, (
            "rcp_read must evict the session from cache on HTTP 401 — "
            "otherwise the next call replays a dead session ID"
        )

    @pytest.mark.asyncio
    async def test_http_403_drops_cache(self) -> None:
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("old-session-id", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(403, b""))

        await rcp_read(session, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert PROXY_HASH not in cache, "HTTP 403 must also evict the session cache"

    @pytest.mark.asyncio
    async def test_http_non_200_no_cache_evict_for_other_status(self) -> None:
        """HTTP 500 (server error) — cache stays intact (session may still be valid)."""
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("my-session-id", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(500, b""))

        result = await rcp_read(
            session, RCP_BASE, "0x0c22", "sid1", session_cache=cache
        )

        assert result is None
        assert PROXY_HASH in cache, "HTTP 500 must NOT evict the session cache"

    @pytest.mark.asyncio
    async def test_error_0x0c0d_drops_cache(self) -> None:
        """RCP error 0x0c0d = 'session closed' -> must evict cache."""
        xml = b"<rcp><err>0x0c0d</err></rcp>"
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("live-session", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session, RCP_BASE, "0x0c22", "sid1", session_cache=cache
        )

        assert result is None
        assert PROXY_HASH not in cache, (
            "Error 0x0c0d means session was closed server-side — cache must be "
            "evicted so the next call re-opens the handshake"
        )

    @pytest.mark.asyncio
    async def test_error_0x90_does_not_drop_cache(self) -> None:
        """RCP error 0x90 = 'not supported' — session still alive; cache must stay."""
        xml = b"<rcp><err>0x90</err></rcp>"
        cache: rcp_module.RcpSessionCache = {
            PROXY_HASH: ("live-session", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(200, xml))

        result = await rcp_read(
            session, RCP_BASE, "0x0c22", "sid1", session_cache=cache
        )

        assert result is None
        assert PROXY_HASH in cache, (
            "Error 0x90 means the command is unsupported, not session-expired — "
            "cache must survive so subsequent supported commands reuse the session"
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        session = _make_session()
        session.get = MagicMock(side_effect=TimeoutError())

        result = await rcp_read(session, RCP_BASE, "0x0c22", "sid1")

        assert result is None


class TestRcpReadNumParam:
    """rcp_read: when num != 0, 'num' is included in the request params."""

    @pytest.mark.asyncio
    async def test_num_param_included_when_nonzero(self) -> None:
        """rcp_read with num=1 -> params dict contains 'num': '1'."""
        captured_params: dict[str, str] = {}

        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<rcp><payload>0102</payload></rcp>")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        def fake_get(
            url: str, params: dict[str, str] | None = None, **kwargs: object
        ) -> MagicMock:
            captured_params.update(params or {})
            return cm

        session = _make_session()
        session.get = fake_get  # type: ignore[method-assign]

        result = await rcp_read(
            session,
            "https://proxy/hash/rcp.xml",
            "0x0c22",
            "sess123",
            type_="T_WORD",
            num=1,
        )

        assert captured_params.get("num") == "1"
        assert result == bytes.fromhex("0102")

    @pytest.mark.asyncio
    async def test_num_param_absent_when_zero(self) -> None:
        """rcp_read with num=0 (default) -> params dict does NOT contain 'num'."""
        captured_params: dict[str, str] = {}

        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<rcp><payload>aabb</payload></rcp>")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        def fake_get(
            url: str, params: dict[str, str] | None = None, **kwargs: object
        ) -> MagicMock:
            captured_params.update(params or {})
            return cm

        session = _make_session()
        session.get = fake_get  # type: ignore[method-assign]

        await rcp_read(session, "https://proxy/hash/rcp.xml", "0x0d00", "sess123")

        assert "num" not in captured_params


class TestRcpReadDropSessionNone:
    """rcp_read: the internal _drop_cached_session with session_cache=None is a
    safe no-op."""

    @pytest.mark.asyncio
    async def test_401_with_none_cache_does_not_crash(self) -> None:
        """HTTP 401 + session_cache=None -> returns None without any AttributeError."""
        session = _make_get_session(_make_ha_resp(401, b""))

        result = await rcp_read(
            session,
            "https://proxy/hash/rcp.xml",
            "0x0d00",
            "sess123",
            session_cache=None,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_401_with_url_not_ending_in_rcp_xml_does_not_evict(self) -> None:
        """`rcp_base` that doesn't end in `.../<hash>/rcp.xml` -> the proxy_hash
        can't be derived, so `_drop_cached_session` is a no-op (nothing evicted,
        no crash)."""
        cache: rcp_module.RcpSessionCache = {
            "somehash": ("sid", time.monotonic() + 300)
        }
        session = _make_get_session(_make_ha_resp(401, b""))

        result = await rcp_read(
            session,
            "https://proxy/notrcp",
            "0x0d00",
            "sess123",
            session_cache=cache,
        )

        assert result is None
        assert "somehash" in cache, (
            "A malformed rcp_base must not evict an unrelated cache entry"
        )

    @pytest.mark.asyncio
    async def test_401_with_hash_not_in_cache_does_not_log_evict(self) -> None:
        """`rcp_base`'s proxy_hash is well-formed but simply isn't present in the
        cache (e.g. two rcp_read calls racing an already-evicted entry) ->
        `session_cache.pop(...)` returns None, the debug-log branch is skipped,
        no crash."""
        cache: rcp_module.RcpSessionCache = {}
        session = _make_get_session(_make_ha_resp(401, b""))

        result = await rcp_read(
            session,
            RCP_BASE,
            "0x0d00",
            "sess123",
            session_cache=cache,
        )

        assert result is None
        assert cache == {}


class TestRcpSession:
    """All branches of rcp_session (2-step cloud proxy session open)."""

    @pytest.mark.asyncio
    async def test_success_returns_session_id(self) -> None:
        """Happy path: step1 returns <sessionid>, step2 ACKs -> returns session_id."""
        step1 = _mock_resp(200, text="<sessionid>0x12345678</sessionid>")
        step2 = _mock_resp(200, text="<result>OK</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1, step2)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result == "0x12345678"
        connector_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step1_non200_returns_none(self) -> None:
        """HTTP 403 on step1 -> returns None."""
        step1 = _mock_resp(403)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_timeout_returns_none(self) -> None:
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_client_error_returns_none(self) -> None:
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn refused"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_sessionid_in_response_returns_none(self) -> None:
        """Step1 200 but no <sessionid> in body -> returns None."""
        step1 = _mock_resp(200, text="<result>ok</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_session_id_0x00000000_returns_none(self) -> None:
        """Proxy rejection indicated by sessionid=0x00000000 -> returns None."""
        step1 = _mock_resp(200, text="<sessionid>0x00000000</sessionid>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step2_timeout_still_returns_session_id(self) -> None:
        """ACK (step2) timeout is non-fatal — session_id already extracted, return it."""
        step1 = _mock_resp(200, text="<sessionid>0xABCDEF01</sessionid>")
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        step2_cm.__aexit__ = AsyncMock(return_value=None)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1, step2_cm)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        # step2 timeout is caught — should still return the session_id
        assert result == "0xABCDEF01"

    @pytest.mark.asyncio
    async def test_step2_client_error_still_returns_session_id(self) -> None:
        """ACK (step2) ClientError is also non-fatal — session_id still returned."""
        step1 = _mock_resp(200, text="<sessionid>0xABCDEF02</sessionid>")
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        step2_cm.__aexit__ = AsyncMock(return_value=None)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_client_session_double(step1, step2_cm)
        with (
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(_fake_ssl_context(), {}, PROXY_HOST, PROXY_HASH)
        assert result == "0xABCDEF02"


class TestRcpLocalRead:
    """All branches of rcp_local_read (direct LAN RCP GET)."""

    @pytest.mark.asyncio
    async def test_non200_returns_none(self) -> None:
        resp_cm = _mock_resp(401)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_with_payload_tag_returns_bytes(self) -> None:
        raw = b"<payload>deadbeef</payload>"
        resp_cm = _mock_resp(200, body=raw)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result == bytes.fromhex("deadbeef")

    @pytest.mark.asyncio
    async def test_200_with_str_tag_returns_bytes(self) -> None:
        raw = b"<str>cafebabe</str>"
        resp_cm = _mock_resp(200, body=raw)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result == bytes.fromhex("cafebabe")

    @pytest.mark.asyncio
    async def test_200_with_err_tag_returns_none(self) -> None:
        raw = b"<err>0x01</err>"
        resp_cm = _mock_resp(200, body=raw)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_raw_binary_fallback(self) -> None:
        """No <str>/<payload>/<err> tag and raw doesn't start with '<' -> return raw bytes."""
        raw = b"\x01\x02\x03\x04"
        resp_cm = _mock_resp(200, body=raw)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result == raw

    @pytest.mark.asyncio
    async def test_200_empty_body_returns_none(self) -> None:
        """No tags and empty raw body -> falls through to the final `return None`."""
        resp_cm = _mock_resp(200, body=b"")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session = _make_session()
        session.get = MagicMock(return_value=cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self) -> None:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn error"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session = _make_session()
        session.get = MagicMock(return_value=cm)
        result = await rcp_local_read(session, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_num_param_included(self) -> None:
        """When `num` > 0, params should include the 'num' key."""
        raw = b"\x01\x02"
        resp_cm = _mock_resp(200, body=raw)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        await rcp_local_read(session, CAM_IP, "0x0c22", num=3)
        _, call_kwargs = session.get.call_args
        assert "num" in call_kwargs.get("params", {})
        assert call_kwargs["params"]["num"] == "3"


class TestRcpLocalWrite:
    """All branches of rcp_local_write (direct LAN RCP WRITE, anonymous path)."""

    @pytest.mark.asyncio
    async def test_success_returns_true(self) -> None:
        resp_cm = _mock_resp(200, body=b"<result>OK</result>")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "deadbeef")
        assert result is True

    @pytest.mark.asyncio
    async def test_0x_prefix_added_when_missing(self) -> None:
        """Payloads without '0x' prefix get it added.

        Params are embedded in the URL query string (not the `params=` kwarg).
        """
        resp_cm = _mock_resp(200, body=b"ok")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        await rcp_local_write(session, CAM_IP, "0x0c22", "deadbeef")
        # First positional arg is the URL; payload="0xdeadbeef" lives in the query.
        call_args, _ = session.get.call_args
        url = call_args[0] if call_args else ""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        assert qs.get("payload", [""])[0].startswith("0x")

    @pytest.mark.asyncio
    async def test_0x_prefix_not_duplicated(self) -> None:
        """Payloads already prefixed with '0x' are passed through unchanged."""
        resp_cm = _mock_resp(200, body=b"ok")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        call_args, _ = session.get.call_args
        url = call_args[0] if call_args else ""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        assert qs.get("payload", [""])[0] == "0xdeadbeef"

    @pytest.mark.asyncio
    async def test_num_param_included_when_nonzero(self) -> None:
        resp_cm = _mock_resp(200, body=b"ok")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef", num=1)
        call_args, _ = session.get.call_args
        url = call_args[0] if call_args else ""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        assert qs.get("num", [""])[0] == "1"

    @pytest.mark.asyncio
    async def test_non200_returns_false(self) -> None:
        resp_cm = _mock_resp(403)
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_err_in_body_returns_false(self) -> None:
        """200 response with <err> tag -> returns False."""
        resp_cm = _mock_resp(200, body=b"<err>0x01</err>")
        session = _make_session()
        session.get = MagicMock(return_value=resp_cm)
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self) -> None:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session = _make_session()
        session.get = MagicMock(return_value=cm)
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_client_error_returns_false(self) -> None:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session = _make_session()
        session.get = MagicMock(return_value=cm)
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_value_error_returns_false(self) -> None:
        """A ValueError from URL building/encoding is also caught -> False."""
        session = _make_session()
        session.get = MagicMock(side_effect=ValueError("bad url"))
        result = await rcp_local_write(session, CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False


class _FakeRcpResp:
    def __init__(self, status: int = 200, body: bytes = b"<ok/>") -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _FakeRcpResp:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeRcpSession:
    """Captures the URL query so the test can assert num=1 was sent.

    `rcp_local_write` embeds params directly into the URL (HTTPS/anon path
    and Digest path both go through the query string here since no
    user/password is supplied). This fake parses the query string back into
    `last_params` so assertions keep working.
    """

    def __init__(self, resp: _FakeRcpResp) -> None:
        self._resp = resp
        self.last_params: dict[str, str] | None = None
        self.last_url: str | None = None

    def get(self, url: str, **_kwargs: object) -> _FakeRcpResp:
        from urllib.parse import parse_qs, urlparse

        self.last_url = url
        parsed = urlparse(url)
        self.last_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return self._resp


@pytest.mark.asyncio
class TestRcpLocalWriteFrontLight:
    """Coverage for the RCP front-light LOCAL writer (Gen2 LAN-fallback).

    `rcp_local_write_front_light` is the cloud-bypass path used during Bosch
    outages. It clamps brightness 0..100, encodes as 4-hex T_WORD and writes
    to RCP `0x0c22` with `num=1`. Other fallback tests mock it entirely, so
    this exercises the encoder + the `params["num"]` plumbing in
    `rcp_local_write` itself.
    """

    async def test_brightness_100_sends_t_word_with_num_1(self) -> None:
        """Pins `params["num"] = str(num)` + the front-light encoder."""
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        session = _FakeRcpSession(resp)
        ok = await rcp_local_write_front_light(session, "1.2.3.4", 100)  # type: ignore[arg-type]
        assert ok is True
        assert session.last_params is not None
        assert session.last_params["command"] == "0x0c22"
        assert session.last_params["type"] == "T_WORD"
        # 100 -> 0x0064, sent as 0x0064 (lower-case hex, 4 digits).
        assert session.last_params["payload"].lower() == "0x0064"
        # num=1 plumbed through — only fires when num > 0.
        assert session.last_params["num"] == "1"

    async def test_brightness_clamped_to_range(self) -> None:
        """Out-of-range brightness clamps to 0..100. 250 -> 100, -10 -> 0."""
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        session = _FakeRcpSession(resp)
        assert (
            await rcp_local_write_front_light(session, "1.2.3.4", 250)  # type: ignore[arg-type]
            is True
        )
        assert session.last_params is not None
        assert session.last_params["payload"].lower() == "0x0064"
        assert (
            await rcp_local_write_front_light(session, "1.2.3.4", -10)  # type: ignore[arg-type]
            is True
        )
        assert session.last_params["payload"].lower() == "0x0000"

    async def test_brightness_zero_encodes_0x0000(self) -> None:
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        session = _FakeRcpSession(resp)
        ok = await rcp_local_write_front_light(session, "1.2.3.4", 0)  # type: ignore[arg-type]
        assert ok is True
        assert session.last_params is not None
        assert session.last_params["payload"].lower() == "0x0000"

    async def test_returns_false_on_http_non_200(self) -> None:
        """Camera responding with HTTP 500 -> caller must see False so the
        SHC-cloud retry path runs."""
        resp = _FakeRcpResp(status=500, body=b"")
        session = _FakeRcpSession(resp)
        ok = await rcp_local_write_front_light(session, "1.2.3.4", 50)  # type: ignore[arg-type]
        assert ok is False

    async def test_returns_false_on_rcp_err_envelope(self) -> None:
        """`<err>` in response body -> write failed even if HTTP 200."""
        resp = _FakeRcpResp(status=200, body=b"<rcp><err>5</err></rcp>")
        session = _FakeRcpSession(resp)
        ok = await rcp_local_write_front_light(session, "1.2.3.4", 50)  # type: ignore[arg-type]
        assert ok is False

    async def test_with_credentials_uses_digest(self) -> None:
        """user+password supplied -> the Digest path is used, not the anonymous GET."""
        observed: dict[str, object] = {}

        class _FakeResp:
            status = 200

            async def read(self) -> bytes:
                return b"<rcp><payload>0064</payload></rcp>"

            async def __aenter__(self) -> _FakeResp:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        async def _fake_digest_request(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            user: str,
            password: str,
            **kwargs: object,
        ) -> _FakeResp:
            observed["url"] = url
            observed["user"] = user
            observed["password"] = password
            return _FakeResp()

        with patch(f"{MODULE}.async_digest_request", side_effect=_fake_digest_request):
            ok = await rcp_local_write_front_light(
                _make_session(), "1.2.3.4", 75, user="cbs-xxx", password="secret"
            )

        assert ok is True
        assert observed["user"] == "cbs-xxx"
        assert observed["password"] == "secret"
        assert str(observed["url"]).startswith("https://1.2.3.4/rcp.xml")


class TestIsXmlEnvelopeHelper:
    """_is_xml_envelope: shared detection of cloud-proxy XML-leak responses.

    Gen2 cloud proxy occasionally returns the outer RCP XML envelope as the
    P_OCTET payload bytes (Bosch-side limitation). The envelope starts with
    whitespace + '<rcp>...'. Short responses (T_WORD = 2 bytes) may contain
    only the leading whitespace and never reach '<' — those are XML too.
    """

    def test_none_is_not_xml(self) -> None:
        assert _is_xml_envelope(None) is False

    def test_empty_is_not_xml(self) -> None:
        assert _is_xml_envelope(b"") is False

    def test_plain_xml_detected(self) -> None:
        assert _is_xml_envelope(b"<rcp><command>0x0c81</command></rcp>") is True

    def test_whitespace_prefixed_xml_detected(self) -> None:
        assert (
            _is_xml_envelope(b"\n\n<rcp>\n\n\t<command>0x0c81</command></rcp>") is True
        )

    def test_pure_whitespace_detected(self) -> None:
        """T_WORD (2 bytes) truncates the XML envelope to just '\\n\\n'."""
        assert _is_xml_envelope(b"\n\n") is True
        assert _is_xml_envelope(b"\t ") is True
        assert _is_xml_envelope(b"\r\n") is True

    def test_binary_payload_not_xml(self) -> None:
        # Valid bitrate ladder uint32 big-endian: 1000, 2000 kbps
        assert _is_xml_envelope(struct.pack(">II", 1000, 2000)) is False
        # Valid LED dimmer 50% as T_WORD
        assert _is_xml_envelope(b"\x00\x32") is False
        # Single byte of zero
        assert _is_xml_envelope(b"\x00") is False

    def test_ascii_text_not_xml(self) -> None:
        # Product name (legitimate ASCII), not XML
        assert _is_xml_envelope(b"Bosch Smart Camera\x00") is False


class TestRcpLocalWriteTransport:
    """`rcp_local_write` must issue HTTPS (not HTTP) and use
    `async_digest_request` when user+password are supplied — cameras only
    listen on HTTPS port 443, so opening plain HTTP always fails with
    connection-refused."""

    @pytest.mark.asyncio
    async def test_url_is_https_when_creds_supplied(self) -> None:
        observed_url: list[str] = []

        class _FakeResp:
            status = 200

            async def read(self) -> bytes:
                return b"<rcp><payload>00</payload></rcp>"

            async def __aenter__(self) -> _FakeResp:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        async def _fake_digest_request(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            user: str,
            password: str,
            **_kwargs: object,
        ) -> _FakeResp:
            observed_url.append(url)
            return _FakeResp()

        with patch(f"{MODULE}.async_digest_request", side_effect=_fake_digest_request):
            ok = await rcp_local_write(
                _make_session(),
                "192.0.2.149",
                "0x0d00",
                "00010000",
                "P_OCTET",
                user="cbs-xxx",
                password="secret",
            )

        assert ok is True
        assert observed_url, "async_digest_request was not invoked"
        assert observed_url[0].startswith("https://"), (
            f"rcp_local_write opened {observed_url[0]} — must be HTTPS so the "
            "camera (port 443, no port 80 listener) accepts it."
        )
        assert "192.0.2.149/rcp.xml" in observed_url[0]

    @pytest.mark.asyncio
    async def test_no_digest_when_creds_missing(self) -> None:
        """Anonymous fallback path still issues HTTPS, just no auth."""
        observed_url: list[str] = []

        class _FakeResp:
            status = 200

            async def read(self) -> bytes:
                return b"<rcp><payload>00</payload></rcp>"

            async def __aenter__(self) -> _FakeResp:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _FakeSession:
            def get(self, url: str, **kwargs: object) -> _FakeResp:
                observed_url.append(url)
                return _FakeResp()

        ok = await rcp_local_write(
            _FakeSession(),  # type: ignore[arg-type]
            "192.0.2.149",
            "0x0d00",
            "00010000",
            "P_OCTET",
        )

        assert ok
        assert observed_url
        assert observed_url[0].startswith("https://"), (
            "Anonymous path emitted HTTP — must be HTTPS."
        )

    @pytest.mark.asyncio
    async def test_digest_non_200_returns_false(self) -> None:
        """Digest path: HTTP != 200 -> False."""

        class _FakeResp:
            status = 401

            async def read(self) -> bytes:
                return b""

            async def __aenter__(self) -> _FakeResp:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        async def _fake_digest_request(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            user: str,
            password: str,
            **_kwargs: object,
        ) -> _FakeResp:
            return _FakeResp()

        with patch(f"{MODULE}.async_digest_request", side_effect=_fake_digest_request):
            ok = await rcp_local_write(
                _make_session(),
                "192.0.2.149",
                "0x0d00",
                "00010000",
                user="cbs-xxx",
                password="secret",
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_digest_err_in_body_returns_false(self) -> None:
        """Digest path: 200 but <err> in body -> False."""

        class _FakeResp:
            status = 200

            async def read(self) -> bytes:
                return b"<err>0x01</err>"

            async def __aenter__(self) -> _FakeResp:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        async def _fake_digest_request(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            user: str,
            password: str,
            **_kwargs: object,
        ) -> _FakeResp:
            return _FakeResp()

        with patch(f"{MODULE}.async_digest_request", side_effect=_fake_digest_request):
            ok = await rcp_local_write(
                _make_session(),
                "192.0.2.149",
                "0x0d00",
                "00010000",
                user="cbs-xxx",
                password="secret",
            )
        assert ok is False


# ── Response parsers ────────────────────────────────────────────────────────


class TestParseAlarmCatalog:
    """Pin _parse_alarm_catalog's UTF-16-BE decoder and alarm-type classifier."""

    def _make_utf16be_blob(self, *names: str) -> bytes:
        """Encode names as UTF-16-BE with null separators."""
        parts = [n.encode("utf-16-be") for n in names]
        return b"\x00\x00".join(parts)

    def _names_to_raw(self, names: list[str]) -> bytes:
        """Encode a list of alarm names as UTF-16-BE, separated by null chars."""
        text = "\x00".join(names)
        return text.encode("utf-16-be")

    def test_virtual_alarm_type_classified(self) -> None:
        raw = self._make_utf16be_blob("Virtual Alarm 0", "Virtual Alarm 1")
        result = _parse_alarm_catalog(raw)

        virtual = [a for a in result if a.get("type") == "virtual"]
        assert len(virtual) >= 1, (
            "Names containing 'Virtual Alarm' must get type=virtual"
        )

    def test_flame_alarm_classified(self) -> None:
        raw = self._make_utf16be_blob("Flame Detector")
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "flame" in types, "Alarm names containing 'flame' must get type=flame"

    def test_motion_alarm_classified(self) -> None:
        raw = self._make_utf16be_blob("Motion Detector")
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "motion" in types

    def test_empty_blob_returns_empty_list(self) -> None:
        assert _parse_alarm_catalog(b"") == []

    def test_non_printable_only_part_skipped(self) -> None:
        """A part that decodes to nothing but non-printable control chars is
        dropped (`name` becomes empty after cleaning), while a valid sibling
        entry is still returned — exercises the `if name and len(name) > 1`
        false branch without breaking the loop."""
        raw = self._names_to_raw(["\x01", "Virtual Alarm 0"])
        result = _parse_alarm_catalog(raw)
        names = [a["name"] for a in result]
        assert "Virtual Alarm 0" in names
        assert all(n.strip("\x01") for n in names), (
            "Control-char-only entry must be dropped"
        )

    def test_garbage_bytes_does_not_raise(self) -> None:
        """Arbitrary bytes must not raise — fallback to empty or partial list."""
        try:
            result = _parse_alarm_catalog(b"\xff\xfe\x00\xab\xcd\xef")
            assert isinstance(result, list)
        except Exception as exc:
            pytest.fail(f"_parse_alarm_catalog must not raise on garbage input: {exc}")

    def test_result_dicts_have_required_keys(self) -> None:
        """Each result dict must have id, name, type."""
        raw = self._make_utf16be_blob("Virtual Alarm 0")
        result = _parse_alarm_catalog(raw)

        for alarm in result:
            assert "id" in alarm, f"Alarm dict missing 'id': {alarm}"
            assert "name" in alarm, f"Alarm dict missing 'name': {alarm}"
            assert "type" in alarm, f"Alarm dict missing 'type': {alarm}"

    def test_flame_type(self) -> None:
        """Name containing 'flame' -> type='flame'."""
        raw = self._names_to_raw(["Flame Detector"])
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "flame" in types

    def test_smoke_type(self) -> None:
        """Name containing 'smoke' -> type='smoke'."""
        raw = self._names_to_raw(["Smoke Detector"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "smoke" for a in result)

    def test_audio_type(self) -> None:
        """Name containing 'audio' -> type='audio'."""
        raw = self._names_to_raw(["Audio Detection"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "audio" for a in result)

    def test_signal_loss_type(self) -> None:
        """Name containing 'signal' -> type='signal'."""
        raw = self._names_to_raw(["Video Signal Loss"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "signal" for a in result)

    def test_storage_type(self) -> None:
        """Name containing 'storage' -> type='storage'."""
        raw = self._names_to_raw(["Storage Failure"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "storage" for a in result)

    def test_motion_type(self) -> None:
        """Name containing 'motion' -> type='motion'."""
        raw = self._names_to_raw(["Motion Detection"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "motion" for a in result)

    def test_reference_type(self) -> None:
        """Name containing 'reference' -> type='reference'."""
        raw = self._names_to_raw(["Reference Image Changed"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "reference" for a in result)

    def test_config_type(self) -> None:
        """Name containing 'config' -> type='config'."""
        raw = self._names_to_raw(["Config Changed"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "config" for a in result)

    def test_global_change_type(self) -> None:
        """Name containing 'global' -> type='global_change'."""
        raw = self._names_to_raw(["Global Change Alarm"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "global_change" for a in result)

    def test_task_type(self) -> None:
        """Name containing 'task' -> type='task'."""
        raw = self._names_to_raw(["Scheduled Task"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "task" for a in result)

    def test_unknown_type_fallback(self) -> None:
        """Name not matching any keyword -> type='unknown'."""
        raw = self._names_to_raw(["Unrecognized Alarm Type"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "unknown" for a in result)

    def test_virtual_alarm_type(self) -> None:
        """Name containing 'Virtual Alarm' -> type='virtual'."""
        raw = self._names_to_raw(["Virtual Alarm 0"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "virtual" for a in result)


class TestParseAlarmCatalogExcept:
    """Exception inside _parse_alarm_catalog's loop is caught and logged;
    the function still returns an empty list rather than raising."""

    def test_exception_in_loop_returns_empty(self) -> None:
        """A raw.decode() failure mid-parse -> except catches it, returns []."""

        class BadBytes(bytes):
            def decode(self, *args: object, **kwargs: object) -> str:
                raise RuntimeError("forced decode error")

        result = _parse_alarm_catalog(BadBytes(b"\x00\x01\x00\x02"))
        assert result == []

    def test_empty_raw_returns_empty_list(self) -> None:
        """Empty bytes -> no parts -> empty list (no exception path needed)."""
        result = _parse_alarm_catalog(b"")
        assert result == []


class TestParseMotionZones:
    """Pin _parse_motion_zones' 28-byte chunk layout."""

    def test_single_zone_parsed(self) -> None:
        """28 bytes -> exactly 1 zone."""
        raw = bytes(28)  # all zeros — valid 1-zone payload
        result = _parse_motion_zones(raw)

        assert len(result) == 1, "28 bytes must produce exactly 1 zone"
        assert result[0]["zone_id"] == 0
        assert len(result[0]["raw_hex"]) == 56  # 28 bytes x 2 hex chars

    def test_five_zones_max(self) -> None:
        """5 x 28 = 140 bytes -> 5 zones (cap at 5)."""
        raw = bytes(28 * 5)
        result = _parse_motion_zones(raw)
        assert len(result) == 5, "5 x 28-byte payload must yield exactly 5 zones"

    def test_extra_bytes_beyond_5_ignored(self) -> None:
        """More than 5 x 28 bytes -> still max 5 zones."""
        raw = bytes(28 * 8)  # 8 "zones" in the data
        result = _parse_motion_zones(raw)
        assert len(result) == 5, "Zone count must be capped at 5"

    def test_too_short_returns_empty(self) -> None:
        """Less than 28 bytes -> no zones."""
        assert _parse_motion_zones(b"\x00" * 10) == []

    def test_zone_ids_are_sequential(self) -> None:
        """zone_id values must be 0-based sequential indices."""
        raw = bytes(28 * 3)
        result = _parse_motion_zones(raw)
        ids = [z["zone_id"] for z in result]
        assert ids == [0, 1, 2], f"Zone IDs must be sequential, got {ids}"

    def test_parses_two_zones(self) -> None:
        """28*2 bytes -> 2 zones returned."""
        raw = bytes(28 * 2)
        zones = _parse_motion_zones(raw)
        assert len(zones) == 2
        assert "raw_hex" in zones[0]
        assert zones[0]["zone_id"] == 0
        assert zones[1]["zone_id"] == 1


class TestParseMotionCoords:
    """Pin _parse_motion_coords' 0-10000 -> 0-100% coordinate conversion."""

    def _make_coord_bytes(self, x1: int, y1: int, x2: int, y2: int) -> bytes:
        """Pack one zone's coordinates as big-endian uint16."""
        return struct.pack(">HHHH", x1, y1, x2, y2)

    def test_full_frame_zone_is_100_percent(self) -> None:
        """0-10000 range -> 100% coverage."""
        raw = self._make_coord_bytes(0, 0, 10000, 10000)
        result = _parse_motion_coords(raw)

        assert len(result) == 1
        assert result[0]["x1"] == 0.0
        assert result[0]["y1"] == 0.0
        assert result[0]["x2"] == 100.0
        assert result[0]["y2"] == 100.0

    def test_half_frame_zone(self) -> None:
        """5000 -> 50%."""
        raw = self._make_coord_bytes(0, 0, 5000, 5000)
        result = _parse_motion_coords(raw)

        assert result[0]["x2"] == 50.0, "5000/10000 must convert to 50.0%"
        assert result[0]["y2"] == 50.0

    def test_multiple_zones_parsed(self) -> None:
        """Two 8-byte entries -> two zone dicts."""
        raw = self._make_coord_bytes(0, 0, 5000, 5000) + self._make_coord_bytes(
            5000, 5000, 10000, 10000
        )
        result = _parse_motion_coords(raw)
        assert len(result) == 2

    def test_too_short_returns_empty(self) -> None:
        """Less than 8 bytes -> empty list."""
        assert _parse_motion_coords(b"\x00" * 4) == []

    def test_coords_rounded_to_one_decimal(self) -> None:
        """Conversion must round to 1 decimal place."""
        raw = self._make_coord_bytes(0, 0, 3333, 6667)
        result = _parse_motion_coords(raw)

        # 3333/100 = 33.3 (rounded to 1dp)
        assert result[0]["x2"] == round(3333 / 100, 1)
        assert result[0]["y2"] == round(6667 / 100, 1)


class TestParseMotionCoordsHappyPath:
    """Cover the _parse_motion_coords parser body with real Bosch coordinate
    layouts. The defensive `break` on a short mid-iteration chunk is
    documented as unreachable through this entry point — see
    TestDefensiveBreakBranches below, which pins that contract separately.
    """

    def test_single_full_zone(self) -> None:
        """One 8-byte zone -> one rect with percent conversion."""
        # x1=1000 y1=2000 x2=9000 y2=8000  (in 0-10000 units)
        raw = struct.pack(">HHHH", 1000, 2000, 9000, 8000)
        zones = _parse_motion_coords(raw)
        assert zones == [{"x1": 10.0, "y1": 20.0, "x2": 90.0, "y2": 80.0}]

    def test_multiple_zones(self) -> None:
        """Real Bosch capture: 4 zones x 8 B -> 4 rects."""
        raw = struct.pack(
            ">HHHH HHHH HHHH HHHH",
            0,
            0,
            10000,
            10000,  # full frame
            2500,
            2500,
            7500,
            7500,  # centre quadrant
            0,
            0,
            5000,
            5000,  # top-left
            5000,
            5000,
            10000,
            10000,  # bottom-right
        )
        zones = _parse_motion_coords(raw)
        assert len(zones) == 4
        assert zones[0] == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}
        assert zones[1] == {"x1": 25.0, "y1": 25.0, "x2": 75.0, "y2": 75.0}

    def test_empty_payload(self) -> None:
        """0 bytes -> 0 zones — n_zones is 0, loop never enters."""
        assert _parse_motion_coords(b"") == []

    def test_seven_bytes_truncated_below_one_zone(self) -> None:
        """7 bytes -> less than one full 8-B zone -> 0 zones.

        Pin: `n_zones = len(raw) // 8 = 0` so the loop body never runs.
        This guarantees no IndexError from unpacking partial chunks.
        """
        assert _parse_motion_coords(b"\x00" * 7) == []

    def test_trailing_garbage_bytes_ignored(self) -> None:
        """1 full zone (8 B) + 3 extra bytes -> still 1 zone (extras dropped)."""
        raw = struct.pack(">HHHH", 0, 0, 5000, 5000) + b"\xff\xff\xff"
        zones = _parse_motion_coords(raw)
        assert zones == [{"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 50.0}]


class TestParseNetworkServices:
    """Pin _parse_network_services' null-separated ASCII decoder."""

    def test_single_service_name(self) -> None:
        raw = b"RTSP\x00"
        result = _parse_network_services(raw)
        assert "RTSP" in result

    def test_multiple_services(self) -> None:
        raw = b"RTSP\x00HTTP\x00ONVIF\x00"
        result = _parse_network_services(raw)
        assert len(result) >= 2, "Multiple null-separated names must all be returned"
        assert any("RTSP" in s for s in result)
        assert any("HTTP" in s for s in result)

    def test_empty_blob_returns_empty(self) -> None:
        assert _parse_network_services(b"") == []

    def test_only_null_bytes_returns_empty(self) -> None:
        assert _parse_network_services(b"\x00\x00\x00") == []

    def test_single_char_entries_filtered(self) -> None:
        """1-char entries must be skipped (len > 1 requirement)."""
        raw = b"X\x00RTSP\x00Y\x00"
        result = _parse_network_services(raw)
        assert not any(len(s) <= 1 for s in result), (
            "Single-char entries must be filtered"
        )

    def test_garbage_bytes_does_not_raise(self) -> None:
        try:
            result = _parse_network_services(b"\xff\xfe\xab\xcd\x00RTSP\x00")
            assert isinstance(result, list)
        except Exception as exc:
            pytest.fail(f"Must not raise on garbage input: {exc}")

    def test_parses_service_names(self) -> None:
        """ASCII blob with null separators -> list of service strings."""
        raw = b"HTTP\x00RTSP\x00HTTPS\x00"
        services = _parse_network_services(raw)
        assert "HTTP" in services
        assert "RTSP" in services
        assert "HTTPS" in services

    def test_empty_parts_filtered(self) -> None:
        """Multiple consecutive nulls -> empty strings filtered out."""
        raw = b"\x00\x00HTTP\x00\x00"
        services = _parse_network_services(raw)
        assert "" not in services


class TestParseNetworkServicesExcept:
    """If an exception occurs inside the try block of _parse_network_services,
    it is caught and logged; the function returns an empty list."""

    def test_exception_in_decode_returns_empty(self) -> None:
        """Subclass bytes whose .decode() raises -> except branch fires."""

        class BadBytes(bytes):
            def decode(self, *args: object, **kwargs: object) -> str:
                raise RuntimeError("forced decode failure")

        result = _parse_network_services(BadBytes(b"HTTP\x00HTTPS"))
        assert result == []

    def test_normal_bytes_returns_services(self) -> None:
        """Sanity: normal ASCII payload parses correctly (no exception)."""
        raw = b"HTTP\x00HTTPS\x00RTSP\x00"
        result = _parse_network_services(raw)
        assert "HTTP" in result
        assert "RTSP" in result

    def test_single_char_names_filtered_out(self) -> None:
        """Names of length <= 1 are excluded (clean and len(clean) > 1 guard)."""
        # "A" alone is filtered; "BB" is kept
        raw = b"A\x00BB\x00"
        result = _parse_network_services(raw)
        assert "A" not in result
        assert "BB" in result


class TestParseIvaCatalog:
    """Pin _parse_iva_catalog's 6-byte TLV entry decoder."""

    def _make_entry(self, module_id: int, version: int, flags: int) -> bytes:
        return struct.pack(">HHH", module_id, version, flags)

    def test_active_module_flag(self) -> None:
        """flags bit 0 set -> active=True."""
        raw = self._make_entry(module_id=1, version=2, flags=0x01)
        result = _parse_iva_catalog(raw)

        assert len(result) == 1
        assert result[0]["active"] is True
        assert result[0]["module_id"] == 1
        assert result[0]["version"] == 2

    def test_inactive_module_flag(self) -> None:
        """flags bit 0 clear -> active=False."""
        raw = self._make_entry(module_id=3, version=1, flags=0x00)
        result = _parse_iva_catalog(raw)

        assert result[0]["active"] is False

    def test_zero_module_id_skipped(self) -> None:
        """module_id=0 is an empty slot — must be filtered out."""
        raw = self._make_entry(0, 0, 0)
        result = _parse_iva_catalog(raw)
        assert result == [], "module_id=0 must be treated as empty and skipped"

    def test_max_65_entries_cap(self) -> None:
        """More than 65 x 6 bytes -> capped at 65 entries."""
        # 70 entries, all with module_id=1 so none are filtered
        raw = self._make_entry(1, 1, 1) * 70
        result = _parse_iva_catalog(raw)
        assert len(result) <= 65, "IVA catalog must be capped at 65 entries"

    def test_too_short_returns_empty(self) -> None:
        assert _parse_iva_catalog(b"\x00" * 4) == []

    def test_multiple_modules_all_returned(self) -> None:
        """Three valid entries -> three dicts."""
        raw = (
            self._make_entry(1, 1, 0x01)
            + self._make_entry(2, 2, 0x00)
            + self._make_entry(3, 3, 0x01)
        )
        result = _parse_iva_catalog(raw)
        assert len(result) == 3
        ids = [m["module_id"] for m in result]
        assert ids == [1, 2, 3]

    def test_zero_module_id_skipped_amid_valid_entries(self) -> None:
        """Entry with module_id=0 -> not included in output, sibling entry kept.

        Distinct from test_zero_module_id_skipped above (single all-zero
        entry vs. a zero entry alongside a real one)."""
        # module_id=0, version=1, flags=1
        entry_zero = struct.pack(">HHH", 0, 1, 1)
        # module_id=5, version=2, flags=0
        entry_five = struct.pack(">HHH", 5, 2, 0)
        raw = entry_zero + entry_five

        modules = _parse_iva_catalog(raw)
        assert all(m["module_id"] != 0 for m in modules)
        assert any(m["module_id"] == 5 for m in modules)

    def test_active_flag_parsed(self) -> None:
        """flags & 0x01 == 1 -> active=True."""
        entry = struct.pack(">HHH", 7, 0x0100, 0x0001)
        modules = _parse_iva_catalog(entry)
        assert len(modules) == 1
        assert modules[0]["active"] is True
        assert modules[0]["module_id"] == 7


class TestParseIvaCatalogShortChunk:
    """If a chunk is shorter than entry_size (6 bytes), the parse loop breaks.

    In practice this guard fires when the raw payload length is not a
    multiple of 6 and the loop counter reaches the final partial chunk. We
    verify by building a payload that lies about its own length via `__len__`
    so the last iteration produces a short chunk.
    """

    def test_short_final_chunk_breaks_loop(self) -> None:
        """2 full entries + a lying __len__ -> only 2 entries parsed, no crash."""
        entry1 = struct.pack(">HHH", 0x0001, 0x0001, 0x0001)  # active
        entry2 = struct.pack(">HHH", 0x0002, 0x0002, 0x0000)  # inactive

        class PaddedBytes(bytes):
            """Bytes that lie about their length to force a short chunk."""

            _calls = 0

            def __len__(self) -> int:
                # Report 18 bytes (n=3) but actual slice at i=2 returns 1 byte
                return 18

        padded = PaddedBytes(entry1 + entry2 + b"\xaa")  # 13 real bytes
        result = _parse_iva_catalog(padded)
        # Loop runs for i=0,1 (full chunks), i=2 -> chunk=b"\xaa" (1 byte) -> break
        assert len(result) == 2
        assert result[0]["module_id"] == 1
        assert result[1]["module_id"] == 2

    def test_normal_payload_all_entries_parsed(self) -> None:
        """Sanity: clean 12-byte payload (2 entries) -> both returned correctly."""
        entry1 = struct.pack(">HHH", 0x0010, 0x0002, 0x0001)  # active flag set
        entry2 = struct.pack(">HHH", 0x0020, 0x0003, 0x0000)  # inactive
        result = _parse_iva_catalog(entry1 + entry2)
        assert len(result) == 2
        assert result[0]["active"] is True
        assert result[1]["active"] is False

    def test_zero_module_id_skipped(self) -> None:
        """module_id == 0 -> entry skipped."""
        zero_entry = struct.pack(">HHH", 0x0000, 0x0001, 0x0001)
        real_entry = struct.pack(">HHH", 0x0005, 0x0001, 0x0001)
        result = _parse_iva_catalog(zero_entry + real_entry)
        assert len(result) == 1
        assert result[0]["module_id"] == 5


class TestParseTlsCert:
    """ImportError on cryptography -> raw_hex fallback."""

    def test_no_cryptography_returns_raw_hex(self) -> None:
        """cryptography package absent -> info contains raw_hex, not subject."""
        fake_cert_bytes = b"\x30" + b"\xff" * 50
        with patch.dict(
            "sys.modules", {"cryptography": None, "cryptography.x509": None}
        ):
            info = _parse_tls_cert(fake_cert_bytes)

        assert "raw_size" in info
        # Either raw_hex is present (ImportError path) or other fields
        assert "raw_hex" in info or "subject" in info

    def test_parse_error_returns_raw_hex(self) -> None:
        """cryptography raises Exception on bad DER -> raw_hex fallback.

        Uses an injected fake loader (not real cryptography) so this pins
        the `except Exception` branch deterministically whether or not the
        optional `cryptography` package happens to be installed — CI does
        not install it (see `_fake_cryptography_modules`).
        """
        bad_bytes = b"\x30\x00" + b"\xcc" * 50

        loader = MagicMock(side_effect=ValueError("malformed DER"))
        with patch.dict("sys.modules", _fake_cryptography_modules(loader)):
            info = _parse_tls_cert(bad_bytes)
        assert "raw_size" in info
        assert info["raw_size"] == len(bad_bytes)
        assert "raw_hex" in info


class TestParseTlsCertImportError:
    """Pin the ImportError branch of rcp._parse_tls_cert.

    Patches `cryptography.x509.load_der_x509_certificate` directly to raise
    ImportError, distinct from TestParseTlsCert above which patches
    `sys.modules` — kept as a separate class since both approaches were
    written independently and each is a more robust regression pin for a
    slightly different failure mode.
    """

    def test_load_der_importerror_falls_back_to_raw_hex(self) -> None:
        """Patch the loader to raise ImportError -> info["raw_hex"] is set.

        Pin: when cryptography is broken/missing, the parser returns a
        usable dict so the diagnostics sensor can still display *something*
        rather than the entry being None.
        """
        # Build a fake DER prefix so the function actually enters the try block
        fake_cert = b"\x30\x82" + b"\xaa" * 60

        loader = MagicMock(side_effect=ImportError("cryptography missing"))
        with patch.dict("sys.modules", _fake_cryptography_modules(loader)):
            info = _parse_tls_cert(fake_cert)

        # ImportError branch sets raw_hex (truncated) and raw_size
        assert "raw_size" in info
        assert info["raw_size"] == len(fake_cert)
        assert "raw_hex" in info
        # subject etc. must NOT be set on ImportError path
        assert "subject" not in info
        assert "issuer" not in info

    def test_load_der_value_error_falls_back_to_raw_hex(self) -> None:
        """Generic Exception (not ImportError) in cryptography -> raw_hex
        fallback via the second `except Exception` branch.

        Pin: malformed DER bytes (cryptography raises ValueError) must
        not break the diagnostics path either. Uses an injected fake loader
        (see `_fake_cryptography_modules`) rather than relying on the real
        `cryptography` package rejecting garbage bytes, since CI does not
        install that optional dependency.
        """
        # 70 bytes of garbage — guaranteed not a valid DER cert
        bad_cert = b"\xff" * 70
        loader = MagicMock(side_effect=ValueError("malformed DER"))
        with patch.dict("sys.modules", _fake_cryptography_modules(loader)):
            info = _parse_tls_cert(bad_cert)

        assert "raw_size" in info
        assert info["raw_size"] == 70
        assert "raw_hex" in info


class TestParseTlsCertHappyPath:
    """When cryptography is available and the cert loads correctly, all 6
    info keys are populated."""

    def test_all_cert_fields_populated(self) -> None:
        """Mock cryptography.x509 fully -> info dict contains all 6 keys."""
        # Build fake DER blob (content doesn't matter; we mock the loader)
        fake_der = b"\x30\x82" + b"\xbb" * 80

        mock_cert = MagicMock()
        mock_cert.issuer.rfc4514_string.return_value = "CN=Bosch CA,O=Bosch"
        mock_cert.subject.rfc4514_string.return_value = "CN=cam-01,O=Bosch"
        mock_cert.serial_number = 0xDEADBEEF
        mock_cert.not_valid_before_utc.isoformat.return_value = (
            "2024-01-01T00:00:00+00:00"
        )
        mock_cert.not_valid_after_utc.isoformat.return_value = (
            "2026-01-01T00:00:00+00:00"
        )
        mock_cert.public_key.return_value.key_size = 2048
        mock_cert.signature_algorithm_oid.dotted_string = "1.2.840.113549.1.1.11"

        loader = MagicMock(return_value=mock_cert)
        with patch.dict("sys.modules", _fake_cryptography_modules(loader)):
            info = _parse_tls_cert(fake_der)

        assert info["issuer"] == "CN=Bosch CA,O=Bosch"
        assert info["subject"] == "CN=cam-01,O=Bosch"
        assert info["serial"] == "deadbeef"
        assert info["not_before"] == "2024-01-01T00:00:00+00:00"
        assert info["not_after"] == "2026-01-01T00:00:00+00:00"
        assert info["key_size"] == 2048
        assert info["signature_algorithm"] == "1.2.840.113549.1.1.11"
        assert info["raw_size"] == len(fake_der)
        assert "raw_hex" not in info  # happy path: no fallback


class TestDefensiveBreakBranches:
    """Pin the defensive `break` statements in _parse_motion_zones and
    _parse_motion_coords.

    Both are guarded by `n_zones = len(raw) // zone_size`, so reaching
    them through the function entry is impossible without buffer
    mutation mid-iteration. We use a `bytes` subclass that returns a
    short slice on the n-th access — simulates the contract a future
    refactor (e.g. streaming reader) might require.

    Without these pins, a refactor that drops the defensive `break`
    while introducing a partial-buffer reader would still pass all
    other tests, and the next firmware that returns a half-zone trailer
    would crash with a struct.error.
    """

    def test_motion_zones_break_on_short_slice(self) -> None:
        """Mid-iteration short slice -> the defensive `break` fires.

        We subclass `bytes` so `raw[start:end]` returns a 10-byte chunk
        on the second iteration even though `len(raw) // 28 == 2`.
        """

        class TruncatingBytes(bytes):
            """Returns a deliberately short slice on the 2nd __getitem__."""

            _calls = 0

            def __getitem__(self, key: object) -> object:
                cls = type(self)
                if isinstance(key, slice):
                    cls._calls += 1
                    # 1st call: full 28-byte chunk (normal zone)
                    # 2nd call: only 10 bytes -> triggers `if len(chunk) < 28: break`
                    if cls._calls == 2:
                        return bytes.__getitem__(self, key)[:10]
                return bytes.__getitem__(self, key)

        TruncatingBytes._calls = 0
        # 56 bytes -> n_zones = 2; second iteration will get a short slice
        raw = TruncatingBytes(b"\x00" * 56)
        zones = _parse_motion_zones(raw)
        # First zone parsed, second triggered `break` -> only 1 result
        assert len(zones) == 1
        assert zones[0]["zone_id"] == 0
        assert zones[0]["size"] == 28

    def test_motion_coords_break_on_short_slice(self) -> None:
        """Mid-iteration short slice -> the defensive `break` fires."""

        class TruncatingBytes(bytes):
            _calls = 0

            def __getitem__(self, key: object) -> object:
                cls = type(self)
                if isinstance(key, slice):
                    cls._calls += 1
                    # 1st call: full 8-byte chunk
                    # 2nd call: only 3 bytes -> triggers `if len(chunk) < 8: break`
                    if cls._calls == 2:
                        return bytes.__getitem__(self, key)[:3]
                return bytes.__getitem__(self, key)

        TruncatingBytes._calls = 0
        # 16 bytes -> n_zones = 2; second iteration short-slices
        raw = TruncatingBytes(
            struct.pack(">HHHH HHHH", 0, 0, 5000, 5000, 1000, 1000, 9000, 9000)
        )
        zones = _parse_motion_coords(raw)
        assert len(zones) == 1
        assert zones[0] == {"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 50.0}


# ── fetch_rcp_camera_data / RcpCameraData ────────────────────────────────────
#
# fetch_rcp_camera_data is a refactor of the HA integration's coordinator-side
# async_update_rcp_data (custom_components/bosch_shc_camera/rcp.py,
# tests/test_rcp.py in that repo) — same 12-command read sequence, same
# 3-strikes cmd_failures skip logic, same per-command guards (XML-envelope
# detection, out-of-range sanity checks, truthy-but-too-short payloads), but
# purely functional: no coordinator/cache-dict side effects, just a returned
# RcpCameraData with only the successfully-read fields populated.
#
# Below ports the SCENARIOS from that source file (not its coordinator-stub
# mocking mechanics) — call fetch_rcp_camera_data directly and assert on the
# returned dataclass.

FETCH_CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_reader(read_map: dict) -> object:
    """Build an async side_effect for `rcp_read` keyed on the `command` arg.

    A value that is a `BaseException` instance is raised instead of
    returned, so a single read_map can drive both success and exception
    scenarios. Commands not present in `read_map` return None.
    """

    async def _reader(
        session: object,
        rcp_base: str,
        command: str,
        sessionid: str,
        type_: str = "P_OCTET",
        num: int = 0,
        session_cache: object = None,
    ) -> object:
        val = read_map.get(command)
        if isinstance(val, BaseException):
            raise val
        return val

    return _reader


async def _call_fetch(
    read_map: dict,
    cmd_failures: dict | None = None,
    session_id: object = "fake-sid",
) -> tuple:
    """Patch get_cached_rcp_session + rcp_read and call fetch_rcp_camera_data.

    `cmd_failures`, if given, is passed through by reference (not copied) so
    callers can assert on its post-call mutated state.
    """
    failures = cmd_failures if cmd_failures is not None else {}
    mock_read = AsyncMock(side_effect=_make_reader(read_map))
    with (
        patch(
            f"{MODULE}.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value=session_id,
        ),
        patch(f"{MODULE}.rcp_read", mock_read),
    ):
        result = await fetch_rcp_camera_data(
            _make_session(),
            _fake_ssl_context(),
            {},
            {},
            failures,
            FETCH_CAM_ID,
            PROXY_HOST,
            PROXY_HASH,
        )
    return result, mock_read


class TestFetchRcpCameraDataNoSession:
    """No RCP session could be opened -> None, zero reads attempted."""

    @pytest.mark.asyncio
    async def test_no_session_returns_none_and_skips_all_reads(self) -> None:
        result, mock_read = await _call_fetch({}, session_id=None)

        assert result is None
        mock_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_string_session_id_also_treated_as_no_session(self) -> None:
        """`if not session_id:` also rejects a falsy non-None session id."""
        result, mock_read = await _call_fetch({}, session_id="")

        assert result is None
        mock_read.assert_not_called()


class TestFetchRcpCameraDataDimmer:
    """0x0c22 LED dimmer -> T_WORD, num=1 -> integer 0-100."""

    @pytest.mark.asyncio
    async def test_valid_dimmer_cached(self) -> None:
        raw = struct.pack(">H", 75)
        result, _mock_read = await _call_fetch({"0x0c22": raw})

        assert result is not None
        assert result.dimmer == 75

    @pytest.mark.asyncio
    async def test_out_of_range_dimmer_not_cached(self) -> None:
        """300 is outside 0-100 -> not cached (out-of-range branch, not the
        XML-envelope branch — unlike Gen2's real 0x0A0A=2570 value, whose raw
        bytes b'\\n\\n' are pure whitespace and hit the XML guard instead;
        that case is covered separately by test_xml_envelope_not_cached)."""
        raw = struct.pack(">H", 300)
        result, _mock_read = await _call_fetch({"0x0c22": raw})

        assert result is not None
        assert result.dimmer is None

    @pytest.mark.asyncio
    async def test_xml_envelope_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c22": b"\n\n"})

        assert result is not None
        assert result.dimmer is None

    @pytest.mark.asyncio
    async def test_truthy_too_short_not_cached(self) -> None:
        """1 byte — truthy, non-XML, but shorter than the expected 2 bytes."""
        result, _mock_read = await _call_fetch({"0x0c22": b"\x01"})

        assert result is not None
        assert result.dimmer is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c22": RuntimeError("boom")})

        assert result is not None
        assert result.dimmer is None


class TestFetchRcpCameraDataPrivacy:
    """0x0d00 privacy mask -> P_OCTET 4B -> byte[1] == 1 means ON."""

    @pytest.mark.asyncio
    async def test_privacy_on_byte1_eq_1(self) -> None:
        raw = bytes([0x00, 0x01, 0x00, 0x00])
        result, _mock_read = await _call_fetch({"0x0d00": raw})

        assert result is not None
        assert result.privacy == 1

    @pytest.mark.asyncio
    async def test_privacy_off_byte1_eq_0(self) -> None:
        raw = bytes([0x00, 0x00, 0x00, 0x00])
        result, _mock_read = await _call_fetch({"0x0d00": raw})

        assert result is not None
        assert result.privacy == 0

    @pytest.mark.asyncio
    async def test_xml_envelope_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0d00": b"\n\n"})

        assert result is not None
        assert result.privacy is None

    @pytest.mark.asyncio
    async def test_truthy_too_short_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0d00": b"\x01"})

        assert result is not None
        assert result.privacy is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0d00": RuntimeError("privacy boom")})

        assert result is not None
        assert result.privacy is None


class TestFetchRcpCameraDataClock:
    """0x0a0f camera clock -> 8 bytes -> offset vs server time."""

    @pytest.mark.asyncio
    async def test_valid_clock_caches_offset(self) -> None:
        raw_clock = struct.pack(">HBBBBBB", 2026, 5, 12, 12, 0, 0, 1)
        result, _mock_read = await _call_fetch({"0x0a0f": raw_clock})

        assert result is not None
        assert isinstance(result.clock_offset, float)

    @pytest.mark.asyncio
    async def test_month_13_unexpected_layout_not_cached(self) -> None:
        """month=13 fails per-field validation -> 'unexpected layout' branch."""
        raw_clock = struct.pack(">HBBBBBB", 2026, 13, 1, 12, 0, 0, 0)
        result, _mock_read = await _call_fetch({"0x0a0f": raw_clock})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_day_32_unexpected_layout_not_cached(self) -> None:
        raw_clock = struct.pack(">HBBBBBB", 2026, 1, 32, 12, 0, 0, 0)
        result, _mock_read = await _call_fetch({"0x0a0f": raw_clock})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_hour_25_unexpected_layout_not_cached(self) -> None:
        raw_clock = struct.pack(">HBBBBBB", 2026, 1, 1, 25, 0, 0, 0)
        result, _mock_read = await _call_fetch({"0x0a0f": raw_clock})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_invalid_calendar_date_marks_fail(self) -> None:
        """Feb 30 — every per-field range passes but it isn't a real date;
        `datetime(...)` raises ValueError -> caught by the inner try/except,
        not the outer broad except."""
        bad_clock = struct.pack(">HBBBBBB", 2026, 2, 30, 12, 0, 0, 0)
        result, _mock_read = await _call_fetch({"0x0a0f": bad_clock})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_xml_envelope_not_cached(self) -> None:
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0a0f</command>\n</rcp>"
        result, _mock_read = await _call_fetch({"0x0a0f": xml_bytes})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_raw_none_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0a0f": None})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_truthy_too_short_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0a0f": b"\x00\x01\x02\x03"})

        assert result is not None
        assert result.clock_offset is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0a0f": RuntimeError("clock boom")})

        assert result is not None
        assert result.clock_offset is None


class TestFetchRcpCameraDataLanIp:
    """0x0a36 LAN IP -> 4-byte binary or null-terminated ASCII."""

    @pytest.mark.asyncio
    async def test_4_byte_binary_ip(self) -> None:
        ip_bytes = bytes([10, 0, 0, 5])
        result, _mock_read = await _call_fetch({"0x0a36": ip_bytes})

        assert result is not None
        assert result.lan_ip == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_ascii_ip_string(self) -> None:
        ip_bytes = b"192.0.2.100\x00"
        result, _mock_read = await _call_fetch({"0x0a36": ip_bytes})

        assert result is not None
        assert result.lan_ip == "192.0.2.100"

    @pytest.mark.asyncio
    async def test_xml_wrapped_payload_not_cached(self) -> None:
        """Nested XML doc -> decoded string starts with '<' -> rejected."""
        xml_bytes = b"<rcp><payload>00000000</payload></rcp>"
        result, _mock_read = await _call_fetch({"0x0a36": xml_bytes})

        assert result is not None
        assert result.lan_ip is None

    @pytest.mark.asyncio
    async def test_zero_ip_rejected(self) -> None:
        ip_bytes = bytes([0, 0, 0, 0])
        result, _mock_read = await _call_fetch({"0x0a36": ip_bytes})

        assert result is not None
        assert result.lan_ip is None

    @pytest.mark.asyncio
    async def test_raw_none_marks_fail(self) -> None:
        result, _mock_read = await _call_fetch({"0x0a36": None})

        assert result is not None
        assert result.lan_ip is None

    @pytest.mark.asyncio
    async def test_empty_bytes_neither_branch(self) -> None:
        """raw = b'' is falsy but not None -> neither `if raw:` nor
        `elif raw is None:` fires -> no crash, field stays unset."""
        result, _mock_read = await _call_fetch({"0x0a36": b""})

        assert result is not None
        assert result.lan_ip is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0a36": RuntimeError("lan ip boom")})

        assert result is not None
        assert result.lan_ip is None


class TestFetchRcpCameraDataProductName:
    """0x0aea product name -> null-terminated ASCII."""

    @pytest.mark.asyncio
    async def test_gen2_outdoor_product_name_cached(self) -> None:
        raw_name = b"FLEXIDOME IP starlight 8000i\x00\x00\x00"
        result, _mock_read = await _call_fetch({"0x0aea": raw_name})

        assert result is not None
        assert result.product_name == "FLEXIDOME IP starlight 8000i"

    @pytest.mark.asyncio
    async def test_whitespace_trimmed_before_cache(self) -> None:
        raw_name = b"  CAMERA_360  \x00"
        result, _mock_read = await _call_fetch({"0x0aea": raw_name})

        assert result is not None
        assert result.product_name == "CAMERA_360"

    @pytest.mark.asyncio
    async def test_xml_wrapped_product_name_skipped(self) -> None:
        xml_blob = b"<rcp><payload>0000</payload></rcp>"
        result, _mock_read = await _call_fetch({"0x0aea": xml_blob})

        assert result is not None
        assert result.product_name is None

    @pytest.mark.asyncio
    async def test_null_only_payload_empty_after_strip_skipped(self) -> None:
        """Decodes to an empty string after rstrip/strip -> falsy `name_str`."""
        result, _mock_read = await _call_fetch({"0x0aea": b"\x00\x00\x00"})

        assert result is not None
        assert result.product_name is None

    @pytest.mark.asyncio
    async def test_raw_none_marks_fail(self) -> None:
        result, _mock_read = await _call_fetch({"0x0aea": None})

        assert result is not None
        assert result.product_name is None

    @pytest.mark.asyncio
    async def test_empty_bytes_neither_branch(self) -> None:
        result, _mock_read = await _call_fetch({"0x0aea": b""})

        assert result is not None
        assert result.product_name is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch(
            {"0x0aea": RuntimeError("product name boom")}
        )

        assert result is not None
        assert result.product_name is None


class TestFetchRcpCameraDataBitrate:
    """0x0c81 bitrate ladder -> series of big-endian uint32 kbps values."""

    @pytest.mark.asyncio
    async def test_bitrate_ladder_parsed(self) -> None:
        bitrate_bytes = struct.pack(">II", 1000, 2000)
        result, _mock_read = await _call_fetch({"0x0c81": bitrate_bytes})

        assert result is not None
        assert result.bitrate == [1000, 2000]

    @pytest.mark.asyncio
    async def test_out_of_range_bitrate_skips_cache(self) -> None:
        bad_bitrate = struct.pack(">I", 999999)
        result, _mock_read = await _call_fetch({"0x0c81": bad_bitrate})

        assert result is not None
        assert result.bitrate is None

    @pytest.mark.asyncio
    async def test_xml_envelope_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c81": b"\n\n"})

        assert result is not None
        assert result.bitrate is None

    @pytest.mark.asyncio
    async def test_truthy_too_short_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c81": b"\x00\x01"})

        assert result is not None
        assert result.bitrate is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c81": RuntimeError("bitrate boom")})

        assert result is not None
        assert result.bitrate is None


class TestFetchRcpCameraDataAlarmCatalog:
    """0x0c38 alarm catalog -- UTF-16-BE, ~1366 bytes."""

    @staticmethod
    def _make_alarm_blob(names: list) -> bytes:
        return ("\x00".join(names) + "\x00").encode("utf-16-be")

    @pytest.mark.asyncio
    async def test_alarm_catalog_cached(self) -> None:
        names = ["Virtual Alarm 0", "Flame detected", "Motion detected"]
        raw = self._make_alarm_blob(names)
        assert len(raw) > 10

        result, _mock_read = await _call_fetch({"0x0c38": raw})

        assert result is not None
        assert result.alarm_catalog is not None
        assert len(result.alarm_catalog) == len(names)

    @pytest.mark.asyncio
    async def test_short_payload_below_threshold_no_cache(self) -> None:
        raw = b"\x00" * 8
        result, _mock_read = await _call_fetch({"0x0c38": raw})

        assert result is not None
        assert result.alarm_catalog is None

    @pytest.mark.asyncio
    async def test_xml_envelope_not_cached(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c38": b"\n\n"})

        assert result is not None
        assert result.alarm_catalog is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c38": RuntimeError("catalog boom")})

        assert result is not None
        assert result.alarm_catalog is None


class TestFetchRcpCameraDataMotionZones:
    """0x0c00 motion detection zones -- 5 zones x 28 bytes. No XML guard
    (per the source comment, this command predates the XML-envelope fix)."""

    @pytest.mark.asyncio
    async def test_valid_28_byte_payload_caches_zones(self) -> None:
        one_zone = b"\x01" + b"\x00" * 27
        result, _mock_read = await _call_fetch({"0x0c00": one_zone})

        assert result is not None
        assert result.motion_zones is not None
        assert len(result.motion_zones) == 1
        assert result.motion_zones[0]["zone_id"] == 0

    @pytest.mark.asyncio
    async def test_short_payload_under_28_bytes_no_cache(self) -> None:
        """Truthy but < 28 bytes -> neither `if raw and len>=28` nor
        `elif raw is None` fires -> no cache, no crash."""
        short_raw = b"\x00" * 20
        result, _mock_read = await _call_fetch({"0x0c00": short_raw})

        assert result is not None
        assert result.motion_zones is None

    @pytest.mark.asyncio
    async def test_raw_none_marks_fail(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c00": None})

        assert result is not None
        assert result.motion_zones is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c00": RuntimeError("zones boom")})

        assert result is not None
        assert result.motion_zones is None


class TestFetchRcpCameraDataMotionCoords:
    """0x0c0a motion zone coordinates -- int32 normalized +-1.0 as x2^31."""

    @pytest.mark.asyncio
    async def test_motion_coords_cached(self) -> None:
        raw_coords = struct.pack(">HHHH", 0, 0, 10000, 10000) + struct.pack(
            ">HHHH", 2500, 2500, 7500, 7500
        )
        assert len(raw_coords) >= 16

        result, _mock_read = await _call_fetch({"0x0c0a": raw_coords})

        assert result is not None
        assert result.motion_coords is not None
        assert len(result.motion_coords) == 2
        assert result.motion_coords[0] == {
            "x1": 0.0,
            "y1": 0.0,
            "x2": 100.0,
            "y2": 100.0,
        }

    @pytest.mark.asyncio
    async def test_xml_envelope_marks_fail(self) -> None:
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0c0a</command>\n</rcp>"
        result, _mock_read = await _call_fetch({"0x0c0a": xml_bytes})

        assert result is not None
        assert result.motion_coords is None

    @pytest.mark.asyncio
    async def test_too_short_or_none_marks_fail(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c0a": None})

        assert result is not None
        assert result.motion_coords is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c0a": RuntimeError("coords boom")})

        assert result is not None
        assert result.motion_coords is None


class TestFetchRcpCameraDataTlsCert:
    """0x0b91 TLS certificate -- DER X.509, ~455 bytes. No XML guard."""

    @pytest.mark.asyncio
    async def test_tls_cert_cached_when_data_present(self) -> None:
        fake_cert = b"\x30\x82" + b"\xff" * 58
        result, _mock_read = await _call_fetch({"0x0b91": fake_cert})

        assert result is not None
        assert result.tls_cert is not None
        assert "raw_size" in result.tls_cert

    @pytest.mark.asyncio
    async def test_raw_none_marks_fail(self) -> None:
        result, _mock_read = await _call_fetch({"0x0b91": None})

        assert result is not None
        assert result.tls_cert is None

    @pytest.mark.asyncio
    async def test_truthy_too_short_neither_branch(self) -> None:
        """Truthy but <= 50 bytes -> neither `if raw and len>50` nor
        `elif raw is None` fires."""
        result, _mock_read = await _call_fetch({"0x0b91": b"\x00" * 10})

        assert result is not None
        assert result.tls_cert is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0b91": RuntimeError("tls boom")})

        assert result is not None
        assert result.tls_cert is None


class TestFetchRcpCameraDataNetworkServices:
    """0x0c62 network services -- TLV list, ~469 bytes."""

    @pytest.mark.asyncio
    async def test_valid_payload_caches_services(self) -> None:
        network_raw = b"HTTP\x00HTTPS\x00RTSP\x00"
        assert len(network_raw) > 10
        assert not network_raw.startswith(b"<")

        result, _mock_read = await _call_fetch({"0x0c62": network_raw})

        assert result is not None
        assert result.network_services is not None
        assert "HTTP" in result.network_services

    @pytest.mark.asyncio
    async def test_xml_payload_skips_cache(self) -> None:
        xml_raw = b"<rcp>" + b"x" * 50
        result, _mock_read = await _call_fetch({"0x0c62": xml_raw})

        assert result is not None
        assert result.network_services is None

    @pytest.mark.asyncio
    async def test_whitespace_prefixed_xml_envelope_rejected(self) -> None:
        whitespace_prefixed_xml = b"\n\n<rcp><payload>aabbcc</payload></rcp>"
        result, _mock_read = await _call_fetch({"0x0c62": whitespace_prefixed_xml})

        assert result is not None
        assert result.network_services is None

    @pytest.mark.asyncio
    async def test_short_payload_below_threshold_no_cache(self) -> None:
        result, _mock_read = await _call_fetch({"0x0c62": b"\x00" * 8})

        assert result is not None
        assert result.network_services is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch(
            {"0x0c62": RuntimeError("network services boom")}
        )

        assert result is not None
        assert result.network_services is None


class TestFetchRcpCameraDataIvaCatalog:
    """0x0b60 IVA analytics catalog -- 65 entries x 6B."""

    @pytest.mark.asyncio
    async def test_iva_catalog_cached(self) -> None:
        entry1 = struct.pack(">HHH", 1, 0x0100, 0x0001)
        entry2 = struct.pack(">HHH", 2, 0x0200, 0x0000)
        raw_iva = entry1 + entry2

        result, _mock_read = await _call_fetch({"0x0b60": raw_iva})

        assert result is not None
        assert result.iva_catalog is not None
        assert any(m["module_id"] == 1 and m["active"] for m in result.iva_catalog)
        assert any(m["module_id"] == 2 and not m["active"] for m in result.iva_catalog)

    @pytest.mark.asyncio
    async def test_xml_envelope_marks_fail(self) -> None:
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0b60</command>\n</rcp>"
        result, _mock_read = await _call_fetch({"0x0b60": xml_bytes})

        assert result is not None
        assert result.iva_catalog is None

    @pytest.mark.asyncio
    async def test_short_payload_no_cache(self) -> None:
        result, _mock_read = await _call_fetch({"0x0b60": b"\x00\x01\x02"})

        assert result is not None
        assert result.iva_catalog is None

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        result, _mock_read = await _call_fetch({"0x0b60": RuntimeError("iva boom")})

        assert result is not None
        assert result.iva_catalog is None


class TestFetchRcpCameraDataSkipGuard:
    """cmd_failures 3-strikes suppression, pinned across all 12 commands."""

    ALL_COMMANDS = (
        "0x0c22",
        "0x0d00",
        "0x0a0f",
        "0x0a36",
        "0x0aea",
        "0x0c81",
        "0x0c38",
        "0x0c00",
        "0x0c0a",
        "0x0b91",
        "0x0c62",
        "0x0b60",
    )

    @pytest.mark.asyncio
    async def test_all_commands_skipped_after_3_failures(self) -> None:
        """Every command already at 3 consecutive failures -> a further call
        skips ALL 12 reads entirely (was previously an unguarded retry for
        some of these commands) and returns an all-default RcpCameraData."""
        cmd_failures = {cmd: 3 for cmd in self.ALL_COMMANDS}

        result, mock_read = await _call_fetch({}, cmd_failures=cmd_failures)

        assert result is not None
        mock_read.assert_not_called()
        assert result == RcpCameraData()

    @pytest.mark.asyncio
    async def test_command_not_skipped_at_2_failures(self) -> None:
        """2 failures — still below the 3-strike threshold, still attempted."""
        cmd_failures = {"0x0c22": 2}
        raw = struct.pack(">H", 42)

        result, mock_read = await _call_fetch(
            {"0x0c22": raw}, cmd_failures=cmd_failures
        )

        assert result is not None
        assert result.dimmer == 42
        called_cmds = [call.args[2] for call in mock_read.call_args_list]
        assert "0x0c22" in called_cmds


class TestFetchRcpCameraDataCmdFailuresLifecycle:
    """A single success pops the per-command counter; a 3rd consecutive
    failure crosses the log-threshold branch inside `_mark_fail`."""

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self) -> None:
        cmd_failures = {"0x0c22": 2}
        raw = struct.pack(">H", 42)

        await _call_fetch({"0x0c22": raw}, cmd_failures=cmd_failures)

        assert "0x0c22" not in cmd_failures

    @pytest.mark.asyncio
    async def test_third_consecutive_failure_reaches_threshold(self) -> None:
        """2 pre-existing failures + 1 more (raw=None) -> counter hits
        exactly 3, exercising `_mark_fail`'s `if cmd_failures[cmd] == 3:`
        True branch (one-time debug log)."""
        cmd_failures = {"0x0c22": 2}

        result, _mock_read = await _call_fetch(
            {"0x0c22": None}, cmd_failures=cmd_failures
        )

        assert result is not None
        assert result.dimmer is None
        assert cmd_failures["0x0c22"] == 3

    @pytest.mark.asyncio
    async def test_first_failure_does_not_reach_threshold(self) -> None:
        """Starting from 0 -> 1 failure, well below the ==3 log branch."""
        cmd_failures: dict = {}

        await _call_fetch({"0x0c22": None}, cmd_failures=cmd_failures)

        assert cmd_failures["0x0c22"] == 1
