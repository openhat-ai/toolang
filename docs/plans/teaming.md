# Teaming

Approved architecture for messaging and team observation.

The [agent Hub transport contract](agent-hub-transport.md) defines messaging,
presence, and export over Hub HTTP APIs. Only Hub connects to Redis/Valkey;
local execution remains independent.

## Goal and delivery

Single-agent execution and multi-client subscriptions work without Redis/Valkey.
Optional teaming adds messaging and cross-agent observation through shared
services. `coord` is reserved; its operations are outside this delivery.

| Capability | Contract |
| --- | --- |
| Messaging | Typed participants, direct/group conversations, membership, messages, and `msg` tools, defined below. |
| Talk | [Conversation interface](talk-status-bar.md): message following, input, footer, titles, and tmux placement. |
| Hub transport | [Agent Hub transport](agent-hub-transport.md): human and agent HTTP routes; Hub owns backend access. |
| Subscriptions | [Canonical subscriptions](local-subscriptions.md): cursors, caches, prefill, and recovery. |
| Observation | [Team observation](team-observation.md): event export, Hub subscriptions, and activity views. |

## Ownership

Paths are relative to `src/toolang/`; create modules only when needed.

| Owner | Responsibility |
| --- | --- |
| `teaming/backend/` | Independent storage protocols and factory; services consume `Backend`, `EventBackend`, and `ActivityBackend`. |
| `teaming/backend/valkey/` | Concrete connection, keys, commands, scripts, and driver error translation. |
| `teaming/messaging.py` | Targets, conversations, membership, messages, history, receipts. |
| `teaming/coord.py` | Reserved coordination boundary. |
| `teaming/events.py` | Cross-agent event import/export. |
| `teaming/config.py` | Separate root/home types; no environment, file, or driver dependencies. |
| `teaming/schemas.py`, `types.py`, `errors.py` | Wire records, vocabulary, package errors; no runtime dependencies. |
| `teaming/api.py`, `client.py` | Hub HTTP app/routes and client. |
| `common/pubsub.py` | Generic local fanout; independent of execution, teaming, and HTTP. |
| `api/`, `execution/events.py` | Existing agent API and canonical execution events. |
| `setup/`, `up/`, `work/`, `cli/` | Source resolution, hosting, execution scheduling, process/command orchestration. |

CLI/toolsets/API use shared services, never raw backend clients or vendor errors.
Keep one driver implementation for Redis and Valkey. Reuse existing factories and
entry points. Execution-specific tracing/serialization stays outside `common`.

## Configuration and ports

Root `<root>/config.toml` (normally `~/.toolang/config.toml`):

```toml
[teaming]
human = "brice"

[teaming.backend]
url = "redis://localhost:6379/0"

[teaming.hub]
port = 7000
```

Home `<agent-home>/config.toml`:

```toml
[teaming]
enabled = true
```

| Setting | Scope | Default |
| --- | --- | --- |
| `teaming.human` | Root | OS username, resolved by setup. |
| `teaming.backend.url` | Root | `redis://localhost:6379/0`. |
| `teaming.hub.port` | Root | `7000`, a visual mnemonic for `too0`. |
| `teaming.enabled` | Home | `true`; set `false` to disable backend participation. |
| `api.port` | Home; hosting-owned | Recorded agent port, otherwise next available `7001`–`7999`. |

No root enable switch or configured conversation/member lists. Parse scopes separately,
reject unknown/misplaced fields with their source, and prevent script projections
from supplying root settings. Core services receive concrete values. Changes
require restart.

Both `start` and `serve` resolve ports as **`--port` > environment > scoped config
> default selection**. Environments are `TOOLANG_HUB_PORT` and
`TOOLANG_AGENT_PORT`, respectively. Overrides beat recorded ports, do not rewrite
configuration, and publish the actual endpoint. Invalid ports or conflicts on
explicitly selected ports fail; Hub never silently changes port. Valid explicit
ports are `1..65535`. Temporary agent port selection is unchanged.

## Hub transport

One Hub per root binds `127.0.0.1`. Remote access/authentication is outside this
delivery. Its private `.runtime/hub.json` records PID/creation time, endpoint,
human, backend identity, and status. Talk discovers this
record; it never guesses a port or starts Hub implicitly. Configuration changes
require restart. Lifecycle commands verify process identity and never manage
agents or Redis/Valkey. A process lock prevents concurrent Hubs for the same root.

