# Changelog

## [v0.5.6] - 2026-08-05

Add pure per-setter request builders to `cloud.py`, layered on top of the
existing `cloud_put_json`: `cloud_set_privacy_mode`, `cloud_set_camera_light`
(+ `CameraLightSetResult`), `cloud_set_notifications`, `cloud_set_pan`
(+ `PanSetResult`). These are the source integration's 4 named cloud-setter
endpoints (privacy mode, camera light on/off, notifications, pan) minus the
401-retry/token-refresh orchestration, multi-tier fallback cascade (cloud ->
local RCP -> SHC local API), coordinator cache writes, and notification side
effects -- all of which stay in the source integration's `shc.py`, which now
calls these pure functions once (and, only for privacy mode, retries once
after a refreshed token) instead of building the URL/body inline. Exact
endpoint URLs, JSON body shapes, and the Gen2 dual-endpoint
`ok = front.ok or topdown.ok` semantics are unchanged -- verified against the
pre-extraction inline code byte-for-byte. `async_cloud_set_light_component`
(the more complex front/wallwasher/intensity setter, whose Gen2 wallwasher
path depends on multiple coordinator-owned caches to build its body) is
intentionally NOT extracted -- it isn't one of the 4 named setters and its
body-building isn't stateless. New `CLOUD_API` constant added to `cloud.py`,
matching the source integration's own constant of the same name
byte-for-byte. 31 new tests, 100% coverage maintained (410 total), ruff /
mypy --strict / codespell / pylint / pip-audit clean.


## [v0.5.5] - 2026-08-04

Add `tls_proxy` module: the source integration's asyncio-native TCP→TLS proxy
(`asyncio.start_server()`-based) that lets FFmpeg/go2rtc connect to a plaintext
`rtsp://127.0.0.1:<port>` instead of handling the camera's self-signed-cert TLS
+ Digest auth directly. Extracted byte-for-byte — the module had zero
HA/coordinator coupling in the source repo to begin with, so this is a pure
move, not a rewrite. Public API: `start_tls_proxy`/`stop_tls_proxy`/
`stop_all_proxies` (server lifecycle over caller-supplied `port_cache`/
`server_cache` dicts, so a caller's own shutdown path can tear everything down
deterministically), `rtsp_keepalive`/`pre_warm_rtsp`/`confirm_encoder_ready`
(authenticated RTSP OPTIONS/DESCRIBE helpers that keep a session alive past
Bosch's 60s inactivity timeout and warm up the camera's H.264 encoder before
FFmpeg connects).

`[tool.mypy] python_version` bumped 3.12 → 3.13 (matches the source HA
integration's own mypy pin) — `asyncio.Server.close_clients()` (used by the
proxy's stop/circuit-breaker cleanup paths, Python 3.13+ stdlib) has no
typeshed stub before 3.13. This only tightens this repo's own type-check
target; `requires-python` stays `>=3.11` since importing the package (or any
module besides `tls_proxy`) is unaffected — only actually calling
`start_tls_proxy`/`stop_tls_proxy` needs a 3.13+ interpreter at runtime, same
as the HA integration itself already requires.

96 new tests (ported from the source repo's `tests/test_tls_proxy.py`, plus
one added here to independently reach 100% branch coverage in this repo's own
isolated suite — the source repo's equivalent branch, the debug-preview skip
for `$`-prefixed RTP-interleaved frames, was covered there by a *different*,
integration/chaos-facing test file that stayed behind), 100% line+branch
coverage maintained across all 6 modules.

## [v0.5.4] - 2026-07-14

Two fixes found by an independent bug-hunt pass on the source integration's rewired cloud setters:

- `cloud_put_json`'s internal failure log dropped from WARNING to DEBUG. Every caller in the source
  integration already logs its own WARNING with richer context (camera id, which of several parallel
  endpoints failed, etc.) once it sees `ok=False` — the library logging its own WARNING for the same
  failure was pure duplicate noise, doubling every failed-write log line.
- `CloudPutResult.body` is now genuinely guaranteed `dict | None`, not just by type-hint convention — a
  non-object JSON response (e.g. a bare array) is now discarded instead of stored as-is. Closes a latent
  `AttributeError` risk: a caller doing `result.body.get(...)` (e.g. the pan setter) would have crashed
  on a non-dict body with no `isinstance` guard of its own.

3 new tests, 100% line+branch coverage maintained across all 5 modules.

## [v0.5.3] - 2026-07-14

Fixed `cloud_put_json`'s JSON-parse attempt: was gated on `status == 200` specifically, but the source
integration's Gen2 wallwasher light-component setter parses a JSON response on 201 too (some Bosch
`lighting/switch` responses land on 201). Now attempts the parse for any ok status (200/201/204) — a
204's genuinely-empty body still just fails to parse and falls back to `None`, matching every existing
caller's expectation either way; this only changes behavior for a 201 that actually carries a body,
which was silently dropped before.

3 new tests, 100% line+branch coverage maintained across all 5 modules.

## [v0.5.2] - 2026-07-14

