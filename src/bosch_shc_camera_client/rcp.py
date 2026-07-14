"""Bosch RCP (Remote Configuration Protocol) via cloud proxy and direct LOCAL.

Session/read helpers for RCP session management, binary protocol reads, and
response parsers. Callers own the aiohttp.ClientSession / ssl.SSLContext —
this module never creates or caches HA-specific process-wide state, it only
does the protocol work with what it's handed.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import ssl
import struct
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .auth_utils import async_digest_request

_LOGGER = logging.getLogger(__name__)

# Type alias for the session cache: {proxy_hash: (session_id, expires_at_monotonic)}
RcpSessionCache = dict[str, tuple[str, float]]
# Type alias for the per-proxy_hash session-open lock dict
RcpSessionLocks = dict[str, asyncio.Lock]


def _get_rcp_session_lock(
    session_locks: RcpSessionLocks, proxy_hash: str
) -> asyncio.Lock:
    """Get or create a per-proxy_hash RCP session-open lock.

    Safe under asyncio: check-then-insert has no `await` between the two
    steps, so concurrent coroutines cannot interleave here.
    """
    lock = session_locks.get(proxy_hash)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[proxy_hash] = lock
    return lock


def _is_xml_envelope(raw: bytes | None) -> bool:
    """True if raw is (or starts with) a cloud-proxy XML envelope.

    Gen2 cloud proxy occasionally returns the outer RCP XML envelope as the
    P_OCTET payload bytes instead of the requested binary value. The envelope
    starts with whitespace + ``<rcp>``. Short responses (e.g. T_WORD = 2 bytes)
    may contain only the leading whitespace; treat pure-whitespace as XML too,
    since no legitimate binary payload should start with bytes 0x0A/0x0D/0x09.
    """
    if not raw:
        return False
    stripped = raw.lstrip(b"\n\r\t ")
    return not stripped or stripped.startswith(b"<")


# ── Session management ───────────────────────────────────────────────────────


async def get_cached_rcp_session(
    ssl_context: ssl.SSLContext,
    session_cache: RcpSessionCache,
    proxy_host: str,
    proxy_hash: str,
    session_locks: RcpSessionLocks | None = None,
) -> str | None:
    """Return a cached RCP session ID, opening a new one if missing or expired.

    Caches valid session IDs for 5 minutes (TTL 300 s) to avoid the 2-step
    RCP handshake (0xff0c + 0xff0d) on every thumbnail or data fetch.

    Serialized per proxy_hash when `session_locks` is given — Bosch's proxy
    only tolerates one live session per proxy_hash, so two callers racing an
    empty/expired cache would otherwise each open their own session and one
    gets rejected (sessionid 0x00000000). Callers sharing a cache dict across
    call sites MUST also share the same `session_locks` dict, or the two
    call sites can still race each other.
    """

    async def _get() -> str | None:
        now = time.monotonic()
        cached = session_cache.get(proxy_hash)
        if cached:
            session_id, expires_at = cached
            if now < expires_at:
                return session_id
            del session_cache[proxy_hash]

        new_session_id: str | None = await rcp_session(
            ssl_context, session_cache, proxy_host, proxy_hash
        )
        if new_session_id:
            session_cache[proxy_hash] = (new_session_id, now + 300.0)  # 5-min TTL
        return new_session_id

    if session_locks is None:
        return await _get()
    async with _get_rcp_session_lock(session_locks, proxy_hash):
        return await _get()


async def rcp_session(
    ssl_context: ssl.SSLContext,
    session_cache: RcpSessionCache,
    proxy_host: str,
    proxy_hash: str,
) -> str | None:
    """Open an RCP session via the cloud proxy and return the sessionid, or None on failure.

    The RCP handshake consists of two steps:
      1. WRITE command 0xff0c with a fixed payload -> extract <sessionid> from XML response
      2. WRITE command 0xff0d with the sessionid -> ACK (confirms the session)

    Auth=3 (anonymous via URL hash) provides read-only access.
    The proxy_host should be in the form "proxy-NN.live.cbs.boschsecurity.com:42090".
    """
    base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
    init_payload = "0x0102004000000000040000000000000000010000000000000001000000000000"

    connector = aiohttp.TCPConnector(ssl=ssl_context)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            # Step 1: open session
            params1 = {
                "command": "0xff0c",
                "direction": "WRITE",
                "type": "P_OCTET",
                "payload": init_payload,
            }
            try:
                async with asyncio.timeout(8):
                    async with session.get(base, params=params1) as resp:
                        if resp.status != 200:
                            _LOGGER.debug(
                                "rcp_session: step1 HTTP %d for %s",
                                resp.status,
                                proxy_host,
                            )
                            return None
                        text = await resp.text()
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("rcp_session: step1 error for %s: %s", proxy_host, err)
                return None

            # Parse <sessionid> from XML response
            m = _re.search(r"<sessionid>(\S+)</sessionid>", text, _re.IGNORECASE)
            if not m:
                _LOGGER.debug(
                    "rcp_session: no <sessionid> in response for %s: %s",
                    proxy_host,
                    text[:200],
                )
                return None
            session_id = m.group(1)

            # Validate session ID before ACK — 0x00000000 means proxy rejected
            if session_id == "0x00000000":
                _LOGGER.debug(
                    "rcp_session: invalid session 0x00000000 for %s — proxy rejected",
                    proxy_host,
                )
                return None

            # Step 2: ACK the session
            params2 = {
                "command": "0xff0d",
                "direction": "WRITE",
                "type": "P_OCTET",
                "sessionid": session_id,
            }
            try:
                async with asyncio.timeout(8):
                    async with session.get(base, params=params2) as resp2:
                        _LOGGER.debug(
                            "rcp_session: ACK HTTP %d for %s (sessionid=%s)",
                            resp2.status,
                            proxy_host,
                            session_id,
                        )
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("rcp_session: step2 error for %s: %s", proxy_host, err)

            return session_id
    finally:
        await connector.close()


# ── Direct LOCAL RCP (no cloud proxy, no auth — Gen2 only) ───────────────────
#
# Gen2 cameras accept RCP commands on http://CAM_IP/rcp.xml without any
# authentication. Used as a fallback path when the Bosch cloud API or
# auth server is unreachable, so the caller can still read and (best-effort)
# write privacy state without going through the cloud.


async def rcp_local_read(
    session: aiohttp.ClientSession,
    cam_ip: str,
    command: str,
    type_: str = "P_OCTET",
    num: int = 0,
) -> bytes | None:
    """Read an RCP value directly from the camera's LAN HTTP endpoint.

    Returns the decoded payload bytes on success, None on any failure.
    Gen2 cameras answer unauthenticated RCP queries on port 80; Gen1 returns
    401 and this function will simply return None (graceful).
    """
    base = f"http://{cam_ip}/rcp.xml"
    params: dict[str, str] = {
        "command": command,
        "direction": "READ",
        "type": type_,
    }
    if num:
        params["num"] = str(num)
    try:
        async with asyncio.timeout(5):
            async with session.get(base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_local_read: %s@%s HTTP %d",
                        command,
                        cam_ip,
                        resp.status,
                    )
                    return None
                raw = await resp.read()
                err_m = _re.search(rb"<err>(\S+)</err>", raw, _re.IGNORECASE)
                if err_m:
                    _LOGGER.debug(
                        "rcp_local_read: %s@%s err=%s",
                        command,
                        cam_ip,
                        err_m.group(1).decode("ascii", errors="replace"),
                    )
                    return None
                payload_m = _re.search(
                    rb"<str>([0-9a-fA-F]+)</str>", raw, _re.IGNORECASE
                ) or _re.search(
                    rb"<payload>([0-9a-fA-F]+)</payload>", raw, _re.IGNORECASE
                )
                if payload_m:
                    return bytes.fromhex(payload_m.group(1).decode("ascii"))
                if raw and not raw.startswith(b"<"):
                    return bytes(raw)
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("rcp_local_read: %s@%s %s", command, cam_ip, err)
    return None


async def rcp_local_write(
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
    """Write an RCP value directly via the camera's LAN endpoint.

    Bosch SHC cameras only listen on HTTPS port 443 (no plain HTTP) and the
    `rcp.xml` endpoint requires HTTP Digest auth. Pass the cycling
    `cbs-XXXXXXXX` user + password from a recent LOCAL session so the request
    is authorised. Earlier versions issued plain HTTP on port 80 and silently
    failed — confirmed against live Gen2 hardware 2026-05-20.

    Returns True on success. `payload_hex` may start with "0x" or not.
    Some commands require `num=1` (e.g. T_WORD-typed writes like 0x0c22 LED
    dimmer); the default 0 keeps backward compatibility with existing callers.
    Best-effort: any error returns False (caller should handle gracefully).
    """
    base = f"https://{cam_ip}/rcp.xml"
    if not payload_hex.lower().startswith("0x"):
        payload_hex = "0x" + payload_hex
    params: dict[str, str] = {
        "command": command,
        "direction": "WRITE",
        "type": type_,
        "payload": payload_hex,
    }
    if num:
        params["num"] = str(num)
    # aiohttp serialises params into the query string when added to a GET URL.
    # async_digest_request takes the full URL so we build it explicitly.
    url = f"{base}?{urlencode(params)}"
    try:
        if user and password:
            async with await async_digest_request(
                session,
                "GET",
                url,
                user,
                password,
                timeout=5.0,
                ssl=False,
            ) as resp:
                status = resp.status
                if status != 200:
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s HTTPS %d (Digest)",
                        command,
                        cam_ip,
                        status,
                    )
                    return False
                raw = await resp.read()
                if b"<err>" in raw.lower():
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s RCP error in response",
                        command,
                        cam_ip,
                    )
                    return False
                return True
        # Fallback path — no auth supplied. Kept for back-compat with
        # callers that haven't been wired through to the creds cache yet.
        # Will fail on every modern Gen2 firmware with HTTP 401.
        async with asyncio.timeout(5):
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s HTTPS %d (no auth)",
                        command,
                        cam_ip,
                        resp.status,
                    )
                    return False
                raw = await resp.read()
                if b"<err>" in raw.lower():
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s RCP error in response",
                        command,
                        cam_ip,
                    )
                    return False
                return True
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        _LOGGER.debug("rcp_local_write: %s@%s %s", command, cam_ip, err)
    return False


async def rcp_local_read_privacy(
    session: aiohttp.ClientSession,
    cam_ip: str,
) -> bool | None:
    """Read privacy-mode state via direct LOCAL RCP (Gen2, no auth).

    Uses command 0x0d00 (privacy mask), which returns 4 bytes where byte[1]
    indicates privacy state: 0=OFF, 1=ON. Returns None if unavailable.
    """
    raw = await rcp_local_read(session, cam_ip, "0x0d00", "P_OCTET")
    if raw and len(raw) >= 2:
        return bool(raw[1])
    return None


async def rcp_local_write_privacy(
    session: aiohttp.ClientSession,
    cam_ip: str,
    enabled: bool,
    *,
    user: str | None = None,
    password: str | None = None,
) -> bool:
    """Write privacy-mode state via direct LOCAL RCP (Gen2 over HTTPS+Digest).

    Best-effort fallback used when the cloud API is unreachable. Pass the
    cycling LOCAL Digest user + password from a recent LAN session so the
    write authorises — Gen2 cameras require Digest auth on `rcp.xml` and
    do not accept anonymous WRITEs.
    """
    # Privacy mask payload: 4 bytes, byte[1] carries the mode. Keep the
    # remaining bytes zero so we don't stamp over other mask fields.
    payload = "00010000" if enabled else "00000000"
    return await rcp_local_write(
        session,
        cam_ip,
        "0x0d00",
        payload,
        "P_OCTET",
        user=user,
        password=password,
    )


async def rcp_local_write_front_light(
    session: aiohttp.ClientSession,
    cam_ip: str,
    brightness: int,
    *,
    user: str | None = None,
    password: str | None = None,
) -> bool:
    """Write the front-light brightness via direct LOCAL RCP (Gen2 HTTPS+Digest).

    Brightness is 0-100. 0 turns the light off; values 1-100 set the dimmer.
    Maps to RCP 0x0c22 (`T_WORD`, num=1) — same command read by a LED-dimmer
    sensor. Pass the cycling LOCAL Digest user + password so the write
    authorises.
    """
    # Clamp to legal range and encode as a 4-hex-digit big-endian word (T_WORD).
    val = max(0, min(100, int(brightness)))
    payload = f"{val:04x}"
    return await rcp_local_write(
        session,
        cam_ip,
        "0x0c22",
        payload,
        "T_WORD",
        num=1,
        user=user,
        password=password,
    )


# ── Read operations (cloud proxy) ─────────────────────────────────────────────


async def rcp_read(
    session: aiohttp.ClientSession,
    rcp_base: str,
    command: str,
    sessionid: str,
    type_: str = "P_OCTET",
    num: int = 0,
    session_cache: RcpSessionCache | None = None,
) -> bytes | None:
    """READ an RCP command and return the payload bytes, or None on failure.

    The RCP endpoint returns XML like:
      <rcp ... ><payload>0a1b2c3d...</payload></rcp>
    or for errors:
      <rcp ... ><err>0xa0</err></rcp>

    This function extracts the hex payload and returns it as bytes. Uses the
    caller-provided (typically pinned/TLS-verified) aiohttp session.

    If session_cache is provided, the cached session for the URL's proxy_hash
    is invalidated on HTTP 401/403 or RCP <err>0x0c0d</err> (session closed)
    so the next call opens a fresh handshake instead of reusing a dead ID.
    """
    params: dict[str, str] = {
        "command": command,
        "direction": "READ",
        "type": type_,
        "sessionid": sessionid,
    }
    if num:
        params["num"] = str(num)

    def _drop_cached_session() -> None:
        if session_cache is None:
            return
        parts = rcp_base.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "rcp.xml":
            proxy_hash = parts[-2]
            if session_cache.pop(proxy_hash, None) is not None:
                _LOGGER.debug("RCP session cache invalidated for %s", proxy_hash[:8])

    try:
        async with asyncio.timeout(8):
            async with session.get(rcp_base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug("rcp_read: command=%s HTTP %d", command, resp.status)
                    if resp.status in (401, 403):
                        _drop_cached_session()
                    return None
                raw = await resp.read()
                # RCP returns XML: <rcp ...><payload>HEX</payload></rcp>
                # or error: <rcp ...><err>0xa0</err></rcp>
                # Parse raw bytes with regex to avoid UTF-8 decode issues.

                # Check for error response
                err_m = _re.search(rb"<err>(\S+)</err>", raw, _re.IGNORECASE)
                if err_m:
                    err_code = err_m.group(1).decode("ascii", errors="replace")
                    _LOGGER.debug(
                        "rcp_read: command=%s error=%s",
                        command,
                        err_code,
                    )
                    # 0x0c0d = session closed → drop the cached ID so the next
                    # call reopens the handshake instead of replaying a dead one.
                    if err_code.lower() == "0x0c0d":
                        _drop_cached_session()
                    return None

                # Extract hex payload from XML — Bosch uses <str> or <payload> tag
                # depending on firmware version / request context
                payload_m = _re.search(
                    rb"<str>([0-9a-fA-F]+)</str>", raw, _re.IGNORECASE
                ) or _re.search(
                    rb"<payload>([0-9a-fA-F]+)</payload>", raw, _re.IGNORECASE
                )
                if payload_m:
                    return bytes.fromhex(payload_m.group(1).decode("ascii"))

                # Fallback: raw binary response (non-XML, e.g. JPEG)
                if raw and not raw.startswith(b"<"):
                    return bytes(raw)

                _LOGGER.debug(
                    "rcp_read: command=%s no payload in response (%d bytes): %.100s",
                    command,
                    len(raw),
                    raw[:100],
                )
                return None
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        _LOGGER.debug("rcp_read: command=%s error: %s", command, err)
        return None


# ── Response parsers ──────────────────────────────────────────────────────────


def _parse_alarm_catalog(raw: bytes) -> list[dict[str, Any]]:
    """Parse alarm catalog (0x0c38) from UTF-16-BE encoded TLV data.

    Returns list of dicts: [{"id": 0, "name": "Virtual Alarm 0", "type": "virtual"}, ...]
    """
    alarms: list[dict[str, Any]] = []
    try:
        # The raw data contains TLV entries with alarm names in UTF-16-BE.
        # Each entry: 2B id + 2B length + UTF-16-BE name
        # Fallback: try decoding entire blob as UTF-16-BE and split by null chars
        text = raw.decode("utf-16-be", errors="replace")
        # Split by null characters and filter empty strings
        parts = [p.strip() for p in text.split("\x00") if p.strip()]
        for i, name in enumerate(parts):
            # Clean up control characters
            name = "".join(c for c in name if c.isprintable() or c == " ")
            if name and len(name) > 1:
                alarm_type = "unknown"
                name_lower = name.lower()
                if "virtual alarm" in name_lower:
                    alarm_type = "virtual"
                elif "flame" in name_lower:
                    alarm_type = "flame"
                elif "smoke" in name_lower:
                    alarm_type = "smoke"
                elif "audio" in name_lower:
                    alarm_type = "audio"
                elif "signal" in name_lower or "loss" in name_lower:
                    alarm_type = "signal"
                elif "storage" in name_lower or "disk" in name_lower:
                    alarm_type = "storage"
                elif (
                    "motion" in name_lower
                    or "resilmotion" in name_lower
                    or "resimotion" in name_lower
                ):
                    alarm_type = "motion"
                elif "reference" in name_lower:
                    alarm_type = "reference"
                elif "config" in name_lower:
                    alarm_type = "config"
                elif "global" in name_lower:
                    alarm_type = "global_change"
                elif "task" in name_lower:
                    alarm_type = "task"
                alarms.append({"id": i, "name": name, "type": alarm_type})
    except Exception as err:
        _LOGGER.debug("_parse_alarm_catalog error: %s", err)
    return alarms


def _parse_motion_zones(raw: bytes) -> list[dict[str, Any]]:
    """Parse motion detection zones (0x0c00) — 5 zones x 28 bytes each.

    Returns list of dicts with zone info (id, enabled, sensitivity fields).
    """
    zones: list[dict[str, Any]] = []
    zone_size = 28
    n_zones = min(len(raw) // zone_size, 5)
    for i in range(n_zones):
        chunk = raw[i * zone_size : (i + 1) * zone_size]
        if len(chunk) < zone_size:
            break
        # First bytes contain zone config, exact struct is camera-specific
        # Expose raw hex for diagnostics, plus zone index
        zones.append(
            {
                "zone_id": i,
                "raw_hex": chunk.hex(),
                "size": len(chunk),
            }
        )
    return zones


def _parse_motion_coords(raw: bytes) -> list[dict[str, float]]:
    """Parse motion region boundary coordinates (0x0c0a).

    Each zone is 8 bytes: x1(2B) y1(2B) x2(2B) y2(2B) in 0-10000 units.
    Returns list of zone rectangles as {x1, y1, x2, y2} in percent (0-100).
    """
    zones: list[dict[str, float]] = []
    zone_size = 8
    n_zones = len(raw) // zone_size
    for z in range(n_zones):
        chunk = raw[z * zone_size : (z + 1) * zone_size]
        if len(chunk) < 8:
            break
        x1 = struct.unpack(">H", chunk[0:2])[0]
        y1 = struct.unpack(">H", chunk[2:4])[0]
        x2 = struct.unpack(">H", chunk[4:6])[0]
        y2 = struct.unpack(">H", chunk[6:8])[0]
        # Convert 0-10000 to 0-100 percent
        zones.append(
            {
                "x1": round(x1 / 100, 1),
                "y1": round(y1 / 100, 1),
                "x2": round(x2 / 100, 1),
                "y2": round(y2 / 100, 1),
            }
        )
    return zones


def _parse_tls_cert(raw: bytes) -> dict[str, Any]:
    """Parse DER X.509 certificate (0x0b91) and extract key info.

    Falls back to raw hex if the `cryptography` package is not available.
    """
    info: dict[str, Any] = {"raw_size": len(raw)}
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(raw)
        info["issuer"] = cert.issuer.rfc4514_string()
        info["subject"] = cert.subject.rfc4514_string()
        info["serial"] = format(cert.serial_number, "x")
        info["not_before"] = cert.not_valid_before_utc.isoformat()
        info["not_after"] = cert.not_valid_after_utc.isoformat()
        info["key_size"] = cert.public_key().key_size
        info["signature_algorithm"] = cert.signature_algorithm_oid.dotted_string
    except ImportError:
        _LOGGER.debug("cryptography package not available — TLS cert raw only")
        info["raw_hex"] = raw[:40].hex() + "..."
    except Exception as err:
        _LOGGER.debug("TLS cert parse error: %s", err)
        info["raw_hex"] = raw[:40].hex() + "..."
    return info


def _parse_network_services(raw: bytes) -> list[str]:
    """Parse network services catalog (0x0c62) — TLV with service names.

    Returns list of service name strings.
    """
    services = []
    try:
        # TLV data contains ASCII service names separated by null bytes
        text = raw.decode("ascii", errors="replace")
        parts = [p.strip() for p in text.split("\x00") if p.strip()]
        for name in parts:
            clean = "".join(c for c in name if c.isprintable() or c == " ")
            if clean and len(clean) > 1:
                services.append(clean)
    except Exception as err:
        _LOGGER.debug("_parse_network_services error: %s", err)
    return services


def _parse_iva_catalog(raw: bytes) -> list[dict[str, Any]]:
    """Parse IVA analytics module catalog (0x0b60) — 65 entries x 6B.

    Returns list of dicts with module info.
    """
    modules: list[dict[str, Any]] = []
    entry_size = 6
    n = min(len(raw) // entry_size, 65)
    for i in range(n):
        chunk = raw[i * entry_size : (i + 1) * entry_size]
        if len(chunk) < entry_size:
            break
        # Each entry: 2B module_id + 2B version + 2B flags
        module_id = struct.unpack(">H", chunk[:2])[0]
        version = struct.unpack(">H", chunk[2:4])[0]
        flags = struct.unpack(">H", chunk[4:6])[0]
        if module_id > 0:  # skip empty entries
            modules.append(
                {
                    "module_id": module_id,
                    "version": version,
                    "flags": flags,
                    "active": bool(flags & 0x01),
                }
            )
    return modules