Startup publishes a `starting` record before backend access, so status/stop remain
available while registration is pending; readiness changes it to `running`.
Startup registers the configured human and requires backend readiness. Each
request idempotently restores that human and system membership if
the backend restarted empty; message writes are never retried.
`GET /healthz` checks current backend availability. Messaging routes under `/msg`
delegate to the existing service as that human; requests cannot supply an actor
or agent origin. No agent execution routes are mounted.

| Method/path | Result |
| --- | --- |
| `GET /msg/targets`, `/msg/agents`, `/msg/conversations` | Targets, ownership, conversation directory. |
| `POST /msg/resolve` | Resolve Talk shorthand to a canonical conversation. |
| `GET /msg/conversations/{conversation}` | Conversation metadata and members. |
| `POST /msg/conversations` | Create a GC. |
| `PUT/DELETE /msg/conversations/{conversation}/participants` | Join/leave as the configured human. |
| `GET /msg/conversations/{conversation}/messages` | Forward reads (`after`) or recent history; count `1..1000`. |
| `GET /msg/conversations/{conversation}/cursor?after=…` | Retention-gap notice or invalid future cursor. |
| `POST /msg/messages` | Send with a caller-preallocated UUID; return the existing receipt. |

History rows carry `stream_id` and raw message JSON (`data`, nullable for a corrupt
record), preserving cursor advancement past malformed records. Service errors
carry `code`/`detail`: invalid operations `400`, backend outages `503`, uncertain
sends `502`; backend identity mismatches return `409`. Clients never retry writes.
A lost send response reports its UUID for manual reconciliation; this is not an
idempotency guarantee.

Acceptance adds HTTP/service parity (including Unicode, membership, corrupt rows,
and uncertainty), actor isolation, empty-backend recovery, unavailable-backend
startup, stop during startup, concurrent start, stale PID safety, stop isolation,
actual-port discovery, and CLI/environment/config precedence. Tests use an
in-memory backend by default and isolated optional
Redis/Valkey processes for lifecycle/Talk smoke tests.

## Targets, conversations, and storage

The [conversation contract](conversation-ids.md) owns canonical `dm_`/`gc_` IDs,
record schemas, keys, membership, names, presence, statistics, and team events.
[Messaging](../messaging.md) documents current commands and public APIs. Use typed
`agent:<name>` and `human:<name>` identities; conversation names are mutable,
non-unique labels. Talk resolves positional agent names/pairs or canonical IDs.

