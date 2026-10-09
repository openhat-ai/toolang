# Team observation

Approved in chat on 2026-10-09 for stage 5 of [teaming](teaming.md). Stages 1–4
are merged, including local subscriptions in #717. Deliver exporter, Hub
subscriptions, and both `top` commands in one implementation PR.

## Goal and scope

Observe enabled agents through Hub using [local subscription](local-subscriptions.md)
recovery rules, including retained finals from offline origins. Preserve messaging,
the agent wire format, and local operation without a backend. Coordination, group
event filters, remote Hub access, execution through Hub, and script lifecycle
changes remain outside this delivery.

## Shared subscription core

Extract shared normalization over ordered delivery positions, source identities,
a structural snapshot, and a suffix reader. Keep tree reconstruction, incarnation
checks, retry invalidation, Part suppression, and terminal-descendant handling in
`execution/`. The two concrete adapters retain SQLite/backend resource ownership;
limit the refactor to this seam and preserve the existing local wire contract.

Structural entities carry routing/parent identity, Begin/optional End payloads,
and source/delivery positions. Resolve content/errors at the origin; retain thread
controls and root change markers. Export source data only. Qualify Hub identities
by agent; compare source cursors only within its epoch. Pending/legacy Begins
retain null cursors and require replacement. Share client structural reduction;
transport adapters own cursor validation/routing. Global recovery commits one
bounded prefix and checkpoint across all selected origins.

## Backend records and positions

Let `E = too:teaming:v1:events`, `A` be an agent identifier such as `alice`, and
`G` a random generation UUID. JSON schemas have `v: 1`; reject unsupported
versions. These keys use the existing driver and independent retention.

| Key/type | Fields/value |
| --- | --- |
| `E:meta`, hash | `epoch` (UUID), `catalog_revision` (origin membership changes), `tail`, `floor` (greatest removed Stream ID), `bytes` (retained serialized entries), and pending operation. |
| `E:stream`, Stream | Each row has `kind`, `agent`, and JSON `data`; canonical data has source cursor, thread/root routing, and the unchanged execution event. Controls are `incomplete` and `recovered`. |
| `E:agents`, hash | `agent:A` → current/staging generation, structural/status revision, lease owner token, source epoch/high-water cursor, last operation ID/digest/result, recovery ID, status/reason, and replacement floor. |
| `E:agent:A:G`, hash | JSON-array fields `['run', id]`, `['step', ref]`, `['root', id]`, and `['control', ref]` hold structural entities. Roots record terminal-tree state and last change. `['manifest']` holds source boundary, digest, counts/bytes, sealing status, and the generation's delivery baseline. |

The root is terminal only after all descendants finish. Thread scope uses physical
ownership and the existing fork-source notification rule. A retry must mirror
`accept_retry`: apply exact invalidations, switch the root control, mark it pending,
and clear its old Begin cursor/start time and terminal result/End cursor. Update
its change marker; the following `RunBegin` installs the new source incarnation.
Recovery between these two events must never reuse the previous terminal state.

Hub cursors are opaque `h1.<backend-epoch>.<milliseconds>-<sequence>` tokens. Their
epoch survives persisted backend/Hub restarts and changes on dataset loss/reset,
independently of agent epochs and messaging. Initialize it atomically. Stream IDs
order reception, not causality; compare full integers, never Lua floating point.
Missing/corrupt established state requires repair. Absent empty hashes and a
missing Stream with `tail = 0-0` are valid initialization states.

## Append, recovery, and retention

One exporter has at most one unconfirmed write. Publication commits use a source cursor
or recovery UUID/phase as operation ID. Validate lease, backend epoch, and absence
of a pending operation; then check the last receipt before generation/prior-cursor
preconditions. Matching ID/digest returns the original receipt, including after a
generation switch; conflicting digests fail. Older source cursors in the accepted
epoch are covered no-ops; another epoch requires recovery. Append, projection,
and receipt commit together. Deduplicate before retry invalidation, and retry an
uncertain commit unchanged. Sequence holes alone do not imply loss.

Staging uploads one bounded full snapshot with absolute fields and a fixed digest.
It does not change publication receipts or append events. Retry writes/verifies
the whole snapshot, including after key expiry, instead of acknowledging absent
data from a receipt. Seal after fields/counts/bytes validate; no upload log.

