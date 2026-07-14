# Changelog

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
