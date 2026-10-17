# Executor LiveTree and snapshot access

Status: proposed; awaiting implementation approval. This PR contains no implementation.

## Goal and scope

Give every root execution one memory-only `LiveTree` containing its current
Run/Step structure and cumulative statistics for each retained Run. Expose detached
snapshots through the executor, local/remote RunClient and HTTP for Top and other
consumers. Keep execution records, transactional Store and canonical publication.

Success means reads use only resident state, completed branches release their
nodes without losing ancestor totals, and the live side branch neither reconstructs
calls nor participates in execution/control and persistence decisions. This follows
the [compact-core removal](compact-execution-core.md) and
[Event/persistence separation](executor-record-event-boundary.md).

In scope: root ownership naming, LiveTree maintenance, per-Run execution counters,
snapshot schemas/read interfaces and necessary lifecycle tests. Out of scope:
Top UI, Hub aggregation/discovery, a new embedded HTTP server, historical/Recent
queries, Period/session statistics, retry, persistent indexes, new event types,
subscription/replay protocols and generic projection frameworks. The older
[PR #820](https://github.com/openhat-ai/toolang/pull/820) is not a dependency;
its executor-maintained Activity, durable statistics and retry design do not apply.

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
| `RunNode` | Discriminator `type="run"`, `id`, `parent`, current `runnable`, `status`, `created_at`, optional `started_at`/`finished_at`, `occurrence`, and `counters`. |
| `StepNode` | Discriminator `type="step"`, `id`, `parent`, `kind`, compact operation/model/tool identity, target and summary, `status`, timestamps and `occurrence`. No cumulative stats. |

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
from a database. Existing durable execution facts remain unchanged.

## Counters and reuse of existing facts

Each retained `RunNode.counters: RunCounters` covers calls executed by that Run and
its owned descendants, including branches already pruned. A contribution follows
only the executing Run's parent chain through ancestor Runs to its root; Step
nodes do not accumulate it. There is no Agent/Thread/session/Period aggregation,
history scan or statistics persistence. A yearly model-call total is external work.

The numeric fields are explicit and reuse existing single-call facts:

| Counter | Existing source | Existing overlap |
| --- | --- | --- |
| `model_calls` | Actual entry into `adapter.stream()` or `adapter.invoke()`. | `_AgicState.model_calls` is a limit reservation at Step begin, not this execution count. |
| `tool_calls` | Actual entry into `tool.invoke()` after preparation and argument validation. | `_AgicState.tool_calls` is an Agic-local limit reservation and excludes runtime-triggered tools. |
| `input_tokens` | `ModelStepNoted.accounting.input_tokens`. | Event `Usage`, `_RunLimitState`, CLI `Metrics`. |
| `output_tokens` | `ModelStepNoted.accounting.output_tokens`. | Event `Usage`, `_RunLimitState`, CLI `Metrics`. |
| `cache_read_tokens` | Accounting meter `input.cache_read`. | Event `Usage`, CLI `Metrics`. |
| `reasoning_tokens` | Accounting meter `output.reasoning`. | Event `Usage`, CLI `Metrics`, historical `TreeMetrics`. |
| `cost` | Existing `selected_usd_cost(accounting)`. | Event `Usage.cost`, `_RunLimitState.cost`, CLI `Metrics.cost`. |

The Noted paths name in-memory values already produced by the call, not database
reads. `ToolStepNoted` contains only a summary; it has no call counter to reuse.

Use integer call counts and token sums; `cost` uses existing normalized USD money
arithmetic. Token/cost totals cover completed calls with returned accounting;
in-flight calls have not settled usage. Unknown settled contributions propagate
`None` for the affected total; an empty contribution set is zero. Preserve the
existing `approximate` marker with selected costs. Cache-read is included in input
and reasoning is included in output; neither is added again to its parent total.

Keep `ModelAccounting` as the sole per-call accounting fact, built once from the
returned usage, including available usage on model failure. Persist that same
object through `ModelStepNoted(accounting=...)`; do not add a second durable
`noted.counters`, change record codecs or recalculate pricing for LiveTree.
Prepare that fact when the result/error usage becomes available, before cancellable
presentation or persistence, and carry it through the existing Noted completion path.
Move the existing small `Usage` value vocabulary from `events.py` to
`execution/types.py`. One pure projection in `accounting.py` supplies these values
to counters and the existing Event summary, preserving the Event wire shape.
LiveTree needs selected values, not pricing lines, continuation or billing
metadata. Collection item/iteration totals stay with their existing Noted progress
facts and are not additive model/tool consumption counters.

Count calls in the actual execution branches, not Run/Step persistence, event
publication or snapshot reads. For Agic, increment after `before_call()` and before
the adapter invocation; for compaction use its adapter invocation as well. Tool
accounting belongs beside the real `tool.invoke()`, not before `invoke_tool_call()`
which can return a preparation error without invoking a tool. Skipped tool batches,
invalid arguments, pre-call cancellation and synthetic compaction read Steps do not
add calls. Once invocation begins, failure/cancellation still counts one call.
Adapter-internal transport attempts are not extra executor calls.

Consume a model's already produced accounting once in its normal/error completion
branch. Records and counters receive the same fact; each ancestor only performs
addition. Repeated receipt/terminal delivery is not another model/tool execution
and performs no counter update. Do not add cursor-based accounting, transaction
staging, a ledger, a consumed-ID set or a generalized callback mechanism.

Leave limit enforcement separate: its reservation timing and compaction restoration
have different semantics. Reuse the accounting values/helpers, not its mutable
totals, and never make admission, limits or persistence read LiveTree counters.
Existing CLI/historical aggregation is reference for shared fields, not an
instruction to redesign those consumers in this change.

Same-Run exec changes the runnable without resetting counters. An existing
compaction Run retains resident contributions when continuing; a new Run starts
at zero. Imported history adds none. Pruning neither subtracts consumption nor
adds a finished child's totals again. Elapsed Run time uses its own timestamps,
not summed child durations.

## Maintenance and snapshots

LiveTree and counters are a synchronous memory side branch of execution. Structural
updates follow existing committed acceptance/begin/end facts directly, without
feeding RunEvents back into execution. Register pending Runs before execution and
install an accepted async child before pruning its finished launch Step. Cover
normal, startup-failure and cancellation cleanup through existing lifecycle paths.
Install structural changes before releasing the existing publication boundary.

Counter updates follow actual invocation/results independently of Store commits:
failure to persist an end does not undo work already performed. Neither counters
nor tree data participates in write transactions, rollback, durable cursor checks
or control decisions. Preserve authoritative persistence and canonical publication
unchanged; do not build a statistics recovery path around them.

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
RunCounters vocabulary in `execution/types.py`; schemas do not depend on LiveTree.

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
or Store method. Existing hosted endpoints and embedded local clients are covered;
observing scripts through a new HTTP listener is separate work.

## Implementation touchpoints and acceptance

Likely files: `execution/executor/executor.py`, `common.py`, `awaitables.py`,
`spawn.py`, `tool_runtime.py`, lifecycle producers under `runs/` and `steps/`, new
`execution/live.py`, `types.py`, shared projection in `accounting.py`/
`event_projection.py` and value imports in `events.py`, `schemas.py`, `client.py`, `remote.py`, and
`api/routers/runs.py`. Update `docs/executor.md`, `docs/execution.md`, `docs/api.md`
and the implementation changelog through the repository runnable. Keep Store SQL,
record codecs, canonical retention and existing public execution behavior unchanged.

| Scenario | Pass condition |
| --- | --- |
| Ownership and startup | Concurrent roots, ordinary children and spawn route controls correctly; pending work is visible before begin; rejected Task creation and pre-entry cancellation leave no live ghosts. |
| Structure | All actual Step kinds can be leaves; nested/parallel branches and terminal async launch ancestors retain real ownership; await does not duplicate/reparent work. |
| Actual invocation | Skipped batches, invalid arguments and pre-call cancellation add no calls; real model/tool invocations count once even when they fail; include runtime tools and compaction model calls but exclude synthetic read Steps. |
| Accounting reuse | The same ModelAccounting supplies Noted persistence and the shared Usage projection for events/counters; pricing is computed once, with no duplicate durable counter payload or new history reads. |
| Totals and pruning | Only the executing Run and ancestor Runs accumulate; pruning, child termination, same-Run exec and compaction continuation neither lose nor double-count contributions; optional usage/cost and approximation retain the defined semantics. |
| Persistence and receipts | Failed acceptance/begin creates no live node or actual call; a failed end write does not undo an executed call; repeated terminal/receipt delivery performs no counter update and preserves existing causal events. |
| Cleanup and observers | Startup failure, normal completion, immediate steer/cancel and canceled stop finalize once; slow/failing/overflowed observers do not affect execution; terminal delivery and bounded drain still work. |
| Snapshot isolation | Concurrent transitions produce one coherent sample; returned values cannot change runtime state; large prior model contexts and completed history do not increase snapshot content. |
| Access parity | Local and HTTP/remote reads validate the same shapes, filtering, empty/stopped/error semantics and epoch replacement; trap Store/history/detail reads to prove they are unused. |
| Scope and regression | No live tables/indexes, historical scans, new events/locks/tasks or Top/Hub implementation; existing limits, compaction continuation, durable records and canonical recovery still pass. |

Use deterministic offline unit/integration tests in `tests/unit/execution/`,
`tests/integration/execution/` and API/client tests. Before implementation commits,
run Ruff checks/formatting, `ty check` and the default `pytest -n auto` suite.
This definition-only PR requires source/link verification and `git diff --check`.

## Risks and approval

The main risks are missed atomic-admission paths, counting a committed Step twice,
misusing pruned nodes as write guards and finishing observers before terminal
publication. Live snapshots are process-local and can miss short-lived Steps
between polls; consumers needing every transition use the existing event stream.
No persistent historical totals or completed-root archive is promised.

No open contract decisions remain in this proposal. Human approval is required
before implementation; merging this plan does not ship runtime behavior.
