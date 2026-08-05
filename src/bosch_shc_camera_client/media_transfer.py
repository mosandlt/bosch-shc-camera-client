"""Pure media-transfer mechanics shared by SMB/FTP/local-save export backends.

`is_safe_bosch_url` — SSRF-prevention URL allowlist check. The source
integration (`smb.py`, `fcm.py`, `coordinator.py`) previously carried three
byte-identical copies of this exact function; this is the single canonical
implementation those call sites now delegate to.

`sanitize_filename` — filename/dir-component sanitizer (path-traversal +
unsafe-character stripping), used to build export folder/file names from a
cloud-provided camera title.

`sync_http_get` / `sync_http_get_to_file` / `sync_http_get_chunked` — the
synchronous (urllib-based, executor-thread) GET-side sibling of `cloud.py`'s
async `cloud_put_json`: download Bosch cloud media (event snapshots/clips)
with a Bearer token over a caller-supplied verifying SSL context. Kept
synchronous (not aiohttp) to match the source integration's existing usage
from `run_in_executor` callbacks (SMB/FTP transfer loops), the same style
already established by this library's own `local_rcp.rcp_read_local_sync` /
`rcp_read_remote_sync`.

`ftp_connect` / `ftp_exists` / `ftp_makedirs` — plain-FTP protocol mechanics
(passive-mode connect/login, existence check, recursive mkdir) with no
caller-specific state — every parameter is explicit (server/credentials/
connection/path), nothing reads a coordinator or HA config entry.

Deliberately NOT included here: SMB (`smbclient`/`smbprotocol`) mechanics.
Unlike FTP (stdlib `ftplib`), `smbclient` is an optional third-party
dependency of the *source integration* specifically (a `manifest.json`
requirement) — pulling it into this library would add a real dependency
surface for every consumer, not just the SMB-upload feature. The SMB-specific
functions (`smb_makedirs`, the transfer loops) stay in the source repo's
`smb.py`.
"""

from __future__ import annotations

import re
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlparse

# ── URL allowlist for image/video downloads (SSRF prevention) ────────────────
DEFAULT_SAFE_DOMAINS = frozenset({".boschsecurity.com", ".bosch.com"})


def is_safe_bosch_url(
    url: str, safe_domains: frozenset[str] = DEFAULT_SAFE_DOMAINS
) -> bool:
    """Validate that a URL points to a known Bosch domain (HTTPS only).

    ``urlparse`` can raise ``ValueError`` on malformed input (unmatched IPv6
    brackets, invalid NFKC-normalized netloc) — fail closed rather than let
    it propagate to callers that don't expect it.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and any(parsed.hostname.endswith(d) for d in safe_domains)
    )


def sanitize_filename(name: str) -> str:
    """Sanitize a name for use as a directory/file name component.

    Removes path traversal sequences and non-safe characters, truncates to
    64 chars.
    """
    return re.sub(r"[^\w\-. ]", "_", name.replace("..", "_"))[:64]


def sync_http_get(
    url: str, token: str, ssl_context: ssl.SSLContext, timeout: int = 60
) -> tuple[int, bytes]:
    """Download *url* using stdlib urllib (no third-party dependency).

    Returns (status_code, body_bytes). For streaming downloads use
    `sync_http_get_chunked` instead so memory stays bounded.
    Raises `urllib.error.URLError` / `OSError` on network failure. Caller
    must validate `url` via `is_safe_bosch_url` first (SSRF prevention) —
    this function does not validate on its own.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url
    with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as r:  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url
        return r.status, r.read()


def sync_http_get_to_file(
    url: str,
    token: str,
    dest_path: str,
    ssl_context: ssl.SSLContext,
    timeout: int = 60,
) -> bool:
    """Stream *url* content to *dest_path* (parent dir must already exist).

    Returns True on success (HTTP 200), False on non-200.
    Raises `urllib.error.URLError` / `OSError` on network failure. Caller
    must validate `url` via `is_safe_bosch_url` first.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url
    with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as r:  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url
        if r.status != 200:
            return False
        with open(dest_path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    return True


def sync_http_get_chunked(
    url: str, token: str, ssl_context: ssl.SSLContext, timeout: int = 60
) -> Any:
    """Context manager yielding raw urllib response for chunked reads.

    Usage::
        with sync_http_get_chunked(url, token, ssl_context) as r:
            if r.status == 200:
                while chunk := r.read(65536):
                    ...

    Caller must validate `url` via `is_safe_bosch_url` first.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url
    return urllib.request.urlopen(req, context=ssl_context, timeout=timeout)  # noqa: S310 # Bosch cloud URL, https+bearer, caller must validate via is_safe_bosch_url


def ftp_connect(server: str, username: str, password: str, timeout: int = 30) -> Any:
    """Open a passive-mode FTP connection. Caller closes via `.quit()`."""
    import ftplib

    ftp = ftplib.FTP(server, timeout=timeout)  # noqa: S321 # FTP is a deliberately configurable upload-target option
    ftp.login(username, password)
    ftp.set_pasv(True)
    return ftp


def ftp_exists(ftp: Any, path: str) -> bool:
    """Return True if *path* exists on the connected FTP server."""
    import ftplib

    try:
        ftp.size(path)
        return True
    except ftplib.error_perm:
        return False
    except Exception:  # any FTP/network hiccup here means "treat as not-existing"
        return False


def ftp_makedirs(ftp: Any, path: str) -> None:
    """Create FTP directories recursively, ignoring already-exists errors."""
    import ftplib

    parts = [p for p in path.split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # already exists or permission — ignore