`P:team` is the global member directory. `P:roster` is its optional agent-only
root ownership/discovery subset, with `{root, managed, missing}` records; it is
neither another member directory nor a presence source. Hub scans resident homes
at startup and every five seconds. Two successful absent scans and no live lease
allow atomic removal of this root's managed roster/team entry and GC memberships.
Preserve DM membership and history. Transient and unscoped registrations do not
undergo discovery cleanup. See [storage](../messaging.md#storage-events-and-statistics).

Python owns protocol validation. Lua owns atomic authority checks and combined
storage operations. Validate records before mutation and compare the read values
at commit; retry changed snapshots only before writes. Backend/transport failures
never automatically replay an uncertain message append. No compatibility adapter
or migration is included.

## Lifecycle, commands, and tools

Disabled agents make no backend connections and keep local execution/API working.
Enabled agents own registration, presence, consumption, and event export; refresh
membership between batches. Messaging and event export share one teaming lifecycle
and lease, rather than registering competing identities. Backend reconnects do
not block local execution.
Hub checks the externally managed backend before readiness; starting/stopping Hub
neither enables/stops agents nor manages Redis/Valkey.

`too` is the alias for `toolang`:

| Command | Contract |
| --- | --- |
| `too hub start/serve` | Background/foreground Hub. |
| `too hub stop/status` | Stop/report Hub only. |
| `too list` | Existing local agent listing; no `--team`. |
| `too talk` | Conversation directory, members, presence, latest preview. |
| `too talk TARGET [MESSAGE...]` | Interactive conversation or confirmed send-and-exit. |
| `too top` | Global activity through Hub. |
| `too AGENT top` | Agent activity through its own API. |

Global commands resolve root configuration; agent API exposes only its own data.
The [Talk contract](talk-status-bar.md) defines interactive presentation and placement.

| Tool | Contract |
| --- | --- |
| `msg/targets()` | Registered members and accessible conversations, with canonical IDs. |
| `msg/send(target, body, in_reply_to?)` | Send using context-derived identity; return receipt. |
| `msg/resolve(target, by_name?)` | Resolve a participant, canonical ID, or explicit conversation name. |
| `msg/create_conversation(name?, participants?)` | Create a GC, or explicitly create/reuse a DM pair. |
| `msg/rename_conversation(conversation, name, revision)` | Rename with the expected revision. |
| `msg/join_conversation(conversation)` | Caller joins a GC. |
| `msg/leave_conversation(conversation)` | Caller leaves a GC. |

Services enforce the same rules for CLI, HTTP, and tools. Conversation management
uses tools and Hub HTTP; no separate CLI management command is introduced.

## Subscriptions and observation

The [canonical subscription protocol](local-subscriptions.md) owns source cursors,
cache watermarks, structural prefill, parallel-run handling, and HTTP recovery.
Local agent and Hub use the same boundary rules: select a stable boundary, fill
missing structure/final results from its records view, then follow the suffix.
Use the execution-owned normalizer with a backend reader; do not copy its state
machine into teaming or HTTP. Backend positions order delivery; source cursors
identify record incarnations.

```text
agent canonical stream -> teaming exporter -> Hub API -> backend Stream -> Hub clients
```

`teaming/events.py` owns the asynchronous exporter, registered before agent work
starts; `teaming/backend/valkey/` remains the only driver boundary. Export locally
originated canonical events once, preserving actual run IDs and source cursors.
Client-specific prefill is not republished as new canonical execution. Hub imports
never re-export, and disabling teaming removes only this optional outlet.

Team observation uses one shared event Stream under `P:events:*`, with agent/thread/root
filters rather than separate copies for each scope. Entries carry origin agent,
`runtime_epoch`, `seq`, thread/root routing context, and the canonical event.
Use [XADD](https://valkey.io/commands/xadd/) and independent
[XREAD](https://valkey.io/commands/xread/) cursors, not competing consumer groups
or ephemeral Pub/Sub. Hub can read backend history and block for live events
without another mandatory in-memory fanout layer.

| Position | Contract |
| --- | --- |
| Source cursor | Preserved agent/epoch/sequence identity, ordered within its agent. |
| Backend Stream ID | Hub delivery/resume position across agents; reception order, not global causality. |

Backend structure projections retain the source Begin/End cursors and corresponding
backend positions needed to locate prefill. Append and projection updates are
atomic; the boundary's records view must remain consistent while it is read.
Persist enough structural/final data for Hub prefill without a live origin agent.
This shared algorithm does not make different agents' source sequences comparable.

Append canonical `RunRetried` and apply its persisted invalidation set atomically,
before replacement events. Deduplicate by source/control identity before changing
projections; repeated mutations cannot delete newer reused paths. Retain enough
mutation metadata to select affected trees for records recovery. The shared
normalizer handles live/cached mutations and current-tree replacement prefill;
Begin/End upserts alone cannot represent deleted rows. Its checkpoint uses backend
boundary `B`, never a reconstructed event's source cursor.

Retained exporter backlog resumes in source order. Uncertain writes require
source-identity deduplication and lease fencing before retry. Filtering/compacted
delta holes are legal; sequence discontinuity alone is not proof of loss. Buffer
overflow, lost source history, or an empty restarted backend requires explicit
resynchronization from agent records. Missing deltas are disposable; missing
structure/final results must be repaired before claiming continuous observation.
Recovery markers are transport control, not invented source execution events;
Hub marks stale/incomplete views until recovery establishes a new boundary.

The approved [team observation contract](team-observation.md) defines backend schemas,
projection versioning, retention, recovery transactions, Hub routes/envelopes,
and activity presentation. Backend retention is separate
from the local cache and messaging retention; coordination remains outside scope.

## Acceptance and touchpoints

| Area | Required checks |
| --- | --- |
| Messaging | Scope errors; disabled agent makes no backend calls; readable typed identities; ambiguous targets; direct-pair uniqueness; participant permissions; context-derived sender. |
| Storage | Concurrent creation; stale lease rejection; persistent membership; independent readers; full cursor order/gaps; uncertain writes; no vendor imports outside backend. |
| CLI/tools | Talk directory and conversation interface, literal messages, and the `msg` conversation tools. |
| Hub | Readiness failure, lifecycle isolation, port precedence/conflicts/discovery, Talk HTTP parity. |
| Local subscriptions | Canonical cursor/prefill/cache acceptance in the linked plan; no backend required. |
| Team observation | Run the shared normalization scenarios against the backend reader; verify multiple clients, source order, lease fencing, duplicate rejection, outage recovery, no forwarding loops, local/global `top`. |

Likely files: new `teaming/`, existing `api/common.py`, setup watcher/types,
`up/server.py`, `work/messaging.py`, toolset factory/context, CLI routing/Talk,
and corresponding tests. Keep default tests offline; run the same backend contract
against Redis and Valkey separately. Follow [repository verification](../../AGENTS.md#verification).
