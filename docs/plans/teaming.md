# Teaming

Approved architecture; implement in the delivery order below. Earlier messaging
was experimental: no data migration or compatibility aliases are required.

## Goal and delivery

Single-agent execution and multi-client subscriptions work without Redis/Valkey.
Optional teaming adds messaging and cross-agent observation through shared
services. `coord` is reserved; its operations are outside this delivery.

| PR | Outcome | Dependencies |
| --- | --- | --- |
| 1. Design | Record these contracts and the acceptance criteria. | None. |
| 2. [#708](https://github.com/openhat-ai/toolang/pull/708), revised | Deliver `teaming` messaging, typed targets, scoped setup, backend membership, `msg`, and Text. Replace `team` with bare `text`. | Design. |
| 3. Hub | Add Hub API/client, `hub start/serve/stop/status`, port overrides, and route Text through Hub. | Revised #708. |
| 4. [Canonical subscriptions](local-subscriptions.md) | Unify resident CLI runtime ownership; persist canonical cursors; add shared caching and agent/thread/root-run catchup. | Design and remaining local protocol decisions. |
| 5. Team observation | Bridge agent events through the backend; add global subscriptions and `top`/agent `top`. | Hub and local subscriptions. |

PR #708 is reused, not replaced by a parallel messaging implementation. Its Text
commands may call the shared messaging service until the Hub PR replaces that
adapter. Each implementation PR updates documentation and its acceptance tests;
subsequent PRs define their remaining protocol details before implementation.

## Ownership

Paths are relative to `src/toolang/`; create modules only when needed.

| Owner | Responsibility |
| --- | --- |
| `teaming/backend.py` | Sole Redis/Valkey driver boundary: connections, keys, commands, scripts, errors. |
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
| `teaming.enabled` | Home | `false`. |
| `api.port` | Home; hosting-owned | Recorded agent port, otherwise next available `7001`–`7999`. |

No root enable switch or configured group/member lists. Parse scopes separately,
reject unknown/misplaced fields with their source, and prevent script projections
from supplying root settings. Core services receive concrete values. Changes
require restart; experimental `[human]`/`[messaging]` settings are not migrated.

Both `start` and `serve` resolve ports as **`--port` > environment > scoped config
> default selection**. Environments are `TOOLANG_HUB_PORT` and
`TOOLANG_AGENT_PORT`, respectively. Overrides beat recorded ports, do not rewrite
configuration, and publish the actual endpoint. Invalid ports or conflicts on
explicitly selected ports fail; Hub never silently changes port. Valid explicit
ports are `1..65535`. Temporary agent port selection is unchanged.

## Hub transport

One Hub per root binds `127.0.0.1`. Remote access/authentication is outside this
delivery. Its private `.runtime/hub.json` records PID/creation time, endpoint,
human, backend identity, status, and a generated bearer token. Text discovers this
record; it never guesses a port or starts Hub implicitly. Configuration changes
require restart. Lifecycle commands verify process identity and never manage
agents or Redis/Valkey. A process lock prevents concurrent Hubs for the same root.

Startup publishes a `starting` record before backend access, so status/stop remain
available while registration is pending; readiness changes it to `running`.
Startup registers the configured human and requires backend readiness. Each
authenticated request idempotently restores that human and system membership if
the backend restarted empty; message writes are never retried. Authenticated
`GET /healthz` checks current backend availability. Messaging routes under `/msg`
delegate to the existing service as that human; requests cannot supply an actor
or agent origin. No agent execution routes are mounted.

| Method/path | Result |
| --- | --- |
| `GET /msg/targets`, `/msg/agents`, `/msg/groups` | Targets, ownership, conversation directory. |
| `POST /msg/resolve` | Resolve Text shorthand to a canonical conversation. |
| `GET /msg/groups/{group}` | Conversation metadata and members. |
| `POST /msg/groups` | Create a custom group. |
| `PUT/DELETE /msg/groups/{group}/membership` | Join/leave as the configured human. |
| `GET /msg/groups/{group}/messages` | Forward reads (`after`) or recent history; count `1..1000`. |
| `GET /msg/groups/{group}/cursor?after=…` | Retention-gap notice or invalid future cursor. |
| `POST /msg/messages` | Send with a caller-preallocated UUID; return the existing receipt. |

History rows carry `stream_id` and raw message JSON (`data`, nullable for a corrupt
record), preserving cursor advancement past malformed records. Service errors
carry `code`/`detail`: invalid operations `400`, backend outages `503`, uncertain
sends `502`; authentication failures return `401`. Clients never retry writes.
A lost send response reports its UUID for manual reconciliation; this is not an
idempotency guarantee.

Acceptance adds HTTP/service parity (including Unicode, membership, corrupt rows,
and uncertainty), token/actor isolation, empty-backend recovery, unavailable-backend
startup, stop during startup, concurrent start, stale PID safety, stop isolation,
actual-port discovery, and CLI/environment/config precedence. Tests use an
in-memory backend by default and isolated optional
Redis/Valkey processes for lifecycle/Text smoke tests.

## Targets and conversations

Canonical targets are `agent:alice`, `human:brice`, and `group:dev`. Identities are
case-sensitive and scoped to the configured backend domain; same-name agents and
humans are distinct. Use typed identities for sender, owner, and membership.

IDs begin with a Unicode letter/number, followed by letters, combining marks,
numbers, or `-_.`. Reject whitespace, controls, `:`, `/`, and `%`; do not silently
normalize names. JSON/CLI retain readable Unicode, e.g. `group:后端开发`.
URL escaping belongs only to HTTP transport. Display names may be free-form.
CLI bare names require an unambiguous match; bare `all` means `group:all`.
Tools/API use canonical targets returned by `msg/targets`.

All conversations use `group:<id>` and the same storage. Metadata distinguishes:

- `direct`: exactly two immutable participants. An agent/human target resolves
  the caller's pair, independent of ownership. Generate one stable group ID per
  unordered pair; concurrent/reversed requests reuse it. Reject self-targets.
  Show participant labels, not opaque IDs, as the primary title.
- `group`: mutable membership and a stable readable ID. `group:all` is the
  system-managed public group, not another kind or a send-time recipient expansion.

Group creation includes its creator; join/leave operate on the caller only.
Custom groups are joinable by registered participants; sending requires membership.
Direct/system memberships cannot be edited by these operations. Human observers
may read direct conversations but only their participants may send. Agent reads
are restricted to joined groups. Do not create unused direct conversations.

## Backend records

One standalone Redis/Valkey database per domain. Prefix `P = too:teaming:v1`;
Cluster support is outside scope. Keys/values use UTF-8; `A`/`G` below are raw
validated agent/group IDs. All non-lease records have no application TTL.

| Key | Type | Field/value |
| --- | --- | --- |
| `P:participants` | Hash | Full participant target → participant JSON. |
| `P:agent:A:online` | Hash | `token`, `endpoint`; whole-key TTL 30s, renewed every 10s. |
| `P:msg:groups` | Hash | Full group target → group JSON. |
| `P:msg:group:G:members` | Set | Full participant targets; sole authoritative membership. |
| `P:msg:group:G:messages` | Stream | Server Stream ID; `data` field contains message JSON. |
| `P:msg:direct` | Hash | Canonical participant-pair JSON → full group target. |

Participant JSON (`owner` is null for humans):

```json
{"display_name":"Alice","owner":"human:brice","created_at":"2026-10-08T09:00:00Z"}
```

Group JSON (`display_name` may be null for direct groups; only `all` has
`system=true` and `created_by=null`):

```json
{"kind":"group","display_name":"Development","created_by":"human:brice","created_at":"2026-10-08T09:00:00Z","system":false}
```

Message JSON:

```json
{"id":"8799b495-8f28-4b14-b12d-9b3ed189c718","sender":"agent:alice","body":"Ready for review.","in_reply_to":null,"origin":{"thread":"term_example","run":"run_example"}}
```

Message IDs are preallocated UUIDs; replies reference optional UUIDs even after
retention trims history. Body is nonblank text up to 256 KiB UTF-8. Human origin
is null; agent thread/run IDs come from context and may be null. Receipts contain
`group`, `stream_id`, and `message`. Timestamps use UTC RFC 3339.

The direct-index field is compact JSON, e.g. `["agent:alice","human:brice"]`,
with literal Unicode and two distinct targets sorted by UTF-8 bytes. It is an
index, not editable membership. Collision-check generated group IDs. Empty groups
have registry/membership records; Streams appear on first send. An absent members
Set denotes an empty group only if its registry entry exists.

Backend-owned atomic operations enforce these invariants:

- Registration ensures participants and `all` membership without clearing custom
  groups or changing existing owners. Agent leases reject another process token.
- Renewal/release/agent writes check the current token; stale processes cannot
  act as a successor. Lease expiry affects presence, never durable membership.
  Human presence is unspecified; do not infer it from an agent's lease.
- Group creation writes metadata/membership together and rejects duplicate IDs.
  Direct creation also writes the unique pair index. Leaving the last member does
  not delete a group; automatic deletion is outside scope.
- Sending checks group, membership, and lease at the append boundary. Validate
  arguments/key types before writes; scripts do not provide rollback on errors.
- Retain approximately 10,000 messages with [XADD](https://valkey.io/commands/xadd/)
  `MAXLEN ~ 10000`. Preserve complete Stream IDs; uncertain sends are not retried.
  UUIDs are not an automatic deduplication mechanism.

Readers have independent cursors ([XREAD](https://valkey.io/commands/xread/) or
exclusive `XRANGE`), not a competing consumer group; report retention gaps.
Agent processing context/checkpoints stay in local runtime files; Text owns its
local drafts/cursors. Derive previews/presence from Streams/leases; add no reverse
membership, latest-message, or message-UUID indexes initially. Backend persistence
controls survival across server restarts. Reserve `P:coord:*` and `P:events:*`;
Hub process records and subscription queues remain local.

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
| `too text` | Conversation directory, members, presence, latest preview. |
| `too text TARGET [MESSAGE...]` | Interactive conversation or confirmed send-and-exit. |
| `too top` | Global activity through Hub. |
| `too AGENT top` | Agent activity through its own API. |

Remove `too team`. Global commands resolve root configuration; agent API exposes
only its own data. Preserve Text literal bodies, drafts, input, tmux, and rendering.

| Tool | Contract |
| --- | --- |
| `msg/targets()` | Registered participants and accessible conversations, with canonical targets. |
| `msg/send(target, body, in_reply_to?)` | Send using context-derived identity; return receipt. |
| `msg/create_group(name)` | Create a custom group containing the caller. |
| `msg/join_group(group)` | Caller joins a custom group. |
| `msg/leave_group(group)` | Caller leaves a custom group. |

Replace `coop` with `msg`; reserve `coord` without tools. Services enforce the same
rules for CLI, HTTP, and tools. Group administration is available through tools
and Hub HTTP; no additional CLI commands or discovery tools are introduced.

## Subscriptions and remaining definitions

The [canonical subscription protocol](local-subscriptions.md) owns source cursors,
cache watermarks, structural prefill, parallel-run handling, and HTTP recovery.
Local agent and Hub use the same boundary rules: select a stable boundary, fill
missing structure/final results from its records view, then follow the suffix.
Reuse this execution-aware normalization; storage readers and transports differ.

```text
agent canonical stream -> teaming exporter -> backend Stream -> Hub clients
```

`teaming/events.py` owns the asynchronous exporter, registered before agent work
starts; `teaming/backend.py` remains the only driver boundary. Export locally
originated canonical events once, preserving actual run IDs and source cursors.
Client-specific prefill is not republished as new canonical execution. Hub imports
never re-export, and disabling teaming removes only this optional outlet.

Stage 5 uses one shared event Stream under `P:events:*`, with agent/thread/root
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

Export canonical `RunRetried` mutations even on an uninterrupted connection.
Before exposing replacement events, atomically append the mutation and apply its
persisted invalidation set: remove obsolete steps/descendants, retain unaffected
prefixes, and clear the root's old terminal result. Record the retry control/version
and source cursor so a duplicate mutation cannot delete newer reused paths.
Preserve invalidation metadata for records recovery; Begin/End upserts alone
cannot represent deleted rows. The same mutation/reset rules apply to Hub live,
cached, and records-prefill readers.

Hub records prefill follows the linked checkpoint protocol: no intermediate SSE
IDs, then one checkpoint at backend boundary `B` after the complete prefix. Source
cursors carried by reconstructed events must not advance that delivery cursor.

Retained exporter backlog resumes in source order. Uncertain writes require
source-identity deduplication and lease fencing before retry. Filtering/compacted
delta holes are legal; sequence discontinuity alone is not proof of loss. Buffer
overflow, lost source history, or an empty restarted backend requires explicit
resynchronization from agent records. Missing deltas are disposable; missing
structure/final results must be repaired before claiming continuous observation.
Recovery markers are transport control, not invented source execution events;
Hub marks stale/incomplete views until recovery establishes a new boundary.

Before stage 5 implementation, define backend key/value schemas, projection
versioning, retention, deduplication/recovery transactions, exact Hub routes and
wire envelopes, and activity presentation. Backend retention is separate from
the local readers' cache watermark and from messaging retention. Do not expand
coordination in these PRs.

## Acceptance and touchpoints

| Area | Required checks |
| --- | --- |
| Messaging (#708) | Scope errors; disabled agent makes no backend calls; readable typed identities; ambiguous targets; direct-pair uniqueness; participant permissions; context-derived sender. |
| Storage (#708) | Concurrent creation; stale lease rejection; persistent membership; independent readers; full cursor order/gaps; uncertain writes; no vendor imports outside backend. |
| CLI/tools (#708) | Bare Text directory, no `team`, preserved interactive Text behavior, five `msg` tools, no `coop`. |
| Hub | Readiness failure, lifecycle isolation, port precedence/conflicts/discovery, Text HTTP parity. |
| Local subscriptions | Canonical cursor/prefill/cache acceptance in the linked plan; no backend required. |
| Team observation | Local/Hub boundary and interrupted-prefill parity; retry deletion/prefix preservation in every read path; duplicate invalidations cannot erase replacements; multiple clients, source order, lease fencing, outage recovery, no forwarding loops, local/global `top`. |

Likely files: new `teaming/`, existing `api/common.py`, setup watcher/types,
`up/server.py`, `work/messaging.py`, toolset factory/context, CLI routing/Text,
and corresponding tests. Keep default tests offline; run the same backend contract
against Redis and Valkey separately. Follow [repository verification](../../AGENTS.md#verification).