`CloudPutResult` gains a `text` field: the raw response body text, captured whenever a real HTTP
response is received (any status, not just success). Needed for `shc.py`'s Gen1 light-component setter,
which logs Bosch's own error-response body on a 400 (it names exactly which field was rejected, e.g.
`frontIlluminatorIntensity must not be set if frontLightOn is false`) — losing that on extraction would
have been a real diagnostic regression, not just a refactor.

6 new tests, 100% line+branch coverage maintained across all 5 modules.

## [v0.5.1] - 2026-07-14

Added `cloud.py`: `cloud_put_json()`, the single shared HTTP pattern duplicated across the source
integration's 5 cloud-setter functions (privacy/light/light_component/notifications/pan) — build
Bearer-auth headers, PUT a JSON body with a timeout, classify the HTTP status (200/201/204 = ok),
optionally parse a 200 response body. Returns a `CloudPutResult` dataclass. Deliberately narrow scope:
the actual multi-tier fallback orchestration (cloud → local RCP → SHC local API), coordinator cache
writes, and notification side effects are NOT extracted — that's genuinely coordinator-specific business
logic, not wire protocol, and stays in the source integration (see
`knowledge-base/ha-core-submission-plan.md` for the "clean boundary" scoping decision).

29 new tests, 100% line+branch coverage maintained across all 5 modules.

## [v0.5.0] - 2026-07-14

Added `RcpCameraData` (dataclass) and `fetch_rcp_camera_data()` to `rcp.py` — the RCP
camera-data-fetch orchestration (LED dimmer, privacy state, clock offset, LAN IP, product name,
bitrate ladder, alarm catalog, motion zones/coords, TLS cert, network services, IVA analytics
catalog), refactored out of the source integration's `async_update_rcp_data` as a pure function:
takes an already-open session/ssl-context and a per-camera failure-counter dict, returns a
`RcpCameraData` with only the fields whose reads succeeded populated (others `None`, meaning "not
read this round" — the caller decides what to do with that, e.g. leave its own cache untouched).
No coordinator/cache-dict coupling at all now; the source integration's `async_update_rcp_data`
becomes a thin wrapper that builds the session/ssl-context and merges the result into its own 11
cache dicts.

105 new tests, 100% line+branch coverage maintained across all 4 modules.

## [v0.4.0] - 2026-07-14

Extracted the RCP (Remote Configuration Protocol) session/protocol layer from `rcp.py`: cloud-proxy
session management (`get_cached_rcp_session`, `rcp_session`, `rcp_read`), direct-LOCAL Gen2 RCP
(`rcp_local_read`, `rcp_local_write`, `rcp_local_read_privacy`, `rcp_local_write_privacy`,
`rcp_local_write_front_light`), and all 6 binary-response parsers (`_parse_alarm_catalog`,
`_parse_motion_zones`, `_parse_motion_coords`, `_parse_tls_cert`, `_parse_network_services`,
`_parse_iva_catalog`) plus `_is_xml_envelope`. Functions that took `hass`/`coordinator` purely to reach a
session or SSL context now take `aiohttp.ClientSession`/`ssl.SSLContext` directly. Not extracted:
`async_update_rcp_data`, the coordinator-cache-writing orchestration (11 cache dicts + a failure
counter) — genuinely HA-integration-specific, stays in the source repo (tracked as a separate
cache-redesign task).

Added a `[[tool.mypy.overrides]]` for `cryptography.*` (optional, lazily-imported dependency for
`_parse_tls_cert`'s DER-certificate parsing, with a raw-hex fallback when absent) so mypy behavior is
deterministic regardless of whether the package happens to be installed — CI never installs it.

212 new tests (`tests/test_rcp.py`), 100% line+branch coverage maintained across all 4 modules.

## [v0.3.0] - 2026-07-14

Extracted `auth_utils.py` (HTTP Digest authentication, RFC 7616/2617) from the Home Assistant
integration — verified zero coupling to `hass`/coordinator (already took `session:
aiohttp.ClientSession` as a plain parameter in the source repo). Ported `tests/test_auth_utils.py`
(65 tests) with the source repo's HA-avoidance import-shim removed (no longer needed — this package has
no HA dependency to avoid). Minor cleanup: `_build_digest_header`'s local `from urllib.parse import
urlparse` moved to the module top level (no functional change). 100% coverage maintained across all
3 modules.

## [v0.1.0] - 2026-07-14

Initial extraction: `local_rcp.py` only, copied from the Home Assistant integration (zero internal
coupling to the integration package, read-only local RCP+ over `/rcp.xml`).

`smb.py` was evaluated but does not qualify: it reaches into the source integration's coordinator
object directly (`coordinator.hass`, `coordinator.options`, a private `_download_started_at`), so it's
integration-specific, not a standalone API client — stays in the HACS repo alongside `fcm.py`.

`shc.py` and `rcp.py` (the cloud API surface) follow once decoupled from
`cloud_ssl.async_get_bosch_cloud_session`/`hass` via session injection (tracked separately).

## [v0.2.0] - 2026-07-14

CI/tooling parity with the source HACS repo: same ruff select (incl. bandit `S` rules), mypy --strict
config, pylint rcfile, pip-audit gate, and a 100%-coverage pytest gate as its own CI job. Ported the
source repo's `test_local_rcp.py` (29 tests) so the coverage bar is met with real tests, not a smoke
test. No functional/API changes.
