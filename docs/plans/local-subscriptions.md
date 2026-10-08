# Canonical event subscriptions

Agreed architecture for stage 4 of [teaming](teaming.md), replacing the earlier
live-only proposal. Stages 1–3 merged as #709, #708, and #710. This PR defines the
design without runtime changes. Resolve the remaining choices below before
implementing the affected scope.

## Goal and ownership

One agent runtime owns one executor and one canonical event stream. Independent
tracers and run/thread/agent clients observe it without blocking execution or
requiring Redis/Valkey. Optional teaming exports the same source events; local
and Hub subscriptions share boundary and structural-prefill rules.

Resident-agent CLI operations that need execution must ensure the agent is ready
and use its API. Concurrent starters converge on the same runtime. Remove host
embedding from these paths; retain the runtime after the command by default.
Mutations of subscribed run/thread structure also go through this owner; an
out-of-process writer must not bypass cursor assignment and publication.
Optional stop-on-exit applies only to the instance started by that command, never
an attached/replacement instance, and must not cancel other active work. Resolve
configuration/environment/CLI policy at the call site. Script mode is a separate,
unresolved lifecycle decision; its eventual policy must preserve single ownership
when sharing an agent home.

Current host CLI acquisition permits embedding; CLI-started guests stop on exit.
The hosted executor permits multiple active roots in one thread; preserve this
concurrency while consolidating execution ownership.

```text
executor / thread mutations -> commit records -> canonical stream
                                                +-> caller tracers
                                                +-> local API subscriptions
                                                +-> teaming exporter, if enabled
```

Create the stream before accepting work, including scheduler/messaging work.
Publish every source event once, independently of caller-supplied tracers; replace
the fallback-only `root_tracer` wiring. `tracer=` registers an observer before the
first event. Callbacks run on the subscriber side; their exceptions, network I/O,
and backpressure never enter the execution path.

## Cursor and persistence

Use one agent-wide cursor `(runtime_epoch, seq)`, encoded as an opaque token.
Sequence assignment, durable projection, and cache publication follow one order
across threads, roots, and parallel descendants. Only this short publication
section is serialized; execution remains concurrent. Assign the cursor before
projection, commit the record and cursor together, then publish with that cursor.
Delta events receive sequence numbers but are not individually persisted.

Run/Step records gain indexed `begin_cursor` and `end_cursor`; durable thread
operations retain their event cursor. Keep actual run/step identities, parent
links, and root/thread routing context. Add `thread_id` to `RunBegin` so agent
observers can organize events without another lookup. Clients supply a cursor,
not a separate step path. Existing timestamps/control revisions are not event
cursors and cannot substitute for these fields.

Run/thread subscriptions filter the same sequence; gaps from filtering or safe
delta compaction are valid. Retry does not reset the runtime sequence. Restart
uses a new epoch; never numerically compare sequences from different epochs.
Legacy records without cursor metadata require structural initialization rather
than invented historical positions.

An accepted retry emits canonical `RunRetried` before its new `RunBegin`, carrying
root/thread, retry control reference/version, invalidated step references, and
removed descendant run IDs. Compute effective sets before deletion; persist them
and the cursor with retry admission. Clear the root's terminal result and mark it
pending, preserving unaffected prefixes/background work. Apply this mutation
before replacement events in live delivery, cached replay, and reconstruction.
Deduplicate by retry control identity so repeats cannot erase newer reused paths.

## Cache and fanout

Maintain one bounded, ordered agent cache. Each subscriber owns a scan cursor,
scope, structural state, and a bounded in-flight batch, rather than another full
event queue. Filter after scanning and advance over nonmatching events.

