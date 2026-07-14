# bosch-shc-camera-client

Async Python client library for Bosch Smart Home Camera cloud + local (RCP) APIs.

Extracted from the [Bosch Smart Home Camera Tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant)
integration family (Home Assistant, ioBroker, Python CLI, MCP, Node-RED, NiceGUI frontend) so every
consumer shares one maintained implementation instead of duplicating the client logic per platform.

Status: early extraction in progress. Not yet feature-complete versus the inline modules it's replacing.

## Scope

- Bosch cloud API (`residential.cbs.boschsecurity.com`): auth, video inputs, motion zones, privacy
  masks, rules, friends/sharing, firmware, lighting.
- Local RCP protocol (Gen2 camera on-device control plane).
- SMB/FTP export helpers for local recording targets.

Out of scope (stays integration-specific, not extracted here): FCM push-notification plumbing tied to
Home Assistant's recorder/snapshot-store internals, and anything HA-entity-shaped.

## Install

```
pip install bosch-shc-camera-client
```

## License

MIT, see [LICENSE](LICENSE).
