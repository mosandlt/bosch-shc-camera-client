# bosch-shc-camera-client

Async Python client library for Bosch Smart Home Camera cloud + local (RCP) APIs.

Extracted from the [Bosch Smart Home Camera Tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant)
integration family (Home Assistant, ioBroker, Python CLI, MCP, Node-RED, NiceGUI frontend) so every
consumer shares one maintained implementation instead of duplicating the client logic per platform.

Status: early extraction in progress. Not yet feature-complete versus the inline modules it's replacing.

## Scope

- Local RCP protocol read access over `/rcp.xml` (Gen2 camera on-device control plane), read-only.
- Bosch cloud API (`residential.cbs.boschsecurity.com`): auth, video inputs, motion zones, privacy
  masks, rules, friends/sharing, firmware, lighting — **planned**, not yet extracted (`shc.py`/`rcp.py`
  in the source integration are coupled to a `hass`/coordinator-provided `aiohttp.ClientSession` and
  need a session-injection refactor before they can move here cleanly).

Out of scope (stays integration-specific, not extracted here): FCM push-notification plumbing tied to
Home Assistant's recorder/snapshot-store internals, anything HA-entity-shaped, and the SMB/FTP export
helpers (`smb.py`) — those reach into the HA integration's coordinator object directly
(`coordinator.hass`, `coordinator.options`, private state) rather than being a standalone API client, so
they don't belong in a generic library without their own decoupling pass first.

## Install

```
pip install bosch-shc-camera-client
```

## License

MIT, see [LICENSE](LICENSE).