Use an isolated [PyPubSub Publisher](https://pypi.org/project/Pypubsub/4.0.7/)
per runtime through `common/pubsub.py` (`pypubsub>=4.0.7,<5`). Its callbacks only
signal availability; coalesce cross-thread wakeups and retain listeners strongly.
Execution owns the cache, cursors, and reconstruction. Scopes are filters, not
separately published copies. Close releases listeners and wakes waiting readers.

| Watermark | Meaning |
| --- | --- |
| `floor` | End of the entirely evicted prefix. |
| `tail` | Latest published sequence. |
| `read_floor` | Minimum active scan cursor, including tracers/exporter; `tail` with no readers. |

Within an epoch, `floor <= cursor < tail` reads the retained suffix;
`cursor == tail` waits, `cursor < floor` requires records, and `cursor > tail`
is invalid.
Checking coverage, acquiring a batch, and entering a wait must avoid eviction
races and missed wakeups. Readers retain acquired batches while sending them;
a scan cursor is not a remote processing acknowledgement.

Preserve the normal cache window. Under capacity pressure, compact only finalized
steps whose cached deltas have been taken by every active reader. `StepEnd` alone
does not authorize immediate deletion. Remove those `PartDelta` entries without
renumbering survivors or advancing `floor`; retain structural/final events.
Compact each actual step independently, never its still-running descendants.

If a lagging reader prevents reclamation at the hard limit, fail that subscription
explicitly with overflow and release its retention constraint. Other readers and
execution continue. Evict the oldest prefix if further space is needed, advancing
`floor`. Never silently remove unread progress from a continuing subscriber or
allow one to pin unbounded memory. Bound cache count/bytes, batches, and framework
buffering, including publishers on worker threads.

## One subscription algorithm

| Scope | Included events | Lifetime |
| --- | --- | --- |
| Run | Root and all admitted descendants, preserving actual IDs. | Until the tree is terminal and matching queued events are drained. |
| Thread | Its run trees and mutations; forks also reach the source thread. | Remains open across runs. |
| Agent | All local execution/thread events, once each. | Remains open across threads. |

1. Atomically establish observation and a safe boundary `B`, with a corresponding
   consistent records view. Capture the active run set at that boundary; newly
   admitted runs must fall into either prefill or the subsequent stream.
2. If the requested cursor `C` is covered, normalize the retained prefix `(C, B]`.
   Deliver source events in canonical order with their resume IDs; missing
   ancestor context has no SSE ID. Otherwise query structural records and retry
   invalidations in that range, reconstructing current Begin/End structure at `B`.
   A cursor that once named a delta needs no exact delta-to-record lookup.
3. For records prefill, send `stream_prefill` declaring boundary `B`, then the
   structural prefix, then `stream_checkpoint` with SSE ID `B`. No prefill frame,
   including reconstructed source events, carries an SSE ID before that final
   checkpoint. Send live events after `B` only after the checkpoint; recheck
   suffix coverage if prefill outlives cache retention.

Records prefill contains no deltas and may group records by run. Clients retain
the previous committed cursor until applying the complete prefix and checkpoint;
repeated prefill is an idempotent structural upsert by record/version identity.
Begins do not clear an applied End of the same version. Disconnect before the
checkpoint retries from the old cursor. Source identities remain metadata,
separate from the delivery checkpoint. A reset clears only the declared view.

On first attachment without a cursor, a run scope initializes its recorded tree;
thread/agent scopes initialize active trees and follow subsequent events. History
browsing remains separate. Cursor recovery must also cover trees that completed
while disconnected, not merely those currently running.

For a step joined after its original `StepBegin`, supply missing Run/Step Begins
and suppress that step's incomplete `Part*` progress through its `StepEnd`.
Forward the final End, then stream later steps normally. The same normalization
handles attachment at an End. Track this per step: other branches and child-run
structure continue. A branch's `StepEnd` is not a completion boundary for its
siblings; parent `RunEnd` is not enough if background descendants remain active.

Record cursors provide lookup, not historical snapshots. All mutations relevant
to prefill must respect the boundary protocol, including admission, retry, and
rewind. Locally, open a separate read-only SQLite transaction and perform its
first read under the publication gate when capturing `B`, pinning the WAL view;
release that gate before reconstruction or network I/O. Do not hold the executor's
store connection/lock for a subscriber's read lifetime. Bound snapshot lifetime
and release it on completion/termination; expiry fails prefill without committing
its checkpoint. Never combine newer mutable records with old `B`.
Historical rows already removed by retry require a declared reset and current
structural prefill; retained retry invalidations still apply
when both source events and records are available. An incompatible epoch also
resets the view. Reset recovery includes affected terminal trees, not only the
active-tree initialization used for a new observer.

## HTTP and lifecycle

Keep SSE and [existing execution routes](../api.md#run-and-thread-endpoints):

| Method/path | Behavior |
| --- | --- |
| `GET /api/v1/stream` | New agent subscription. |
| `GET /api/v1/threads/{thread_id}/stream` | Thread subscription. |
| `GET /api/v1/runs/{run_id}/stream` | Root-tree subscription. |
| Existing POST execution streams | Start and subscribe before the first event. |

GET and POST accept optional `after` tokens; resume checkpoints use SSE IDs.
Validate cursor/scope before POST admission. Successful new-run start-and-subscribe
begins with the root `RunBegin`, which supplies the run ID; retry streams expose
`RunRetried` first for their already-known root, installing observation before
retry admission. Keep the existing `X-Toolang-Run-ID` header for compatibility,
without making it a protocol
requirement. A cursor controls observation, not execution idempotency: never
automatically repeat POST after a disconnect. Once the run ID is known, reconnect
through GET. Lost responses before learning the ID need a separate request-ID
reconciliation contract.

Reuse canonical event serializers, adding the source `RunRetried` event. Prefill,
reset, checkpoint, and error frames are transport control, not source mutations.
Overflow sends `stream_error` with `{"code":"overflow"}` and closes; failed writes
can close without that frame.
Latch overflow and recheck it and queued events after awaited terminal lookups.
An error before the first POST event must not appear as successful completion.
Disconnecting any subscription never cancels execution.

Keep native FastAPI generator SSE, 15-second keep-alives, and a five-second
deadline per ASGI `send`, including headers but excluding idle event waits.
Implement the deadline at the ASGI boundary: the generator path constructs a
plain `StreamingResponse`, bypassing response-subclass send overrides. Runtime
shutdown wakes subscriptions before the server waits for streams; API cleanup
only detaches observers. The runtime closes the canonical source after final
executor persistence, with bounded observer/exporter drain. Cleanup is idempotent.

## Delivery, verification, and remaining choices

Implement in order: resident CLI ownership/lifecycle, canonical cursor persistence
and cache, local subscription normalization/HTTP, then
[teaming observation](teaming.md#subscriptions-and-remaining-definitions).

| Acceptance scenario | Pass condition |
| --- | --- |
| Concurrent CLI acquisition | One runtime/executor; defaults keep it running; stop-on-exit cannot stop another instance/workload. |
| Multiple publishers and observers | Parallel roots in one thread, multiple threads, descendants, and worker-thread mutations share ordered unique cursors; slow/failing tracers do not delay execution. |
| Durable publication | Begin/End cursors commit with records before delivery; failed projection publishes nothing; restart/legacy cursors never alias current events. |
| Every scope and start-and-subscribe | Correct isolation/ancestry; earliest POST events are captured; one client's closure leaves others running; background descendants are observed through completion. |
| Mid-run attachment and replay | Missing Begins precede Ends; only incomplete steps suppress Part progress; finalized history needs no deltas; disconnected clients recover completed trees. |
| Interrupted grouped prefill | Disconnect after A's 140 but before B's 120/130 keeps the old cursor; retry loses no records, duplicates no structural nodes, and checkpoints only after the complete prefix. |
| Retry invalidation | A shorter retry removes obsolete steps/descendants and preserves the retained prefix in live, cached, and records paths; duplicate invalidations cannot delete replacement records. |
| Consistent handoff | A pinned read view stays at `B` while writers progress; admission/retry/rewind causes neither omission nor future-state leakage; unavailable historical versions explicitly reset, including affected terminal trees. |
| Cache pressure | Slow readers retain unread deltas until explicit overflow; only consumed finalized deltas compact; legal holes resume correctly; memory and wakeups stay bounded. |
| Transport races and shutdown | Actual ASGI blocked writes release dependencies; idle streams survive; overflow wins terminal races; no POST replay or implicit execution cancellation. |
| Local/Hub parity | Equivalent retained events/records produce equivalent prefill and per-agent order; teaming disabled makes no backend calls. |

Touchpoints: `cli/common/agent_server.py` and agent execution callers; `up/core.py`
and `up/server.py`; `execution/events.py`, `records.py`, `store.py`, executor
publication/spawn paths and a subscription module; `common/pubsub.py`; API/remote
stream adapters; dependencies, `docs/api.md`, and focused lifecycle/store/stream
tests. Runtime PRs update the changelog through the existing runnable and run the
[default checks](../../AGENTS.md#verification).

Remaining choices: script lifecycle; stop-on-exit option/environment/config names
and scope; cache/batch/snapshot budgets and oversized-event handling; exact cursor,
prefill/reset/checkpoint payload schemas, and request-ID reconciliation. The
checkpoint commit rules and retry mutation semantics above are fixed. Hub
storage/versioning/retention details are settled in stage 5. These are explicit
follow-ups, not guarantees already provided by the current runtime.
