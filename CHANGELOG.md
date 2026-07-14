# Changelog

## [v0.1.0] - 2026-07-14

Initial extraction: `smb.py` and `local_rcp.py`, copied verbatim from the Home Assistant integration
(zero internal coupling to the integration package). `shc.py` and `rcp.py` follow once decoupled from
`cloud_ssl.async_get_bosch_cloud_session` (session-injection refactor, tracked separately).