All scripts receive their keys explicitly and validate types, payloads, bounds,
and preconditions before writing. Use ordinary Redis/Valkey commands and
[Lua atomic execution](https://redis.io/docs/latest/develop/programmability/eval-intro/),
not an assumption that script errors roll back earlier writes. Mark publication
pending in `E:meta` before changing shared state; clear it only after all writes
and the receipt complete. A surviving marker makes the event dataset unavailable
until repaired: append/trim failure may damage shared accounting. Report
`protocol_error` without automatic retry. Operator repair restores a consistent
event dataset or clears the entire event namespace while its writers are stopped;
reinitialization uses a new epoch. Never automatically erase data. Staging failures
leave the old generation intact; gaps/budget failures mark only that origin
incomplete. Execution and messaging remain independent.

On first export, source restart, lost backlog, missing ancestors, or backend reset:

1. Fence and mark this origin incomplete. Append a control only on an observable
   status change; repeating a failed recovery must not invalidate Hub snapshots
   again. Keep the previous projection readable as stale data.
2. Capture source boundary `C`, pin records, and subscribe after `C` under the
   publication gate. Build the bounded snapshot outside that gate and close its
   read-only store before backend I/O. Keep the suffix reader registered.
3. Upload/seal one fenced staging generation, refreshing its 60-second expiry
   on upload. Remove abandoned staging before beginning a new generation.
4. Check the suffix reader before commit. A fenced commit verifies the recovery
   ID and sealed manifest, switches generation, records source high-water `C`,
   sets the manifest baseline/replacement floor, and appends `recovered` at `B`
   atomically. Remove staging expiry and
   [unlink](https://valkey.io/commands/unlink/) the previous generation;
   concurrent snapshot readers detect the changed revision and retry. Repeating
   this commit returns its original receipt even though the generation changed.
5. Export the suffix after `C`. Overflow detected before commit abandons staging;
   overflow during commit triggers another incomplete recovery with bounded
   backoff. At most a current and a staging generation exist per origin.

Recovered entities retain source cursors and inherit delivery position `B` from
the manifest; activation updates only bounded metadata. Later canonical mutations
record their own positions. Backend loss cannot recover an offline origin until
it reconnects.

| Resource | Initial internal limit and overflow behavior |
| --- | --- |
| Backend Stream | 10,000 entries and 64 MiB serialized payloads; trim only a prefix and atomically advance `floor`/byte accounting. Canonical entries over 1 MiB require records recovery. |
| Projection per agent | All active trees plus at most 100 most recently completed terminal trees, within 10,000 entities/16 MiB. Evict oldest terminal trees, then old thread controls; advance the replacement floor. Never truncate an active tree. |
| Snapshot/connection | Reuse the local 10,000-record/16 MiB/five-second snapshot and 4,096-open-entity bounds, aggregated across selected agents. Reuse bounded batches and SSE send deadlines. |

Generation switches and evictions persist a replacement floor; `after < floor`
forces origin replacement even after controls are trimmed. Load active trees
first, then whole recent terminal trees while space remains; optional history
must not make a fitting active snapshot fail. Bound selection in the records
query. Count metadata, upload, replay, and scratch state toward their budgets;
the complete serialized upload, including its manifest, is limited to 16 MiB.

If active structure alone exceeds the budget, remain incomplete and retry after
source progress or bounded backoff, without blocking execution/messaging. Full
history stays in agent records; replacement floors avoid an unbounded tombstone
log. Offline projections persist until explicit backend removal, so storage
scales with registered agents.

## Consistent Hub attachment

Atomically capture backend epoch, tail `B`, floors, and selected origins' generation
references/revisions. For a team scope, also capture the catalog revision. Read
bounded projection pages and candidate replay through `B`; atomically revalidate
these versions, required retention floors, and absence of a pending operation.
Discard and retry within the original five-second budget on change. Copy the
validated view before emitting any prefix; release all backend snapshot resources.

Structural mutations, evictions, generation switches, and status change increment
that origin's revision. Part progress, unrelated origins, and staging bookkeeping
do not. Team reads also detect origin additions/removals. This validates the view
at `B` without historical versions; sustained selected structural traffic may
exhaust the budget and return `snapshot_limit`.

Apply the local first-attachment, replay, and replacement rules to each origin.
An incompatible backend epoch replaces the entire selected view. A generation
change or projection-history loss replaces that origin's selected view, including
retained terminal trees. A matching recovery control in `(after, B]` also requires
structural recovery; represent it as snapshot/status, never a canonical event.
Decide the entire prefix before emitting it:

- If every selected origin is replayable, merge cached events in backend-ID
  order, inserting context without IDs. Never concatenate per-origin replay lists.
- If any origin needs replacement, build one structural prefix for all selected
  origins' active/changed trees and any required full-origin resets. Emit no
  `Part*` or source-frame SSE IDs inside it. Other origins' progress deltas may be
  omitted; their structure/finals through `B` must be included.

Finish with one checkpoint `B`, then read strictly after `B`. Matching recovery
controls encountered live trigger the same preparation from the last delivered
position; discard the old acquired batch, including its suffix beyond the control.
The new capture covers those entries or reacquires them after its boundary.

Use independent [XREAD](https://valkey.io/commands/xread/) positions. A
`BLOCK 1000 COUNT 1` read only wakes the reader; release that reply, then acquire
a batch with an atomic floor/epoch check and range read, bounded at 128 entries/
1 MiB. The one-second wait fits the driver's existing five-second socket timeout.
Never resume with `$`. Check epoch/floor on idle wakeups too; behind-floor readers
get `overflow` and reconnect for recovery. Yield between batches and emit periodic
checkpoints even under continuous filtered traffic. Cancellation closes the
blocking connection; no Hub fanout queue or consumer group is required.

## Hub HTTP and client contract

Add `GET /events/stream`, using existing local Hub discovery. Optional `agent=agent:alice` selects an origin;
`thread=ID` or `run=ID` additionally selects its thread or root tree. Thread/run
are mutually exclusive and require an agent. No filter means the whole team.
`after` is a Hub cursor; do not add execution POST routes or a run-ID header.

| Frame | SSE/data contract |
| --- | --- |
| Execution event | Existing SSE event name and payload, augmented with `agent`, `source_cursor`, and Hub `cursor`; SSE ID is the Hub cursor. |
| Structural context | Same envelope with `context: true`; no SSE ID. The source cursor identifies its incarnation, never a resume checkpoint. |
| `stream_prefill` | `{cursor, scope, replace}`. `replace: null` resets the entire selected view; otherwise a list of `{agent, roots}` replaces each origin's selected view (`roots: null`) or named trees. No SSE ID. |
| `stream_checkpoint` | `{cursor}` with the matching SSE ID. Commit the entire pending prefix and its replacement manifest together. |
| `stream_status` | `{agent, online, complete, reason}`; no SSE ID. Emit initial status inside prefill or before cached replay; refresh presence at keepalive intervals and status after recovery. |
| `stream_error` | `{code, detail?}`; no SSE ID, then close. |

`scope` is `{kind: "team"}`, `{kind: "agent", agent}`, or
`{kind: "thread"|"run", agent, id}`. Bind client state and its checkpoint to that
scope; changing filters starts a new view without reusing the old cursor.
Clients stage replacement frames and completion status in scratch state, validate
the complete candidate, and swap all selected state and the checkpoint together.
Do not mutate committed trees before every origin's reducer succeeds. Clear
transient Parts on attachment; incomplete prefixes leave the old cursor intact.
A checkpoint acknowledges the known view/status, including explicitly incomplete
origins; it cannot acknowledge missing events or complete those origins' roots.
Presence describes the current lease, not a historical fact at boundary `B`;
an owner-token mismatch makes the old projection incomplete. The current
participant directory supplies status-only origins with no projection.

Validate cursor syntax, then epoch, then position: a different epoch requires
replacement; only a position ahead of the tail in the same epoch is invalid.
Before streaming, invalid cursors/filters return `400`,
unknown/unretained scopes `404`, and unavailable backend/incompatible schema
`503`, using Hub's `code`/`detail` format. Registered agents without a projection
are valid incomplete scopes. During streaming use `overflow`, `snapshot_limit`,
`backend_unavailable`, `scope_unavailable`, or `protocol_error`; an explicitly
selected scope lost during recovery closes with `scope_unavailable`.
Keep 15-second keepalives and five-second ASGI send deadlines. When a root tree
is terminal and complete, capture a fixed tail and drain through it, including
queued matching retries. Close if it is still terminal; never wait for unrelated
team traffic to become idle. Agent/thread/team subscriptions stay open.
Disconnect never cancels execution.

## Lifecycle and activity

The [agent Hub transport amendment](agent-hub-transport.md) owns the HTTP
publication port, discovery, and reconnection. Backend transactions below it
remain Hub-owned; agents never connect directly to Redis/Valkey.

One teaming lifecycle owns registration, lease renewal, messaging consumption,
and export. Install the raw canonical reader before scheduler/channel/message
work starts. Backend access stays asynchronous and optional; registration or
recovery failure cannot block local readiness. Isolate exporter and messaging
failures so a projection limit does not stop message handling. Lease loss fences
both writers; exporter failures mark observation incomplete when the backend is
reachable. On shutdown stop admitting message work, keep the lease/renewal alive
through final persistence and bounded export drain, then release it. Teardown of
messaging must not release the lease while the exporter still needs it.

`too top` connects to the running Hub; `too AGENT top` uses existing agent-target
and endpoint discovery rules, without a resident-only restriction. Both observe
already running services. Use layout-only preparation, existing command factories,
and terminal libraries. Add `--once`; non-TTY also takes one snapshot and exits.

Both commands share activity reduction/presentation: agent summaries and active
root rows (agent, thread, run, runnable, status, current step/active-step count,
elapsed time). Track descendants under their root. Initially show active work;
retain at most 20 session-completed roots for 30 seconds. Label offline,
incomplete, and reconnecting states; lease loss never invents a canceled result.
Omit content bodies and resource metrics. Refresh TTY at most twice per second;
`q`/Ctrl-C exits. Reconnect GET using the committed cursor with 0.5–5-second
backoff; configuration/schema/budget errors require user action. `--once` stops
at the initial checkpoint with incomplete labels or fails on transport/protocol
error, never presenting an error as an empty successful snapshot.
Expire completed trees from client structural state as well as rendered rows.
If a later retry references a forgotten tree, retain the previous cursor and
reattach through the normal recovery path before consuming that mutation; do not
accumulate hidden history until a long-running `top` exceeds the reducer limit.

## Acceptance and implementation touchpoints

| Scenario | Pass condition |
| --- | --- |
| Shared normalization | Existing local scenarios pass through the backend adapter. Retry-admission snapshots match the new pending incarnation, including cleared Begin/End state. |
| Concurrent clients | Interleaved IDs stay ordered and agent identities stay isolated; mixed recovery disconnect/reducer failure commits neither partial trees nor a later cursor. |
| Uncertain writes | Lost publication replies deduplicate; staging expiry/retry restores full data. Injected partial staging preserves the current generation; partial publication fails closed. |
| Snapshot races | Selected writes, generation deletion, trim, and catalog changes cannot yield mixed views; unrelated-agent/Part writes do not restart a scoped snapshot. |
| Recovery | Outage/overflow, source restart, idle empty-backend reset, interrupted upload, and trimmed controls recover available finals or expose missing data. Old-epoch IDs above the new tail reset successfully. |
| Bounds | Repeated failures keep two generations without prefill churn. Large optional history cannot block active-tree recovery; oversized active structure stays incomplete. Activation work is independent of entity count. |
| Lifecycle | Export starts before work, is absent when disabled, and retains the single lease through final drain. Upload holds no records snapshot; idle/filtered streams remain cancellable without timeout loops or forwarding loops. |
| HTTP/CLI | Terminal roots close under unrelated traffic but drain queued retries. Scope/target routing, both `top` modes, and once/non-TTY work over sockets/PTYs. Long sessions release old trees and recover later retries. |

Likely files: `execution/{subscriptions,stream_client,schemas}.py` and the existing
records snapshot helpers; `teaming/{backend,events,schemas,errors,api,client}.py`;
the teaming lifecycle in `work/` and `up/server.py`; CLI routing/registration,
`commands/top.py`, and shared activity presentation; focused tests and API/CLI
documentation. Preserve the SQLite schema and source publication contract.

Keep default tests offline and deterministic. Run the same backend contract with
isolated Redis and Valkey processes, then real TCP tests against both agent and
Hub APIs plus terminal smoke tests. Follow [repository verification](../../AGENTS.md#verification).
