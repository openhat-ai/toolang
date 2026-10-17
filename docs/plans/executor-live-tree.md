# Executor LiveTree and snapshot access

Status: proposed; awaiting implementation approval. This PR contains no implementation.

## Goal and scope

Give every root execution one memory-only `LiveTree` containing its current
Run/Step structure and cumulative statistics for each retained Run. Expose detached
snapshots through the executor, local/remote RunClient and HTTP for Top and other
consumers. Save compact terminal statistics through existing Step/Run completion
writes, preserving transactional Store and canonical publication.

Success means reads use only resident state, completed branches release their
nodes without losing ancestor totals, and the live side branch neither reconstructs
calls nor controls execution or persistence decisions. The Store receives final
values without calculating statistics. This follows
the [compact-core removal](compact-execution-core.md) and
[Event/persistence separation](executor-record-event-boundary.md).

In scope: root ownership naming, LiveTree maintenance, per-Run call counts and model
usage, terminal record fields, snapshot schemas/read interfaces and lifecycle tests.
Out of scope: Top UI, Hub aggregation/discovery, a new embedded HTTP server,
historical/Recent queries, Period/session statistics, retry, persistent indexes, new event types,
subscription/replay protocols and generic projection frameworks. The older
[PR #820](https://github.com/openhat-ai/toolang/pull/820) is not a dependency;
its executor-maintained Activity, statistics indexes and retry design do not apply.

## Runtime ownership

| Current field/type | Required result |
| --- | --- |
| `_ActiveRun` | Rename to `_RootRun`: task ownership, controls and root lifecycle. |
| `RunExecutor._tasks` | Replace with `_root_runs: dict[str, _RootRun]`, keyed by root Run ID. Read the Task from `root.task`; remove the redundant `(run_id, root)` tuple and retain no second Task index. |
| `RunExecutor._active` | Rename to `_active_runs: dict[str, _RootRun]`. Root and child IDs point to the same owner; this remains a flat control-routing index. |
| `_active_lock` | Rename to `_runs_lock`; use this existing registry lock for both indexes with publication-gate-before-registry-lock ordering. |
| `root_run_id`, `thread_id`, `task` | Keep stable identity and task ownership; rename `root_run_id` to `run_id`. |
| `loop` | Remove the stored value; use `task.get_loop()`. |
| `controls`, `interruptions` | Keep task-control state; rename `controls` to `pending_controls`. |
| `event_lock` | Rename to `boundary_lock`; preserve its existing execution/admission serialization. |
| `execution` | Keep the optional `_Execution` reference for executing state and async-task control. |
| `ended`, `early_ends` | Move execution write guards to `_Execution`, preserving duplicate-terminal, late-Part and committed-receipt protection. Startup before `_Execution` exists still uses the existing terminal-finalization path. |
| `observer` | Remove from `_RootRun`; preserve the observation behavior below. |
| New `tree` | Own one `LiveTree`. It alone owns the nodes and Run counters; add no parallel `_RootRun.nodes`, `_RootRun.counters` or global graph. |

Register pending work before it can execute. Remove Run IDs from `_active_runs`
when their Runs terminate, but keep `_root_runs` until the root Task has finished
cleanup. Task completion removes only the matching owner. Preserve independent
spawn Tasks, control authority and `has_owned_work()` shutdown semantics.

Keep `RunTracer` and `TraceObserver`: Chat and script consumers still require
streamed content. Existing `run(..., tracer=...)` contracts, `LocalRunHandle`
waiting and executor-wide observer cleanup remain. Use the launch/owned-task
completion path to finish the observer without storing it on `_RootRun`; do not
add an observer manager. Finish only after committed root terminal publication,
including launch failure and stop fallback, and retain bounded drain/reader
release under cancellation. LiveTree is independent of observers and their speed.

## LiveTree structure and lifetime

`LiveTree` is a small executor-owned data structure, not a service or subscriber.
Its node map contains Run and Step nodes connected by actual execution ownership:
Run parents are launching Steps; a Step belongs to its recorded enclosing Step
or Run. Preserve nesting and occurrence; await does not reparent its target, and
independent spawn belongs to another root/tree.
Export parents before descendants, with roots and siblings in admission/begin
order; consumers do not infer ordering from IDs or reconstruct it from history.

| Node | Snapshot fields |
| --- | --- |
| `RunNode` | Discriminator `type="run"`, `id`, `parent`, current `runnable`, `status`, `created_at`, optional `started_at`/`finished_at`, `occurrence`, `calls: CallCounters` and `usage: Usage \| None`. |
| `StepNode` | Discriminator `type="step"`, `id`, `parent`, `kind`, compact operation/model/tool identity, target and summary, `status`, timestamps and `occurrence`. Model/tool nodes also retain `invoked: bool` and available model `usage`; no descendant totals. |

Use existing scalar status/kind/occurrence vocabulary and necessary fields from
the existing compact StepInfo shapes. Do not expose Records, full calls, ASTs,
input/output values, messages, provider state, arguments, error bodies or content
references. Bound free-text names/targets/summaries to 240 characters when creating
LiveTree nodes; snapshots reuse those small values. Preserve complete structural
IDs. Consumers format labels.

Retain pending/running Runs, unfinished Steps of every supported kind and their
necessary ancestors. Agic uses its actual model/tool Steps, including runtime
tools; do not add a model/tool-only whitelist or manufacture Steps for preparation
or recovery waits. A Run with no current Step remains a valid leaf.

An ended async launch Step remains while its child executes. Prune finished
leaves and then unnecessary finished ancestors using current child membership;
keep no archive of completed children. Keep the root node through task cleanup,
but omit a committed terminal root from live reads. Release its tree with
the owning lifecycle. Restart discards all trees and totals; never rebuild them
from a database. Terminal record values do not restore live state.

## Realtime call counts and reuse of model usage

Each retained Run accumulates its own calls and those of its owned descendants,
including branches already pruned. Contributions follow only the executing Run's
parent chain through ancestor Runs to its root. A model/tool Step retains only its
own invocation facts; no Step aggregates child work.
There is no Agent/Thread/session/Period aggregation, history scan or statistics
index. A yearly model-call total is external work.

`CallCounters` contains only `model_calls: int = 0` and `tool_calls: int = 0`.
These count actual invocation entries, not Steps or completed calls:

| Counter | Increment point | Existing counts that must remain separate |
| --- | --- | --- |
| `model_calls` | Immediately before the real `adapter.stream()` or `adapter.invoke()`, after `before_call()` for Agic; include compaction's adapter invocation. | `_AgicState.model_calls` reserves limits at Step begin. |
| `tool_calls` | Immediately before the real `tool.invoke()`, after preparation and argument validation. | `_AgicState.tool_calls` reserves Agic-local limits and excludes runtime-triggered tools. |

At entry, mark the Step `invoked=True` and increment its owning Run and ancestor
Runs synchronously. A snapshot taken while the call is blocked already includes
it. Completion, failure and cancellation do not increment again or subtract.
Skipped tool batches, invalid arguments, pre-entry cancellation and synthetic compaction read Steps add
zero. Calling `invoke_tool_call()` is insufficient: it can return a preparation
error without invoking a tool. Adapter-internal transport attempts are not extra
executor calls. Active work remains visible in unfinished Step nodes; add no
separate active/completed-call counters.

Model usage has a different update point. Reuse the in-memory
`ModelStepNoted.accounting` produced by the existing normal/error completion paths,
including compaction; apply its contribution once when that Noted is prepared,
before awaiting persistence. Persist the same accounting object as today. Do not
add duplicate usage fields to `ModelStepNoted`, read a stored Step, or move
aggregation into generic Step completion/event-publication paths. Existing
`ToolStepNoted` has only a summary, so no invocation fact can be reused there.

`RunNode.usage` reuses the existing small `Usage` shape for cumulative available
model accounting, independently of `calls`:

| Usage field | Source in `ModelStepNoted.accounting` | Shared extraction rule |
| --- | --- | --- |
| `input_tokens` | `input_tokens` | Reuse the normalized integer. |
| `output_tokens` | `output_tokens` | Reuse the normalized integer. |
| `cache_read_tokens` | Meter `input.cache_read` | Existing `token_meter_quantity()`. |
| `reasoning_tokens` | Meter `output.reasoning` | Existing `token_meter_quantity()`. |
| `cost` | Selected USD amount | Existing `selected_usd_cost()`; do not calculate prices again. |
| `approximate` | Selected cost provenance | Existing `selected_cost_is_approximate()`; combine with logical OR. |

Move `Usage` from `events.py` to `execution/types.py`. Extract the existing
accounting-to-Usage projection into one pure helper in `accounting.py`, shared by
LiveTree and Event summaries without changing the Event wire shape. Keep
`ModelAccounting` and existing Noted usage fields unchanged; retain no accounting
objects, pricing lines, continuation or billing metadata in LiveTree.

Usage becomes visible as soon as its in-memory Noted is available; do not wait
for Run termination or a poll to calculate it. It covers available accounting,
not live token estimates or an assurance of complete provider billing. Before any
accounting is available, `usage` is `None`; absent accounting adds no contribution.
Sum known input/output tokens using integer arithmetic and costs using existing
normalized money addition. For each optional meter/cost field, an unknown value in
any contributed accounting makes that aggregate field `None`; known zero stays
zero. Cache-read is included in input and reasoning in output; neither is added
again to its parent total. Collection item/iteration totals remain existing Noted
progress facts and are outside this scope.

Counter/usage updates belong to the actual invocation and Noted-producing
branches, not persistence, event publication or snapshot reads. Repeated receipts
and terminal delivery add nothing. Add no cursor-based accounting, transaction
staging, ledger, consumed-ID set or generalized callback mechanism. Limits retain
their existing reservation/restoration semantics and mutable totals; admission
and limits never read LiveTree. Completion reads final values as described below;
the Store never reads LiveTree. Existing CLI/historical aggregation remains unchanged.

Same-Run exec changes the runnable without resetting counters. An existing
compaction Run retains resident contributions when continuing; a new Run starts
at zero. Imported history adds none. Pruning neither subtracts consumption nor
adds a finished child's totals again. Elapsed Run time uses its own timestamps,
not summed child durations.

## Terminal records

LiveTree is the only realtime accumulator. Persist compact final values in the
existing completion transaction, not through a second statistics writer:

| Record | Terminal data | Source |
| --- | --- | --- |
| `ModelStepNoted.invoked` / `ToolStepNoted.invoked` | Add `bool \| None`: actual invocation entry for this Step. New terminal model/tool Steps always supply `True` or `False`; `None` means legacy/unknown. | Snapshot the Step node before terminal pruning. |
| Existing `StepRecord.noted` fields | Retain model accounting/continuation, tool summary and other Step facts. Do not add a duplicate `Usage` payload or counters to Flow Steps. | The same in-memory model accounting used to update LiveTree. |
| `RunRecord.noted: RunNoted \| None` | Add `RunNoted(calls: CallCounters, usage: Usage \| None)` for cumulative calls and available model usage for this Run and owned descendants. | Snapshot the Run node after existing child cleanup, before terminal pruning. |

All persisted statistics belong inside a record's `noted`. Add no top-level
`stats`, `counters`, `calls`, `usage`, token or cost fields to any Record, and no
parallel statistics columns or indexes. Lifecycle timestamps and execution-limit
inputs retain their existing execution meanings.

`ModelAccounting` remains the per-call accounting fact; do not rebuild it from
the smaller aggregate `Usage`. A model's accounting and call-entry count are
different facts and must not be inferred from one another or from terminal status.
An invoked failed/canceled Step records `True`; a skipped/pre-entry-canceled
Step records `False`. New Runs ending before invocation record zero call counts.

Pass these immutable values into existing `RunStore.finish_step()` and
`finish_run()` writes, including atomic async admission/exec completion and
startup/stop fallback paths. Store code only validates and encodes values; add no
tree dependency, aggregation SQL, extra write, history scan, new table or index.
Persist no nodes or intermediate snapshots. Keep terminal events compact and do
not add aggregate counters/usage to RunEnd or duplicate model usage in StepEnd.

Extend the existing model/tool Noted codecs for `invoked`; add no Step column.
Add only a nullable `noted` column to `runs`, with `RunNoted` in `types.py` and
its codec in `records.py`, using existing schema upgrade/serialization patterns.
Pending/running records and old records
without captured statistics retain `None`; legacy Noted without `invoked` and
legacy read-only databases remain readable. Do not backfill or mistake absence
for zero. Record inspection reads captured fields without reconstructing calls;
no new historical statistics API is included.

Capture each value once before terminal pruning and use the existing completion
guards/receipt behavior. Retried delivery reuses the same final values and never
adds counters; conflicting terminal values follow existing Store conflict rules.
A failed terminal transaction leaves the live facts intact and adds no separate
statistics recovery path. Existing async-child draining precedes Run termination;
do not change execution scheduling or wait for independent spawned roots.

## Maintenance and snapshots

LiveTree and counters are a synchronous memory side branch of execution. Structural
updates follow existing committed acceptance/begin/end facts directly, without
feeding RunEvents back into execution. Register pending Runs before execution and
install an accepted async child before pruning its finished launch Step. Cover
normal, startup-failure and cancellation cleanup through existing lifecycle paths.
Install structural changes before releasing the existing publication boundary.

Call counts update at invocation entry and usage at Noted preparation, independently
of Store commits: failure to persist an end does not undo work already performed.
Only the detached terminal values enter the existing write transaction. Mutable
tree data participates in neither rollback, durable cursor checks nor control
decisions. Preserve authoritative persistence and canonical publication; do not
build a statistics recovery path around them.

Treat an unexpected LiveTree/counter invariant failure as a fatal programming error;
fail-fast process termination is acceptable. Add no retries, reconstruction,
degraded-mode flags or failure-isolation framework. Normal execution requires only
small direct state updates. Execution write guards remain separate from prunable
display nodes.

Capture root membership, nodes, counters and epoch using existing publication/
registry synchronization, and use the same short synchronization for counter
updates; hold no lock across actual model/tool execution. Copy current values only,
without copying the event ring; serialize and perform network work outside the gate.
Live size follows current execution, not completed history. No export-budget,
pagination, versioned-tree or per-viewer cache subsystem is required in this scope.

## Read interfaces

| Layer | Contract |
| --- | --- |
| `LiveTree` | `snapshot() -> LiveTreeSnapshot`, detached from mutable runtime state. |
| `RunExecutor` | `live_trees(*, root_run_id: str | None = None) -> LiveSnapshot`. |
| `RunClient` | Async `live_trees` with the same parameter and return type. |
| `LocalRunClient` | Require connection and forward to the executor without Store/history reads. |
| `RemoteRunClient` | Request the route below and validate the same schema; no detail-query fallback. |
| HTTP | `GET /api/v1/runs/live`, optional `root_run_id` query, using the existing agent executor and API access policy. Declare before the dynamic Run-detail route. |

Define immutable protocol values in `execution/schemas.py`: `LiveSnapshot` has
`epoch`, `observed_at` and `trees`; each `LiveTreeSnapshot` has `root_run_id`,
`thread_id` and flat parent-linked `RunNode | StepNode` values. Epoch reuses the
executor's canonical stream epoch. Batch metadata occurs once, not on each node.
Define executor-owned mutable maintenance in `execution/live.py` and neutral
`CallCounters` and shared `Usage` vocabulary in `execution/types.py`; schemas do not
depend on LiveTree.

One request returns all nonterminal roots or the exact root ID match. Validate ID
syntax; a well-formed unknown, child or finished ID returns no trees, without a
database lookup. Empty results mean no matching live roots in this reachable
executor. Remote transport failure and invalid response data retain distinct
messages/causes using `RemoteRunClientError`, never empty success or a new error
hierarchy. An executor already stopped rejects reads; HTTP returns 503.

This is absolute snapshot polling, with no subscription or replay contract.
Consumers keep at most one read in flight per source/query and discard obsolete
query responses. A new epoch replaces old state. Snapshots do not promise a
canonical replay cursor: admission can change live state without a public event.
Do not add per-viewer state, caches, a sampling scheduler or version negotiation
to the executor. AgentCore already exposes its executor; add no forwarding service
or new Store read method. Existing hosted endpoints and embedded local clients
are covered; observing scripts through a new HTTP listener is separate work.

## Implementation touchpoints and acceptance

Likely files: `execution/executor/executor.py`, `common.py`, `awaitables.py`,
`spawn.py`, `tool_runtime.py`, lifecycle producers under `runs/` and `steps/`, new
`execution/live.py`, `types.py`, shared projection in `accounting.py`/
`event_projection.py`, value imports in `events.py`, terminal fields/codecs in
`records.py`/`store.py`, `schemas.py`, `client.py`, `remote.py` and `api/routers/runs.py`.
Update `docs/executor.md`, `docs/execution.md`, `docs/api.md` and the implementation
changelog through the repository runnable. Limit Store changes to terminal value
storage and compatible reads; preserve canonical retention and execution behavior.

| Scenario | Pass condition |
| --- | --- |
| Ownership and startup | Concurrent roots, ordinary children and spawn route controls correctly; pending work is visible before begin; rejected Task creation and pre-entry cancellation leave no live ghosts. |
| Structure | All actual Step kinds can be leaves; nested/parallel branches and terminal async launch ancestors retain real ownership; await does not duplicate/reparent work. |
| Actual invocation | Skipped batches, invalid arguments and pre-call cancellation add no calls; real model/tool invocations count once even when they fail; include runtime tools and compaction model calls but exclude synthetic read Steps. |
| Realtime counts | While a real model/tool call is blocked, snapshots already include its entry in the owning Run and ancestors; completion, failure and cancellation never count it again. |
| Accounting reuse | Existing in-memory ModelStepNoted supplies persistence and the shared Usage projection; usage is visible before awaiting its end write, including failure accounting, without duplicate Step usage, pricing calculation or history reads. |
| Totals and pruning | Only the executing Run and ancestor Runs accumulate; pruning, child termination, same-Run exec and compaction continuation neither lose nor double-count contributions; missing accounting, unknown optional values and known zero follow the defined usage semantics. |
| Persistence and receipts | Failed acceptance/begin creates no live node or actual call; a failed end write does not undo an executed call; repeated terminal/receipt delivery performs no counter update and preserves existing causal events. |
| Terminal records | Model/tool Noted invocation facts and final per-Run calls/usage match live values, including skip/failure/cancel/startup and async admission/exec paths; each uses its existing terminal transaction, with no additional write. |
| Record compatibility | Pending/legacy Run Noted remains unknown, new zero-work terminals record zero counts, legacy read-only schemas remain readable, and record selection/serialization exposes captured Noted without backfill or a history scan. |
| Record boundary | All durable statistics are inside Step/Run Noted; neither Record types nor Store tables gain top-level stats/counters/calls/usage fields or parallel statistics storage. |
| Cleanup and observers | Startup failure, normal completion, immediate steer/cancel and canceled stop finalize once; slow/failing/overflowed observers do not affect execution; terminal delivery and bounded drain still work. |
| Snapshot isolation | Concurrent transitions produce one coherent sample; returned values cannot change runtime state; large prior model contexts and completed history do not increase snapshot content. |
| Access parity | Local and HTTP/remote reads validate the same shapes, filtering, empty/stopped/error semantics and epoch replacement; trap Store/history/detail reads to prove they are unused. |
| Scope and regression | No live tables/indexes, historical scans, new events/locks/tasks or Top/Hub implementation; existing limits, compaction continuation, durable records and canonical recovery still pass. |

Use deterministic offline unit/integration tests in `tests/unit/execution/`,
`tests/integration/execution/` and API/client tests. Before implementation commits,
run Ruff checks/formatting, `ty check` and the default `pytest -n auto` suite.
This definition-only PR requires source/link verification and `git diff --check`.

## Risks and approval

The main risks are missed atomic-admission/terminal paths, counting Steps instead
of actual calls, misusing pruned nodes as write guards and finishing observers
before terminal publication. Live snapshots are process-local and can miss short-lived Steps
between polls; consumers needing every transition use the existing event stream.
Terminal records preserve captured final statistics; no historical rollup or
completed-root archive is kept in LiveTree.

No open contract decisions remain in this proposal. Human approval is required
before implementation; merging this plan does not ship runtime behavior.
