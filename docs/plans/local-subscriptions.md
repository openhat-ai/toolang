# Canonical event subscriptions

Agreed architecture for stage 4 of [teaming](teaming.md), replacing the earlier
live-only proposal. Stages 1–3 merged as #709, #708, and #710; this design merged
as #711. The first implementation covers hosted CLI ownership. Canonical cursors,
cache, normalization, and teaming export follow. Resolve remaining choices before
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

Chat, retry/rerun, and run/thread controls now use a persistent hosted runtime.
Script mode still permits host embedding and stops script-created guests on exit.
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
cursors and cannot substitute for these fields. A record's Begin cursor identifies
its execution incarnation; retained retry prefixes keep theirs. Reused paths must
not inherit the replaced record's cursor or terminal state. Use existing control
references for mutation identity, without another independent version counter.

Run/thread subscriptions filter the same sequence; gaps from filtering or safe
delta compaction are valid. Retry does not reset the runtime sequence. Restart
uses a new epoch; never numerically compare sequences from different epochs.
Legacy records without cursor metadata require structural initialization rather
than invented historical positions.

An accepted retry emits canonical `RunRetried` before its new `RunBegin`, carrying
root/thread, retry control reference, invalidated step references, and
removed descendant run IDs. Compute effective sets before deletion; persist them
and the cursor with retry admission. Clear the root's terminal result and mark it
pending, preserving unaffected prefixes/background work. Live/cached delivery
applies this mutation before replacement events; records recovery replaces the
affected tree with its current structure. Ignore already-applied retry controls
so duplicates cannot erase newer reused paths.

## Cache and fanout

Maintain one bounded, ordered agent cache. Each subscriber owns a scan cursor,
scope, structural state, and a bounded in-flight batch, rather than another full
event queue. Filter after scanning and advance over nonmatching events. Retain
normalization state only for open structure and acquired batches; release closed
trees once their descendants finish. Bound this state and snapshot resources too;
an agent/thread subscription must not accumulate a lifetime set of seen events.

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

Preserve the normal cache window. Under capacity pressure, compact a finalized
step's `Part*` events only after every active reader has taken them. `StepEnd` alone
does not authorize immediate deletion. Remove that disposable progress together,
without renumbering survivors or advancing `floor`; retain Run/Step Begins/Ends
and durable mutations. This also releases the full content duplicated in PartEnd.
Compact each actual step independently, never its still-running descendants.

If a lagging reader prevents reclamation at the hard limit, fail that subscription
explicitly with overflow and release its retention constraint. Other readers and
execution continue. Evict the oldest prefix if further space is needed, advancing
`floor`. Never silently remove unread progress from a continuing subscriber or
allow one to pin unbounded memory. Bound cache count/bytes, batches, and framework
buffering, including publishers on worker threads.

## One subscription algorithm

Execution owns one normalizer for the local and Hub readers; scope is a filter.
Readers supply ordered positions, retained events, and a consistent structural
view. HTTP encodes the resulting frames; it does not implement another recovery
algorithm. Raw canonical readers, including the exporter, bypass client prefill.

| Scope | Included events | Lifetime |
| --- | --- | --- |
| Run | Root and all admitted descendants, preserving actual IDs. | Until the tree is terminal and matching queued events are drained. |
| Thread | Its run trees and mutations; forks also reach the source thread. | Remains open across runs. |
| Agent | All local execution/thread events, once each. | Remains open across threads. |

1. Atomically establish observation and a safe boundary `B`, with a corresponding
   consistent records view. Capture the active run set at that boundary; newly
   admitted runs must fall into either prefill or the subsequent stream.
2. Use cached replay only if `(C, B]` is covered and every needed ancestor Begin
   can be recovered for that event's incarnation. Deliver source events in order
   with their resume IDs; ancestor context has no SSE ID. Never pair an old cached
   event with a newer record at a reused path. Otherwise use records prefill.
3. Records prefill selects active trees and trees changed in `(C, B]`, including
   retry invalidations and completed trees, with applicable thread context/mutations.
   Replace those trees with their current structure at `B`; do not reconstruct
   deleted attempts. Send `stream_prefill` declaring `B` and the replacement scope,
   the structural prefix without SSE IDs, then `stream_checkpoint` with SSE ID `B`.
   Only then follow the suffix after `B`.
   Lost suffix coverage fails this subscription; do not repeatedly restart prefill
   while execution outruns it.

