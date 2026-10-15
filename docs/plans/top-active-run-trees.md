# Root-run activity trees for Top

Status: proposed; awaiting human confirmation. Feature definition only.

## Goal and scope

Give Top two root-run datasets: **Active**, containing each unfinished root's
current execution tree and consumption, and **Recent**, containing one summary
per recently finished root. Agent and Thread views group these datasets; Run and
Tree views expose their detail.

The executor maintains one live projection for all observers. Completed branches
release memory without losing consumption; completion and retry switch root
membership coherently. Reads scale with exported activity, not completed history.

Scope includes projection maintenance, compact snapshots, a read-only observation
endpoint for embedded execution, and Top integration. Preserve execution ownership,
async/spawn/cancellation semantics, process lifetimes, CLI entry points, presence,
and discovery scope. Do not redesign statistics storage or event subscriptions.
This replaces the active/recent and execution-row statistics contracts in
[Top activity](top-activity.md) and [Top dashboard](top-live-layout.md).
The unmerged [Top simplification proposal](https://github.com/openhat-ai/toolang/pull/797)
is context, not a dependency: this design requires Recent.

## Verified foundation

- `RunExecutor` supports concurrent roots, including multiple roots in one Thread.
  Its `_active` map owns tasks and controls, not presentation.
- [Canonical publication](../../src/toolang/execution/stream.py) orders committed
  events across roots. `RunBegin.parent`, `StepRef.parent`, and `Occurrence`
  supply parentage, nesting, item, lane, and iteration identity.
- [Current Top observation](../../src/toolang/up/activity.py) reads SQLite even
  for local live runtimes. This path must change to consume the new projection.
- `async run` ends its launch Step before its child finishes; the owner cleans
  up async children before its own `RunEnd`. `spawn` creates an independent root.
- Run acceptance precedes `RunBegin` without an equivalent source event.
  [Host scripts](../../src/toolang/cli/toolang/commands/script.py) can embed their
  executor without an HTTP endpoint; both gaps need explicit integration.

## Tree and vocabulary

| Term | Meaning |
| --- | --- |
| Root Run | Physical Run without a parent Step; unit of Active and Recent. |
| Active Run Tree | Root plus unfinished execution and its required ancestors. |
| Run / Step node | Existing physical Run / execution Step, retaining its real reference. |
| Current calls | Unfinished model/tool Steps; separate from accumulated call counts. |
| Root summary | Terminal root identity, outcome, timestamps, consumption, and short diagnostic. |

Use Run **and** Step nodes: Runs alone cannot express concurrent tool calls in one
Run, flow structure, or async launch/await. Lane/item/iteration are attributes;
Agent and Thread are grouping keys, never additional tree nodes.

```text
r1 review_project                                      running
├─ r1.2 map review_file                                running
│  ├─ r3 review_file [item 3/8, lane 1/2]                running
│  │  └─ r3.1 model provider/model                      running
│  └─ r4 review_file [item 4/8, lane 2/2]                running
│     └─ r4.2 tool shell                               running
├─ r1.3 async run check                                succeeded
│  └─ r5 check                                         running
│     └─ r5.1 tool shell                               running
└─ r1.4 await check                                    running
```

Actual references and positions come from execution. Await does not add another
parent edge to its target; a spawned root remains separate even when awaited.

Keep pending/running Runs, running Steps, and their ancestors. Prune terminal
nodes once they have no retained descendants. A terminal async launch Step can
remain as an ancestor; completed siblings disappear. Keep a pending/running root
with no current call. Preserve existing statuses: await is an operation, not a
new persisted status; an empty call set does not imply idle.

## Consumption and Top statistics

Every retained node has inclusive `stats`: model/tool call counts,
input/cache-read/output tokens, spend, and accounting coverage. Call nodes retain
their direct contribution as well. Ancestors include completed descendants that
have already been pruned. Cache-read tokens are included in input, not added again.

- Count each actual model/tool attempt at committed StepBegin; settle recorded
  usage and selected cost at StepEnd, including available partial accounting.
  Keep known/unknown, estimated, and partial values; missing usage is not zero.
- Apply each contribution once to its node and ownership ancestors. Reuse one
  pure contribution conversion for durable accounting and the live projection;
  do not copy pricing or usage-normalization logic.
- Runtime tools count as tool calls. Independently spawned work belongs to its
  own root. Pruning never subtracts or reapplies contributions.
- Maintain instantaneous `active_model_calls` and `active_tool_calls` separately:
  ten model begins with two unfinished means ten calls accumulated, two active.
- Root statistics cover the physical root across all attempts. On retry, seed
  that root once from its existing durable lifetime aggregate, without replay.
  All non-root Run/Step nodes cover their current incarnation only and start at
  zero, including reused references. Retained historical prefixes contribute to
  the seeded root, not to fresh descendant counters. Rerun creates a new root.
- Root completion releases the live tree. Recent reads the root's same lifetime
  totals from durable accounting in one bounded batch, without descendant scans
  or one statistics query per summary. No completed tree or second archive is kept.

| Top surface | Metric range and source |
| --- | --- |
| Header and Agent/Thread rows | Existing selected Period, using the shared scope statistics reader. |
| Active root and Recent root rows | Root lifetime consumption, including prior retry attempts. |
| Active non-root tree rows | That node's current execution incarnation, including its completed descendants. |

Execution views label their metrics **Execution totals**; Header keeps the
selected Period label. Details states root lifetime or current incarnation as
applicable. Switching to Agent/Thread restores Period-based row metrics.
`--since` and Period controls still affect Header and Agent/Thread, not execution
rows. `--recent` keeps its existing syntax/default; `--active` hides Recent.

Do not issue Period queries for execution-tree nodes or compute Header/Thread
statistics by summing visible roots. Scope statistics remain independent of
filters, truncation, and visibility. Read Agent/Thread metrics in bounded scope
batches without invoking the legacy reader's full tree projection. This adds no
new persistent child/Step series and does not remove existing legacy storage.

## Executor maintenance

Add `ExecutionActivity` in `execution/live.py`, exposed by `RunExecutor`. It owns
compact descriptors, parent links, counters, active membership, and a tree
revision; `_active` retains execution-control ownership.

Stage changes at canonical publication. Apply them after the owning record
transaction succeeds and before releasing the publication gate or notifying
observers. Rollback applies none. This is a built-in synchronous projection,
not a tracer/subscriber that could lag or overflow.

| Committed fact | Action |
| --- | --- |
| Run acceptance | Add pending Run with root, Thread, parent, runnable, occurrence, and admission identity. |
| RunBegin | Mark the admitted incarnation running and retain its begin identity/time. |
| StepBegin | Add compact operation/model/tool metadata; apply a call begin contribution when applicable. |
| StepEnd | Settle accounting, update status/current-call counts, then prune unnecessary terminal nodes. |
| Child RunEnd | Close its unfinished membership and prune; ancestor consumption remains. |
| Root RunEnd | Remove Active membership after descendant cleanup; the durable root becomes Recent-eligible. |
| RunRetried | Apply invalidation before replacement Begins and reopen the seeded root pending. |
| Part events / Thread mutations | No consumption changes; fork/rewind never copy or refund usage. |

Use a private staged acceptance fact, not a new public RunEvent. Stage async child
acceptance and its launch StepEnd in the same batch so pruning cannot lose the
ancestor. Cover root, child, spawn, retry/rerun, and dispatch failure. Never
fabricate Runs for inputs waiting on map lanes.

Consume canonical source facts only, not subscription prefill/context. Use begin
cursors for started incarnations and admission/retry controls for pending Runs.
Advance the tree revision once per committed batch that changes compact state.
Do not retain completed-child lists or lifetime sets of consumed event IDs.

Maintain exact active root/node/current-call counts and Agent/Thread membership
incrementally. Keep roots in last-change publication order so bounded export does
not sort all roots. A transition costs its ancestor depth; live memory follows
current structure, independent of observer count and completed history.

## Snapshot construction and Recent

Define `ExecutionActivitySnapshot` in `execution/schemas.py`:

| Data | Content |
| --- | --- |
| Identity | Executor epoch, sample sequence, observed time, and coverage. |
| Active | One root descriptor and flat parent-linked nodes per root, with stats and current-call counts. |
| Recent | Bounded terminal root summaries with root lifetime stats. |
| Limits | Exact active counts, exported counts, active truncation, and `recent_has_more`. |

Bound free text to 240 characters. Include IDs, node incarnation identity, parent,
kind/operation, names, status, timestamps, occurrence, and explicit tool summary.
Exclude full inputs/outputs, model messages, streamed text, arguments, and authored ASTs.
Keep inspect references; reference-only errors need not be dereferenced for Top.

Capture at most 200 roots and 4,000 nodes as detached flat descriptors/counters
under the publication gate; pin the first read of a separate read-only SQLite
transaction at that boundary. Do not copy the full live tree/event cache or add
persistent immutable-tree versions. Reuse detached active data while its revision
is unchanged. Query Recent and encode outside the gate; no network or awaited
client work belongs under it.

Recent selects terminal root Runs with
`finished_at >= observed_time - recent`, ordered by finish time descending, then
ID. Use a partial root-terminal `(finished_at, id)` index and `LIMIT 1001`: export
at most 1,000 rows and use the extra row for `recent_has_more`. Never count all
eligible history, including for `--recent all`. Bulk-read totals for selected IDs.
Select compact metadata; never decode full outputs or invocation payloads for a
summary. No recent-run table is introduced. Descendant changes, thread activity,
and observation refreshes cannot advance root completion time.

The pinned view and copied Active data represent one commit boundary. Completion
cannot appear in both datasets or neither; retry removes the former terminal
summary while reopening Active. Recent expiry uses the existing clock tick.
Assign sample sequences at capture, including clock-only refreshes; reject older
sequences and obsolete query responses within an epoch. A new executor epoch
invalidates old samples; this version is not an event replay cursor.

Cap serialized activity at 1 MiB. Active roots have priority; trim Recent before
active paths. Retain roots before descendants and preserve ancestry. Further
Recent truncation sets `recent_has_more`; active truncation retains exact runtime
counts without claiming all paths were exported. Recent outcome counts describe
loaded summaries and are labeled partial/lower bounds when more exist. No exact
Recent eligible/omitted counts or full-history grouping queries are required.
These limits constrain observation, not execution or statistics.

Share a bounded cache of latest samples across viewers with the same Recent
query; retain at most eight query entries. Slow viewers hold at most one pending
absolute replacement. Reconnection reads current state, without replaying missed
UI transitions. Reuse existing refresh/tick scheduling; allow one in-flight read
per source/query family, with a two-second deadline and at most four concurrent
source reads. Slow Period queries cannot block execution-snapshot updates.

## One live source

All live Top paths read the executor snapshot, including local runtimes. Add two
read-only routes; existing agent activity routes retain their public contracts:

| Route | Inputs and responsibility |
| --- | --- |
| `GET /api/v1/activity/current` | Expected executor epoch and Recent query; return the execution snapshot. |
| `GET /api/v1/activity/stats` | Expected epoch, Period, and at most 200 Thread IDs; batch scope metrics for the Agent and requested visible Threads. No execution-tree reads. |

Scope-metric responses identify their Period and durable revision; load/cache them
independently of live-tree refreshes and reject obsolete Period/epoch responses.
Hosting injects concrete readers; `execution` owns no HTTP/discovery policy.

Publish `activity_url` (the activity API base URL) and `executor_epoch` in runtime
registration once the observer is ready. Hosted agents mount the read-only routes
in their existing application. Top discovers registrations as today and reads
these routes; local access to `runs.db` is not an alternative Active source.

For embedded host scripts, mount the same routes in a small FastAPI application
factory backed by the existing executor. Run Uvicorn in that process/event loop
on an ephemeral loopback port; publish the URL after the listener is ready and
before admitting work. Do not instantiate another executor, enable drivers,
move execution to a daemon, or expose mutation routes. Reuse existing factory,
logging, and server-cleanup patterns. The observer owns neither the event loop nor
signal handlers; the script retains cancellation authority. Drain execution before
closing observation; close the listener before releasing its store and
registration. Observation startup failure marks Active unavailable without
changing script execution.

If an endpoint is missing, unavailable, or belongs to another epoch, mark Active
unavailable or retain an explicitly stale last sample; never rebuild it from
records. Offline sources may read terminal Recent summaries and scope statistics
through existing read-only store access, but cannot assert live work or idle.
No live activity tree is persisted to SQLite, files, or Valkey. Top never starts
an executor/server or changes execution because an observer attaches/detaches.

## Views and compatibility

Active and Recent are eligibility classes, not mandatory separate panels.
Retain table/navigation structure. Activity sorting puts Active before Recent;
other sorts move whole root groups without interleaving descendants.

| View | Active | Recent |
| --- | --- | --- |
| Agent | Exact root/current-call counts; Period statistics. | Outcome counts for loaded summaries, with partial indication. |
| Thread | Same aggregation scoped to that Thread. | Same loaded-summary aggregation. |
| Run/List | One root row: runnable, pending/running, current-call counts, and root lifetime stats. | One root row: runnable, outcome, elapsed time, lifetime stats, and short diagnostic. |
| Run/Tree | Root plus current Run/Step paths and inclusive execution stats; folds are presentation only. | Same single root summary; completed internals do not expand. |

For example: `review_project · 2 model + 1 tool active`. With no call, show the
current operation or pending/running. A handled child failure does not make its
root failed. On completion, selection stays on the root's Recent row; a pruned
child selects its nearest retained ancestor. Keep existing expiry fallback.

Apply literal text filters to exported descriptors/summaries; filtering does not
expand the bounded query or search historical descendants. Labels, counts, and
coverage must reflect truncation. Header and scope statistics do not change.
Preserve public flags and keys; document the execution-total range and sampled
filter behavior as compatibility changes in the implementation changelog.

## Top integration

```text
executor live tree + terminal root reader -> /activity/current --+
                                                              +-> RuntimeObservation -> Activity -> rows/dashboard
Agent/Thread Period reader -------------> /activity/stats ------+
```

The API paths use the `/api/v1` prefix defined above. RuntimeObservation delivers
complete execution snapshots and separate scope-stat replacements; Top applies
them directly instead of converting them into legacy activity pages/checkpoints.

| Existing component | Integration responsibility |
| --- | --- |
| `cli/toolang/commands/top.py:top_command` | Resolve root/home scope and supply the new observer to `watch`; route live activity through executor snapshots. Keep offline terminal/history reads and presence metadata distinct. |
| `up/activity.py:RuntimeObservation` | Discover instances, read their registered activity URLs, attach ownership/coverage metadata, and produce independent execution/stat updates per instance. Apply the source limits above. |
| `cli/common/activity.py:watch` | Keep background reads, input handling, repaint scheduling, and terminal cleanup. Receive complete replacements and request redraw; source failures affect that source only. |
| `cli/common/activity_view.py:Activity` | Store the latest execution snapshot and scope metrics separately; validate identity/query versions and replace state before projecting rows. |
| `Activity.rows()` and `activity_dashboard.py` | Apply the view table above using existing grouping, folding, sorting, layout, and Details interactions; read supplied statistics without execution-node SQL. |

Key source state by runtime instance and expected executor epoch, then group by
Agent/Thread for display. Distinct instances of the same Agent must not overwrite
each other. Discovery owns instance removal/replacement; a partial update from
another source cannot remove an omitted instance. Hub roster/presence can decorate
the observation, but legacy Hub activity pages cannot replace executor snapshots.

Provide explicit activity and scope-stat replacement handlers in `Activity`.
An activity replacement validates instance/epoch, Recent-query generation, and
sample sequence, then swaps Active and Recent together. A scope-stat replacement
validates epoch, Period generation, and requested scopes. Keep requested settings
visible while loading; mark the affected Recent window or scope metrics updating
until replacement arrives, without relabeling old data as a fresh calculation.
The two families have independent readiness/error state, so slow Period reads or
another agent's failure cannot freeze fresh execution activity.

Keep root selection by instance/epoch/root ID across Active-to-Recent and retry
transitions while that root remains visible. Before replacement, retain the
selected child's ancestor chain for the existing nearest-visible-ancestor
fallback. Include incarnation identity in
non-root selection/fold keys so a reused reference cannot inherit an old node's
UI state. Folds and selection survive unrelated sources' replacements. Existing
Details result lookup stays an explicit asynchronous read outside compact frames.

| Trigger | Observation work |
| --- | --- |
| Startup or reconnect | Read a complete current snapshot; load Header/scope metrics independently. `--once` uses the same bounded reads for one render. |
| Refresh tick or Recent expiry | Request the latest current snapshot; replace it atomically on success. |
| Recent change | Advance its query generation and reload current activity; invalidate late responses for the previous window. |
| Period change | Advance the Period generation and reload scope stats; retain the current execution snapshot. |
| View change, thread viewport change | Project existing activity immediately; batch-load newly needed visible Thread metrics when applicable. |
| Fold, filter, sort, selection, resize | Reproject/repaint locally without execution reads or statistics recomputation; newly visible Thread metrics follow the viewport rule above. |
| Root completion | Arrives in the next complete snapshot as a Recent summary; no client-synthesized terminal transition. |

Opening Top and subscriber lifetime never own execution. Reconnect uses current
state rather than canonical event replay. Preserve existing alternate-screen,
keyboard/mouse, scrolling, and redraw behavior while replacing the data path.

## Touchpoints and acceptance

Likely files: `execution/{live,types,schemas,activity,accounting,statistics,stream}.py`,
executor publication/admission and `executor/{awaitables,spawn}.py`;
`execution/store.py` for indexed Recent and bulk totals; `up/{core,activity,observation,server}.py`;
API app/router factories, script lifecycle wiring, observation adapters, and
`cli/toolang/commands/top.py`, `cli/common/{activity,activity_view,activity_dashboard}.py`.
Schemas remain independent of stores/runtime services.
Only this definition changes now; no implementation or changelog changes ship.

| Scenario | Pass condition |
| --- | --- |
| Concurrent roots in one/multiple Threads | One tree per root; exact active grouping/counts. |
| Map lanes, nested flows, parallel model/tools | Real parentage and occurrence; every retained branch survives view changes. |
| Async launch/await and spawn | Required terminal ancestor remains; no duplicate await edge or spawned consumption. |
| Pending, pure Flow, dispatch failure, cancellation | Accepted no-call work remains visible and closes correctly. |
| Pruning and nested consumption | Completed nodes disappear without refund/recount; durable and live contribution conversion agrees. |
| Ten begins, two current calls, partial/unknown usage | Accumulated/active counts and recorded usage/cost/coverage remain distinct and correct. |
| Retry with reused descendant references | Root retains old usage once; fresh non-root nodes count only their new incarnation; no database-total substitution. |
| Completion/retry racing capture | One coherent membership; final root counters agree with durable lifetime totals. |
| Long history, Recent all, export caps | Limited Recent seek and bulk totals, no full counts/history grouping; bounded gate copy and truthful has-more coverage. |
| All views, Period changes, filters/folds | Execution rows use live/root totals without per-node Period SQL; scope metrics remain independent. |
| Top source wiring and duplicate Agent names | Live data reaches Top through the executor observer; distinct runtime instances remain distinct and legacy activity pages cannot overwrite them. |
| Top replacement, selection, and query races | Active/Recent swap together; root selection survives completion/retry, pruned children select ancestors, and late epoch/window/Period responses are discarded. |
| Top navigation, refresh, and once | Local interactions repaint without execution reads; view changes fetch only missing scope metrics; normal and once rendering use the same snapshot contract. |
| Hosted/local/sandbox/embedded host script | Live Active always comes from the owning executor; embedded endpoint adds no executor/driver or persistent daemon. |
| Embedded observer failure/exit and observer disconnect | Execution semantics are unchanged; listeners/registrations are cleaned up and source loss is not fabricated completion. |
| Two viewers, slow reads, stale samples, clock-only expiry | Shared bounded replacements, epoch/sequence/query checks, independent healthy-source updates. |
| Stopped or endpoint-less source | Explicit unavailable/stale Active; only terminal Recent/history is record-backed. |

Verify links and source claims, then `git diff --check`. Implementation adds
offline deterministic acceptance tests, focused CLI/API documentation and
changelog coverage, and passes [default verification](../../AGENTS.md#verification).

## Risks and confirmation

Missing admission paths, early StepEnd delivery, async pruning, retry scope, and
mixed capture boundaries are the main correctness risks; acceptance above covers
them. Missing durable root aggregates remain explicitly unknown, never an
unbounded replay or invented zero. The embedded observation listener is the only
new hosting surface; it must not own execution lifecycle.

Mixed Run/Step trees, per-incarnation descendant totals, root lifetime totals,
Period scope, and the embedded observation route are explicit proposed decisions.
Human confirmation is pending; this request does not authorize implementation.
