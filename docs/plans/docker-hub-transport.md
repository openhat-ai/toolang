# Docker agent access to the host Hub

Status: proposed; requires explicit human approval before implementation.

## Goal and evidence

Docker agents with teaming enabled must acquire a lease, process Talk messages,
use conversation tools, and export execution events through their selected host
Hub on supported macOS and Linux hosts.

Current Docker agents have no mounted Hub discovery. Their clients look under
the guest place, while host discovery advertises loopback. Both reported Docker
agents reproduce `BackendUnavailable`; the host Hub has no live lease for either.
`too hub` reports driver selection, not transport health. The approved
[Hub transport plan](agent-hub-transport.md) excludes sandbox networking and
requires a separate approved connection contract.

## Scope

- Support locally managed Docker agents and the existing local Hub lifecycle.
- Preserve the human API's loopback listener, backend ownership, root isolation,
  lease fencing, delivery semantics, and explicit teaming/Talk opt-outs.
- Keep current public commands and `too hub` status meanings. Explain enabled
  versus connected in the messaging documentation.
- Exclude remote Docker engines, remote Hub deployment, host networking mode,
  direct backend access, and unrelated mount cleanup.

## Connection and authorization

1. Hub owns a second, agent-only HTTP listener on `0.0.0.0` with an OS-assigned
   port. The human listener remains `127.0.0.1`. The guest listener exposes only
   existing `/agents/{agent}/...` operations and authenticated health checks;
   human routes are absent. Reuse the existing agent handlers and backend
   instance rather than proxying arbitrary URLs or duplicating operations.
2. Every guest request requires a random 256-bit bearer capability bound to one
   canonical host root, complete agent address, actor, and launch ID. Validate it
   before backend access, including initial lease registration. Existing lease
   headers and backend/human identity checks remain independent requirements.
   Reject missing, forged, expired, other-root, and other-agent capabilities.
3. Host orchestration prepares a mode-`0600` authorization record under
   `ROOT/.runtime/guest-hub/<address>/<launch-id>.json`. It contains the capability
   and launch binding; this directory is never mounted into a guest. Hub checks
   the record against the authoritative runtime generation. Allow startup and
   final drain for that generation; terminal or replaced launches cannot renew
   authorization. Compare capabilities in constant time and never log them.
4. Hub publishes only connection metadata, readiness, human, and backend identity
   under `ROOT/hub/guest/discovery.json`. No PID, backend credentials, or guest
   capability appears there. Mount the containing `guest/` directory read-only
   at `HOME/.runtime/hub` so atomic replacement remains visible on Linux.
   Mount no Hub log, process lock, backend files, or other agent authorization.
5. Put only that launch's capability in its private mode-`0600` staging file,
   exposed through the existing read-only bootstrap mount. Resolve discovery and
   credential file paths at the hosting boundary; pass concrete connection
   providers to the lifecycle and conversation tool factory. Never reinterpret
   the guest filesystem path as the canonical host root.
6. Docker resolves the listener through its existing `host.docker.internal`
   host-gateway mapping. The sandbox supplies this routing choice explicitly;
   shared clients do not inspect environment variables or guess a gateway.
   Host clients continue using ordinary local discovery.

The new authenticated listener is a deliberate network-boundary change requiring
human approval. Its capability protects agent authority; it does not add remote
deployment or TLS support to this local Docker contract.

## Lifecycle and failure

- Create the guest discovery directory before mounting, even without a running
  Hub. Missing/starting discovery means unavailable; local execution still works.
  Neither agent launch nor retries start Hub implicitly.
- Read discovery on the next connection/retry and pin it for a Talk batch using
  the existing session mechanism. A Hub restart or port/backend change must not
  require restarting agents or reuse another backend's cursor.
- Hub listener readiness and shutdown follow Hub lifetime. Bridge failures retain
  the existing communication-unavailable behavior and never stop local runs.
- Retire authorization after bounded final export/unregister on normal shutdown.
  Failed launch and forced cleanup remove only the matching generation; stale
  records cannot authorize traffic after a crash or replacement.
- Keep uncertain-send handling, retry boundaries, event deduplication, cursor
  persistence, and publication limits as defined by the existing Hub contract.

## Implementation touchpoints

- `up/hub.py`, `teaming/api.py`, and a focused guest authorization module: second
  listener, metadata publication, scoped routing, and capability checks.
- `up/sandbox.py`, `up/server.py`, Docker sandbox/bootstrap, and sandbox request
  types: generation-bound credentials, directory mounts, and concrete routing.
- `teaming/discovery.py`, `teaming/agent_client.py`, and `teaming/client.py`:
  explicit guest connection provider and redacted authentication handling.
- `setup/teaming.py`, `setup/watcher.py`, and `plugin/toolsets/convo.py`: share the
  resolved root/connection contract between polling, export, and tools.
- Corresponding unit/integration tests and `docs/design/{hosting,messaging}.md`;
  generate the user-facing changelog through `too aid.too update_changelog`.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| No Hub at launch | Docker agent serves local requests; communication retries; no backend connection or implicit Hub startup occurs. |
| Hub starts later | Existing guest acquires a lease and consumes an isolated test message without restart. |
| macOS and Linux | A real guest registers, handles a local test-provider Talk batch, writes its response, and exports events through the host Hub. |
| Restart and atomic discovery replacement | Existing guests reconnect to the current listener/identity; no stale file bind or old-backend cursor survives. |
| Shared connection | Lifecycle, receive batch, conversation tools, and exporter use the same host root and selected Hub. |
| Authorization | Missing/forged/cross-agent/cross-root/retired credentials and human routes are rejected before backend access. |
| Failure and replacement | Partial startup, forced stop, and replacement revoke only their launch; stale cleanup cannot revoke the new guest. |
| Opt-outs and regression | Teaming-disabled agents stay disconnected; Talk-disabled agents never consume; host transport and current delivery/recovery tests pass unchanged. |

Default tests stay offline using in-process transports and isolated fake storage.
Real Docker plus isolated Redis/Valkey checks are opt-in and use a local model
stub; never send verification messages into user conversations or call a live
model provider. Implementation must pass the repository's default checks.

## Risks and open questions

The additional listener introduces a reachable network surface; authorization
must precede every operation, with no human-route fallback. Lifecycle races and
credential disclosure are addressed by generation checks, private files,
redacted representations, and explicit cleanup tests. Docker gateway behavior
requires validation on both supported host platforms.

No implementation decision remains open. Approval of this listener and
authorization contract is pending; this definition ships no product code.
