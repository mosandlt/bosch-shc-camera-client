# bosch-shc-camera-client

Async Python client library for Bosch Smart Home Camera cloud + local (RCP) APIs.

Extracted from the [Bosch Smart Home Camera Tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant)
integration family (Home Assistant, ioBroker, Python CLI, MCP, Node-RED, NiceGUI frontend) so every
consumer shares one maintained implementation instead of duplicating the client logic per platform.

Status: early extraction in progress. Not yet feature-complete versus the inline modules it's replacing.

## Scope

- Local RCP protocol read access over `/rcp.xml` (Gen2 camera on-device control plane), read-only
  (`local_rcp` module — synchronous, used from an executor thread).
- HTTP Digest authentication (RFC 7616/2617) — `auth_utils.async_digest_request`.
- RCP (Remote Configuration Protocol) via cloud proxy and direct LOCAL (`rcp` module): session
  management with TTL caching, binary protocol reads, direct-LOCAL Gen2 reads/writes (privacy mode,
  front-light brightness), response parsers (alarm catalog, motion zones/coords, TLS cert info,
  network services, IVA analytics catalog), and `fetch_rcp_camera_data()` — a pure per-camera
  orchestrator that reads all of the above in one call and returns an `RcpCameraData` dataclass (only
  fields whose reads succeeded are populated; the rest stay `None`, meaning "not read this round").
  All session/SSL-context objects are caller-injected — this module never builds or caches
  HA-specific process-wide state itself, and never touches any coordinator/cache-dict state.
- Bosch cloud API (`residential.cbs.boschsecurity.com`) write mechanics (`cloud` module):
  `cloud_put_json()` — Bearer-auth PUT with a timeout, HTTP-status classification, optional JSON
  response parsing. This is the one shared piece extracted from the source integration's 5 cloud-setter
  functions (privacy/light/light_component/notifications/pan); deliberately narrow — see "Out of scope"
  below.

Out of scope (stays integration-specific, not extracted here, by deliberate "clean boundary" decision —
see `knowledge-base/ha-core-submission-plan.md` in the source repo): the cloud setters' actual
multi-tier fallback orchestration (cloud → local RCP → SHC local API), coordinator cache writes, and
notification side effects — genuine coordinator business logic, not wire protocol, and threading the
~15 coordinator attributes it touches through a "pure" interface would obscure the boundary rather than
clarify it. Also out of scope: FCM push-notification plumbing tied to Home Assistant's
recorder/snapshot-store internals, anything HA-entity-shaped, and the SMB/FTP export helpers (`smb.py`)
— those reach into the HA integration's coordinator object directly (`coordinator.hass`,
`coordinator.options`, private state) rather than being a standalone API client.

## Install

```
pip install bosch-shc-camera-client
```

## License

MIT, see [LICENSE](LICENSE).
