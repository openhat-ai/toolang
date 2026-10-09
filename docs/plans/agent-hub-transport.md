# Agent teaming through Hub

Approved amendment to [teaming](teaming.md) and [team observation](team-observation.md).
Approved in chat on 2026-10-09; implement in PR #721.

## Goal and scope

All agents support teaming by default. Agents use Hub HTTP APIs for messaging,
presence, and event export; only Hub accesses Redis/Valkey. Explicit home
`teaming.enabled = false` remains an opt-out. Human users start and stop Hub.

Hub availability never controls agent or executor lifetime. Without Hub, tools
report unavailability and background communication retries; local execution and
subscriptions continue. Do not start Hub automatically or fall back to backend
connections. Coordination and remote Hub deployment remain outside this change.

## Connection and identity

- Reuse the root's private `.runtime/hub.json` endpoint and backend
  identity. Read the current record when connecting/reconnecting; a missing or
  starting record means unavailable. Endpoint changes are picked up on the next
  operation, without restarting the agent. Clients do not validate Hub's PID.
- Keep process validation in `up/hub.py`; share record decoding and connection
  discovery under `teaming`. Setup passes root/discovery configuration to `msg`
  and the lifecycle, never backend credentials or a backend client.
- Assume trusted local callers and retain the loopback listener. Defer security
  authentication; remove Hub bearer tokens and credential refresh. This adds
  no remote binding or sandbox networking configuration. An agent that cannot
  reach this Hub reports unavailability without a bypass.
- Derive the owner from Hub configuration and the actor from the agent route.
  Use the resident process's existing lease token for fenced writes; never accept
  an arbitrary actor/owner
  in message bodies. Human routes retain their current actor and origin rules.

## API and ownership

| Route | Contract |
| --- | --- |
| `PUT /agents/{agent}/lease` | Register the agent's process token and advertised endpoint; idempotent for that token. |
| `PATCH /agents/{agent}/lease` | Renew the matching process token; reject a lost lease. |
| `DELETE /agents/{agent}/lease` | Release only the matching lease. |
| `/agents/{agent}/msg/...` | The existing messaging operations, bound to this agent. Agent sends accept context-derived thread/run origin. |
| `GET /agents/{agent}/events/state` | Return event dataset metadata and this agent's recovery state. |
| `PUT /agents/{agent}/events/staging/{generation}` | Upload a bounded, validated structural snapshot under the matching lease/recovery. |
| `POST /agents/{agent}/events/commits` | Commit an incomplete marker, recovery, or canonical event with existing deduplication and ordering rules. |
| `DELETE /agents/{agent}/events/staging/{generation}` | Fenced cleanup; never remove the active generation. |

Reuse messaging handlers/services through actor dependencies rather than copying
human routes. Add an agent HTTP client and a narrow event-publication interface;
the exporter retains its canonical reader, projection, and recovery algorithm.
Redis keys, scripts, and transactions stay behind Hub. Derive storage keys on the
server; publication requests carry generation IDs, not arbitrary backend keys.
Validate publication kinds, identities, cursors, and existing size/entity budgets
before writes. Preserve current atomic lease checks and deduplication in storage.

## Failure and recovery

- Missing/unreachable Hub or unavailable backend is a communication failure.
  No implicit retries for message sends; an uncertain response reports its message
  UUID. A later read may reconcile it. Never report an unacknowledged send as sent.
- Background registration, heartbeat, and export reconnect with bounded backoff.
  Hub restart must work while agents remain running. An interrupted receive does
  not advance its saved message cursor; in-flight execution is not canceled.
- Keep checkpoints separated by Hub's backend identity. Switching datasets cannot
  reuse another dataset's message cursor. HTTP clients send the discovered
  identity in `X-Toolang-Backend` and the percent-encoded configured human in
  `X-Toolang-Human`; Hub rejects a mismatch with `409 hub_changed` before storage
  access. A receive batch pins that connection. These are configuration consistency
  checks, not authentication; direct local requests may omit them.
  Canonical export keeps its existing event identity, lease fencing, bounded
  backlog, and records-based recovery.
- Event publication may retry the exact operation after an uncertain HTTP result;
  existing deduplication resolves it. Lease loss/reset requires recovery; invalid
  protocol data fails closed. Use `409` for recovery/lease loss, `503` for
  unavailability, `400` for invalid protocol data, and `413` for upload limits;
  preserve `502 send_unconfirmed` for uncertain message writes.
- Preserve bounded agent shutdown and final export drain. Hub shutdown never
  stops agents; unexported final state is recovered when communication resumes
  while the agent is running, or on its next start.

## Acceptance and touchpoints

| Scenario | Pass condition |
| --- | --- |
| No Hub, live backend | Default agent starts, executes, streams locally, and stops without any direct backend access. `msg` reports Hub unavailable. |
| Hub starts after agent | The existing agent registers, consumes messages, and exports without restart. |
| Hub stops/restarts | Local runs continue; polling/export reconnect and recover structure/cursors without credentials. |
| Messaging parity | Human and agent routes preserve membership, origin, uncertain-send handling, and explicit opt-out. |
| Publication faults | Lost responses, stale leases, invalid requests, and oversized snapshots preserve fencing, deduplication, and recovery. |
| Both engines | Isolated Redis and Valkey tests exercise two agents through Hub HTTP APIs and local/Hub subscriptions. |

Touch `teaming` API/client/schemas/discovery and publication interfaces,
`setup/teaming.py`, `plugin/toolsets/msg.py`, `work/{messaging,teaming}.py`, and
`up/{hub,server}.py`, with corresponding tests and concise documentation updates.
Keep backend protocol tests in-process and the default suite offline. Run the
repository's default checks and opt-in isolated Redis/Valkey wire tests.

Risks: HTTP adds publication overhead and uncertain acknowledgments; reuse bounded
payloads and exact-operation deduplication. Current loopback discovery cannot make
a Hub reachable from an isolated guest; remote/sandbox transport needs its own
approved configuration contract.