Prefill has snapshot/upsert semantics, contains no `Part*` events, and may group
records by run. Supply ancestor Begins before descendants and a matching Begin
before each End. Source identities identify record incarnations, not delivery
checkpoints; Begins never clear an applied End of the same incarnation. Clients
replace only the declared view and keep the old cursor until the complete prefix
and checkpoint are applied. An interruption retries that replacement from the
old cursor. A cursor that named a delta needs no exact delta-to-record lookup.

On first attachment without a cursor, a run scope initializes its recorded tree;
thread/agent scopes initialize active trees and follow subsequent events. History
browsing remains separate. Cursor recovery must also cover trees that completed
while disconnected, not merely those currently running.

On every attachment, the client adapter clears unfinished Part rendering in scope.
Cached catchup omits `Part*` for steps already finalized at `B`, even if their Begin
is retained: compacted delta holes must not appear as complete progress. For an
unfinished step joined after its original Begin, supply missing ancestor Begins
and suppress that step's `Part*` through `StepEnd`. The authoritative End replaces
partial progress; later steps stream normally. Other branches continue independently.
A branch's End is not its siblings' completion, and parent `RunEnd` does not close
a tree with active background descendants.

After delivering a scanned batch, advance over filtered/suppressed events with a
coalesced `stream_checkpoint`, at latest at the next keep-alive on an idle scope.
It never passes an undelivered matching event or an incomplete prefill. This bounds
reconnect work without keeping a second queue of acknowledgements.

Record cursors provide lookup, not historical snapshots. All mutations relevant
to prefill must respect the boundary protocol, including admission, retry, and
rewind. Locally, open a separate read-only SQLite transaction and perform its
first read under the publication gate when capturing `B`, pinning the WAL view;
release that gate before reconstruction or network I/O. Do not hold the executor's
store connection/lock for a subscriber's read lifetime. Bound snapshot lifetime
and release it on completion/termination; expiry fails prefill without committing
its checkpoint. Never combine newer mutable records with old `B`.
An incompatible epoch or unavailable mutation history uses the same replacement
prefill for the entire selected scope, including retained terminal trees. Its
declared scope is the reset; no separate reset state machine is needed.

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
`RunRetried` first without `after`, installing observation before retry admission.
With `after`, retry catchup may precede that mutation; an earlier attempt's RunEnd
must not end observation of the accepted retry. Keep `X-Toolang-Run-ID` for
compatibility, without making it a protocol requirement. A cursor controls
observation, not execution idempotency: never automatically repeat POST after a disconnect.
Once the run ID is known, reconnect through GET. Lost responses before learning
the ID need a separate request-ID reconciliation contract.

Reuse canonical event serializers, adding the source `RunRetried` event. The three
transport controls are `stream_prefill`, `stream_checkpoint`, and `stream_error`;
none is a source mutation. Shared client adapters apply prefill, clear transient
Parts on attachment, and track checkpoints before dispatching to presentation.
Existing live-only projectors cannot consume reconstructed events unchanged.
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
| Every scope and start-and-subscribe | Correct isolation/ancestry; earliest POST events are captured; retry catchup cannot end at an older RunEnd; one client's closure leaves others running; background descendants are observed through completion. |
| Mid-run attachment and replay | Mid-Part reconnect clears stale rendering; missing Begins precede Ends; compacted finalized steps emit no partial progress; other active branches continue. |
| Interrupted grouped prefill | Disconnect after A's 140 but before B's 120/130 keeps the old cursor; retry loses no records, duplicates no structural nodes, and checkpoints only after the complete prefix. |
| Retry invalidation | A shorter retry removes obsolete steps/descendants and preserves the prefix in all read paths; a cached old End with an unavailable Begin falls back to current records; duplicates cannot erase replacements. |
| Consistent handoff | A pinned read view stays at `B` while writers progress; admission/retry/rewind causes neither omission nor future-state leakage; unavailable historical versions explicitly reset, including affected terminal trees. |
| Bounded observation | Slow readers retain unread Parts until overflow; only consumed finalized Parts compact; many completed roots do not grow normalizer state; slow prefill terminates without a restart loop. |
| Filtered scopes | Scanning only unrelated/suppressed events still advances a delivered checkpoint; no checkpoint skips a pending matching event or incomplete prefill. |
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
prefill/checkpoint/error payload schemas, and request-ID reconciliation. The
checkpoint commit rules and retry mutation semantics above are fixed. Hub
storage/versioning/retention details are settled in stage 5. These are explicit
follow-ups, not guarantees already provided by the current runtime.
