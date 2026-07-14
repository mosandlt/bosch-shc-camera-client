# Changelog

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
