# Root-run activity trees for Top

Status: proposed; awaiting human confirmation. Feature definition only.

## Goal and scope

Give Top two coherent root-run datasets: **Active**, containing the current
execution tree and accumulated consumption of each unfinished root, and
**Recent**, containing one summary per recently finished root. Agent and Thread
views group these datasets; Run and Tree views expose their detail.

The executor maintains the live projection once, independently of observers.
Successful delivery means concurrent roots and branches remain distinguishable,
completed branches release memory without losing consumption, and completion or
retry moves a root between Active and Recent without duplication or omission.

Scope includes projection maintenance, compact snapshots, existing activity
adapters, and Top presentation. Preserve execution, cancellation, async/spawn
ownership, existing CLI entry points, Period accounting, presence, and runtime
discovery. Do not redesign statistics storage, retention, event subscriptions,
or hosting. This definition replaces the active/recent row and tree decisions in
[Top activity](top-activity.md); retain unrelated dashboard contracts.
The unmerged [Top simplification proposal](https://github.com/openhat-ai/toolang/pull/797)
is context, not an approved dependency: in particular, this design requires Recent.

## Verified foundation

- `RunExecutor` supports concurrent roots, including multiple roots in one Thread.
  Its `_active` map tracks task ownership; it is not a presentation tree.
- [Canonical publication](../../src/toolang/execution/stream.py) already orders
  committed events across roots. `RunBegin.parent`, `StepRef.parent`, and
  `Occurrence` supply ownership, nesting, item, lane, and iteration identity.
- [Current activity reads](../../src/toolang/execution/activity.py) reconstruct
  paths from SQLite indexes. [Top](../../src/toolang/cli/common/activity_view.py)
  already has Agent, Thread, Run/List, and Run/Tree presentations.
- `async run` ends its launch Step before the child finishes. The owner cleans up
  its async children before its own `RunEnd`. `spawn` admits an independent root
  in a work Thread; launch provenance does not make it a child.
- Admission persists pending Runs before `RunBegin`, without an equivalent source
  event. Raw Begin/End events alone therefore cannot represent all accepted work.

## Vocabulary and tree shape

| Term | Definition |
| --- | --- |
| Root Run | Physical Run with no parent Step; the unit of Active and Recent. |
| Active Run Tree | Root plus unfinished execution and the ancestors needed to locate it. |
| Run node | Existing physical Run, retaining its real ID and parent Step. |
| Step node | Existing execution Step, retaining its StepRef, kind, and nesting. |
| Current calls | Unfinished model/tool Steps; distinct from accumulated call counts. |
| Root summary | Terminal root identity, outcome, timestamps, consumption, and bounded diagnostic. |

Use Run **and** Step nodes. Runs alone cannot represent concurrent tool calls
inside one Run or distinguish a map, loop, async launch, and await. Do not invent
lane, item, Thread, or Agent tree nodes. Positions are metadata; views may hide
Step layers while preserving root ownership and active branches.

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

This is a structural example. Actual IDs and positions come from execution.
An await is an observation, not a second parent edge to its target. A spawned
root stays separate even when another root awaits it.

Keep every pending/running Run and running Step, plus their ancestors. Drop a
terminal node as soon as it has no retained descendants; recursively prune empty
terminal ancestors. Keep a pending/running root even with no current call. A
terminal launch Step can therefore remain with a running child, while completed
siblings disappear. Preserve existing status values; `await` describes the
operation without inventing a new persisted waiting status. Never infer idle from
an empty current-call set.

## Node consumption

Every retained node has inclusive `stats`: model/tool call counts, input,
cache-read input, output tokens, and spend. Call nodes also retain their own
contribution. Run/structural nodes aggregate contributions from their owned
descendants, including completed descendants that have already been pruned.
Use existing accounting vocabulary and normalization, with explicit known,
unknown, estimated, and partial coverage. Cache-read tokens are part of input.

- Count each physical model/tool attempt at its committed StepBegin. On StepEnd,
  settle the usage and selected cost actually recorded, including available
  partial accounting for failed/canceled calls. Missing usage is not zero.
- Apply a call's contribution once to itself and every ancestor on its ownership
  path. Runtime execution tools count as actual tool calls; work in a spawned
  root contributes to that root, not to the launching root.
- Pruning removes structure, never consumption. Contributions are already in
  ancestors: do not add them again when folding or deleting a node. Maintain no
  completed-child list or lifetime set of consumed event IDs.
- Root totals cover the physical root's accumulated consumption, including prior
  attempts after retry. Reused Step paths have new incarnations and new local
  counters. Seed reopened Run totals from existing durable scope aggregates at
  retry admission; retained prefixes and earlier attempts are not replayed or
  counted again. New Runs start at zero.
- `active_model_calls` and `active_tool_calls` are separate instantaneous counts.
  A root can have `model_calls=10` and `active_model_calls=2`. They are neither
  begin-minus-end window estimates nor accumulated consumption.

These are execution totals, not rolling-window series. Top keeps the existing
Period source for metric columns at every level, including retained tree nodes;
the current accounting reader already supports those scopes. Expose the live
cumulative values separately as Execution total in Details, and reuse them for
columns only when their scope exactly matches the selected range. Active and
Recent root rows use the same Period contract. Filtering, folding, and changing
views never recompute statistics by summing visible inclusive rows. This adds no
new persistent Step/child series. Removing existing series or changing Period
presentation belongs to the separately approved statistics design.

Root completion releases its live subtree and counters. Recent reads the same
root consumption from durable accounting rather than retaining a finished tree
or persisting another copy of its counters.

## Executor maintenance

Add a focused `ExecutionActivity` projection in `execution/live.py`, exposed by
`RunExecutor`. It owns compact descriptors, open parent links, inclusive counters,
and a revision; it owns no tasks, records, message bodies, or view state.
Keep `_active` responsible for execution control.

Update it synchronously at the existing canonical publication boundary, after
the owning record transaction succeeds and before releasing the publication gate
or notifying observers. Stage compact changes with the publication; rollback
applies none. This is a built-in projection, not a tracer or another asynchronous
subscriber that could lag, overflow, or miss its own state changes.

| Committed fact | Projection action |
| --- | --- |
| Run acceptance | Add pending Run with concrete root, Thread, parent, runnable, occurrence, and admission control. |
| RunBegin | Mark the admitted incarnation running and retain its start time. |
| StepBegin | Add compact operation/model/tool metadata and propagate a call begin contribution when applicable. |
| StepEnd | Settle available call accounting, update status, and prune unless an ancestor is still required. |
| Child RunEnd | Remove its unfinished membership and prune; ancestor consumption remains. |
| Root RunEnd | Remove Active membership after existing descendant cleanup; durable terminal root becomes eligible for Recent. |
| RunRetried | Apply invalidation before replacement Begins, reopen the root pending with seeded consumption, and remove its Recent eligibility. |
| Part events / Thread mutations | No contribution; never duplicate calls or copy statistics through fork/rewind. |

Use a private staged acceptance fact, not a new public RunEvent. Admission paths
already know concrete metadata. Stage async child acceptance in the same batch as
its launch StepEnd, so pruning cannot lose the launch ancestor between them.
Cover root, child, spawn, rerun/retry, and dispatch-failure paths. Do not change
`RunBegin` to mean acceptance or fabricate child Runs for inputs waiting on lanes.

Project canonical source facts only, never subscription prefill/context events.
Identify started Step incarnations by their begin cursor and Run admission/retry
by control identity. The projection's `(runtime_epoch, revision)` advances for a
committed batch that changes compact state, including acceptance without a source
event. It is an observation version, not a new event replay cursor. Root retry
and completion update structure and counters in one visible replacement.

Each transition costs the affected ancestor depth, independent of observer count
and completed history. Memory is proportional to current execution plus required
ancestors, with bounded text per descriptor. A large live workload still requires
a proportionally large tree; snapshot limits must not restrict execution.

## Compact snapshot and Recent

Define a separate `TopExecutionSnapshot` contract in `execution/schemas.py`:

| Data | Content |
| --- | --- |
| Boundary | Runtime epoch, projection revision, source boundary, sampled time, and coverage; runtime-only fields are absent for record-backed reads. |
| Active | One root descriptor and a flat parent-linked node list per root, with node stats and current-call counts. |
| Recent | One terminal root summary per ID, including status, start/end times, total stats, and optional short diagnostic. |
| Completeness | Exact eligible counts and exported/omitted counts for roots, nodes, and recent summaries. |

Descriptors contain IDs, parent, kind/operation, runnable/model/tool name, status,
timestamps, occurrence, and existing explicit tool summary. Bound free text to
240 characters. Do not include full inputs/outputs, model messages, streamed text,
tool arguments, authored ASTs, or generated summaries. Keep inspect references.
Terminal errors can use a bounded recorded message; reference-only errors remain
references until explicit inspection.

Recent selects existing root Run records with terminal status and
`finished_at >= sample_time - recent`. Sort by finish time descending, then ID.
Use an indexed terminal-root query; add a partial `(finished_at, id)` index if
needed. Do not use descendant changes, thread activity, or snapshot refreshes as
completion time. Preserve existing `--recent` syntax/default and `--active`
behavior; `--active` hides Recent. An unbounded recent window still obeys the
export cap. No recent-run table or in-memory archive is needed.

Capture an immutable live projection and pin a read-only records/accounting view
under the same publication gate. Perform recent/statistics queries and encoding
after releasing the gate. The paired state must represent the same commit
boundary: completion cannot appear in both datasets or neither, and a reopened
root cannot retain its former Recent row. Apply only complete per-source
replacements; discard older generations/revisions. Window expiry refreshes Recent
on the existing clock tick without changing consumption.

Export at most 200 active roots, 4,000 tree nodes, 1,000 recent summaries, and
1 MiB of serialized activity data. Active roots take priority over Recent;
order roots by latest execution change descending, then ID. Retain roots before
descendant paths, keep ancestry intact, and trim Recent before active paths to
meet the byte cap. Never export dangling branches. Report exact active counts
and retained-root call counts even when paths are omitted; partial output cannot
assert idle. These are transport limits, not consumption or executor limits.

Existing activity adapters compose this contract with Period statistics and
runtime/presence metadata. Hosted reads use the executor projection through the
existing agent activity service. Store-only embedded/offline adapters retain a
bounded current-record implementation of the same tree pruning and summaries;
identify record-backed coverage explicitly. They must not claim current execution
from a stopped or mismatched session. Do not add a server or persist a live tree
just for Top. Preserve existing source-selection behavior in this definition.

Share the projection and latest encoded replacement between viewers where their
queries agree. Keep at most one pending absolute update per viewer; slower viewers
skip superseded snapshots. Reattachment reads current state rather than rebuilding
a tree from missed events. Failure freezes the last sample as stale; absence of
fresh data is not successful completion or cancellation.

## Top views

Active and Recent are eligibility classes, not a requirement for separate panels.
Keep the existing table/navigation structure. With activity sorting, display
Active before Recent; other sorts move whole root groups without interleaving
descendants. All views derive from the same root membership.

| View | Active presentation | Recent presentation |
| --- | --- | --- |
| Agent | Root and current-call counts for the Agent; keep runtime identity where supplied. | Counts by root outcome within Recent. |
| Thread | Root and current-call counts for that Thread. | Counts by root outcome within Recent. |
| Run/List | One root row: runnable, pending/running, counts of model/tool calls now; one current-call label when unique. | One root row: runnable, terminal status, elapsed duration, optional diagnostic. |
| Run/Tree | Root plus Run/Step paths; structural labels, occurrence, current calls, and node consumption. Folding changes presentation only. | Same single root summary; completed internals do not expand. |

Root list can say `review_project · 2 model + 1 tool active`; a running root with
no call shows its current operation or `running`, and pending remains visible.
Agent/Thread summaries distinguish current roots/calls from accumulated model/tool
counts and from Recent success/failure/cancellation counts. A failed child that
is handled does not make its root failed. A root completing while selected keeps
selection on that root's Recent row; a pruned child selects its nearest retained
ancestor. Recent expiry follows existing selection fallback rules.

## Implementation touchpoints and acceptance

Likely files: `execution/{live,types,schemas,stream,activity,accounting,statistics}.py`,
executor admission/publication and `executor/{awaitables,spawn}.py`,
`execution/store.py` for the bounded root query/index, `up/core.py`, existing
activity HTTP/observation adapters, and `cli/common/{activity_view,activity_dashboard}.py`.
Keep schema vocabulary independent of the store and runtime implementation.
No implementation or changelog changes are part of this definition.

| Scenario | Pass condition |
| --- | --- |
| Two roots in one Thread, multiple Threads | One Active tree per root; grouping and identities stay distinct. |
| Map with eight items and two lanes, nested loops, parallel tools | Only real admitted Runs/Steps appear; lane/item metadata and every active branch survive view changes. |
| Async launch and await, independent spawn | Terminal launch ancestor remains as needed; await creates no duplicate child; spawn owns a separate root and statistics. |
| Pending, empty/pure Flow, dispatch failure, cancellation | No-call work remains visible; no accepted root leaks or disappears before its terminal summary. |
| Completed branch followed by new work | Branch structure is pruned, accumulated root/ancestor consumption is unchanged, and current-call counts fall. |
| Ten model begins, two still running, partial/unknown usage | Accumulated count is ten and active count two; known usage/cost and unknown coverage match durable accounting. |
| Nested runtime tools and independently spawned work | Every actual call contributes once per owning scope; summing displayed inclusive rows is unnecessary. |
| Retry, reused StepRef, rerun, rewind, fork | Earlier consumption is retained once; replacement incarnations have fresh local counters; copied history adds nothing. |
| Completion/retry racing a snapshot | Exactly one correct membership at a coherent boundary; terminal root totals match its last live totals and durable accounting. |
| Recent expiry and handled child failure | Window uses root finish time; root outcomes are authoritative; no retained internal terminal tree. |
| Long execution with many completed calls | Tree size follows concurrent structure, while counters continue growing accurately; no history scan on each refresh. |
| Two viewers, slow client, reconnect, source loss | Identical totals at a boundary, bounded pending delivery, current-state convergence, and explicit stale coverage. |
| Caps, all views, filtering, Period changes | Coherent partial trees; exact scope counts, unchanged Period statistics, and separately labeled execution totals. |
| Hosted versus record-backed source | Equivalent structure/stats at the same committed state; stopped or mismatched owners cannot appear confirmed active. |

Use deterministic execution harnesses and existing Top tests. Validate the plan's
links/source claims and `git diff --check`; an implementation must pass the
[default verification](../../AGENTS.md#verification) before each commit.

## Risks and confirmation

The main risks are missing admission paths, double-counting early StepEnd delivery,
dropping async ancestry, mixing Period and execution totals, and pairing snapshots
from different commit boundaries. The transitions and acceptance cases above are
required safeguards. Existing durable aggregates must supply retry/root totals;
if an aggregate is unavailable, expose unknown coverage instead of replaying
unbounded history or manufacturing zero.

All product choices above are proposals for confirmation, including mixed Run/Step
nodes, inclusive live consumption, unchanged Period semantics, and Recent without
completed internals. No further hosting or statistics-storage redesign is implied.
