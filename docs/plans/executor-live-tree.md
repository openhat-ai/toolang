# Executor LiveTree and snapshot access

Status: approved in PR #841; implemented and verified against the acceptance scenarios below.

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

In scope: root ownership naming, LiveTree maintenance, per-Run call counts, token
usage and final costs, separation of TokenUsage/ModelCost, terminal Noted fields,
snapshot schemas/read interfaces and lifecycle tests.
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
| `RunNode` | Discriminator `type="run"`, `id`, `parent`, current `runnable`, `status`, `created_at`, optional `started_at`/`finished_at`, `occurrence`, `calls: CallCounters`, `usage: TokenUsage \| None`, `cost: float \| None` (USD) and `cost_approximate: bool`. |
| `StepNode` | Discriminator `type="step"`, `id`, `parent`, `kind`, compact operation/model/tool identity, target and summary, `status`, timestamps and `occurrence`. Model/tool nodes also retain `invoked: bool` and their own available model usage/final cost; no descendant totals. |

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

## Realtime call counts, token usage and costs

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

Separate token usage from financial information and remove the `ModelAccounting`
wrapper. Keep neutral immutable values in `execution/types.py`:

| Type | Fields and responsibility |
| --- | --- |
| `TokenUsage` | Exactly `input_tokens`, `output_tokens`, `cache_read_tokens`, `reasoning_tokens`; token quantities only. Rename/move Event `Usage` from `events.py` and remove money/approximation fields. |
| `ModelCost` | Direct final `amount: float \| None`, `currency: str \| None`, `source` (`reported`, `estimated`, `zero`, `unknown`), `complete: bool`, `pricing`, `meters` and `lines`. Pricing and calculation evidence reuse `ModelPricing`, `ModelUsageMeter` and `ModelCostLine`; billing is not restricted to tokens. |
| `ModelStepNoted` | Direct `usage: TokenUsage \| None`, `cost: ModelCost \| None`, existing `continuation` and the invocation fact below. Remove `accounting`; add no replacement envelope. |

Remove the old amount-only `ModelCost` along with `ModelAccounting`; add no
`CostAmount`, separate reported/estimated amount objects or duplicate total.
`ModelCost` retains the selected amount and the pricing/calculation basis, not
unused alternate totals. Calculation lines explain the catalog estimate; when
the source is reported, the final amount may differ from their sum. Only the
selected pricing plan/rates and necessary billing evidence are retained.
Unknown cost has no amount/currency and is incomplete; known zero has amount
zero and retains its currency. An estimated amount may be incomplete when some
billing components lack prices.

Replace `build_model_accounting()` with direct TokenUsage/ModelCost construction
in existing result preparation. Normalize provider data and calculate/select
cost once, including available error usage and compaction. The provider contract
`base.types.run.ModelUsage` stays unchanged. Preserve these rules:

| Value | Source | Normalization rule |
| --- | --- | --- |
| `TokenUsage.input_tokens` / `output_tokens` | Provider totals | Reuse normalized integers. |
| `TokenUsage.cache_read_tokens` | Meter `input.cache_read` | Preserve the unique, integral token-meter rule; ambiguous/missing/wrong-unit values are unknown. |
| `TokenUsage.reasoning_tokens` | Meter `output.reasoning` | The same token-meter rule. |
| `ModelCost.amount` / `currency` | Selected amount | Preserve current source/currency selection, with no currency conversion. Persist the final amount once, in its selected currency. |
| `ModelCost.source` / `complete` | Cost provenance and calculation coverage | Preserve known zero, reported/estimated and incomplete-calculation semantics. Derive approximation from source (`estimated`/`unknown`), without another stored flag. |

Consume the same in-memory Noted usage and final cost once when prepared, before
awaiting persistence. LiveTree immediately updates the executing Step and Run
ancestors. Existing `ToolStepNoted` has no reusable invocation count. Collection
item/iteration totals remain their existing Noted progress facts.

Event `StepSummary.usage` directly receives `noted.usage`; add separate
`cost: float | None` (USD) and `cost_approximate: bool` from `noted.cost`.
Read the selected amount only when its currency is USD; otherwise expose unknown
USD cost, without dropping the selected currency from the durable ModelCost.
No second TokenUsage definition, money inside TokenUsage, cost-summary DTO, meter scan,
price selection or database read is required for Event construction. LiveTree
retains only these small values and accumulated totals. Full ModelCost evidence stays
with Step Noted; neither Events nor live nodes contain pricing or cost lines.

This changes the Event wire shape: move `usage.cost`/`usage.approximate` to
`StepSummary.cost`/`cost_approximate`. Update built-in presenters/decoders together
and document subscriber upgrade/reconnect; add no dual Event format. Adapt limits
and record inspection to TokenUsage/ModelCost access while preserving their policies and
historical aggregation algorithms. Remove obsolete extraction helpers when unused;
add no compatibility property layer or new observer/aggregation framework.

New model Noted encoding has `usage`, `cost`, `cont` and `invoked`, with no
`accounting` envelope. Persist TokenUsage and direct ModelCost fields once.
Raw meters and cost lines preserve billing evidence, not another normalized
TokenUsage. Implement only the new format: no old-record decoding, field aliases,
conversion, backfill or compatibility tests. Bump the Store schema version and
require fresh storage; reject older schemas through the existing unsupported-
schema error, with no migration/recovery machinery.
Record inspection/API serialization uses `noted.usage`/`noted.cost`; document
that field migration alongside the Event migration, without legacy field aliases.

Token usage and cost become visible when Noted is available; do not wait for Run end or
a poll. They cover available results, not streaming token estimates or complete
provider billing. With no available contribution, `usage`/`cost` is `None` and
`cost_approximate` is false. Sum input/output using integers and costs using
existing normalized money addition. Any unknown optional token field or selected
USD amount in a contributed result makes its aggregate field `None`; known zero
stays zero. Combine cost approximation with logical OR. Cache-read is included in
input and reasoning in output; do not add either again to its parent total.

Counter/usage/cost updates belong to the actual invocation and Noted-producing
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
| `ModelStepNoted.invoked` / `ToolStepNoted.invoked` | Add `bool`: actual invocation entry for this Step. Terminal model/tool Steps supply `True` or `False`. | Snapshot the Step node before terminal pruning. |
| `ModelStepNoted` | `usage`, full `cost`, existing `continuation` and `invoked`. Keep tool summaries and other Step Noted facts unchanged; no counters on Flow Steps. | The same in-memory TokenUsage/ModelCost used to update LiveTree. |
| `RunRecord.noted: RunNoted \| None` | Add `RunNoted(calls: CallCounters, usage: TokenUsage \| None, cost: float \| None, cost_approximate: bool)` for this Run and owned descendants. Cost is the final USD total, with no merged prices/calculation lines. | Snapshot the Run node after existing child cleanup, before terminal pruning. |

All persisted statistics belong inside a record's `noted`. Add no top-level
`stats`, `counters`, `calls`, `usage`, token or cost fields to any Record, and no
parallel statistics columns or indexes. Lifecycle timestamps and execution-limit
inputs retain their existing execution meanings.

Full ModelCost evidence remains per-call; do not rebuild it from aggregate usage/cost.
Result usage/cost and invocation entry are different facts and must not be
inferred from one another or from terminal status.
An invoked failed/canceled Step records `True`; a skipped/pre-entry-canceled
Step records `False`. New Runs ending before invocation record zero call counts.

Pass these immutable values into existing `RunStore.finish_step()` and
`finish_run()` writes, including atomic async admission/exec completion and
startup/stop fallback paths. Store code only validates and encodes values; add no
tree dependency, aggregation SQL, extra write, history scan, new table or index.
Persist no nodes or intermediate snapshots. Keep terminal events compact and do
not add aggregate statistics to RunEnd or duplicate TokenUsage/ModelCost in StepEnd.

Extend model/tool Noted codecs for the new fields; add no Step column. Add only
a nullable `noted` column to `runs`, with `RunNoted` in `types.py` and its codec
in `records.py`. Pending/running records keep `noted=None`; new zero-work terminal
Runs record zero calls and no usage/cost contribution. Record inspection reads
captured fields directly. No historical statistics API or old-record support is
included.

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

Call counts update at invocation entry and usage/cost at Noted preparation, independently
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
`CallCounters`, `TokenUsage`, `ModelCost` and `RunNoted` vocabulary
in `execution/types.py`; schemas do not depend on LiveTree.

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
`execution/live.py`, `types.py`, TokenUsage/ModelCost construction in `accounting.py`,
value access in `event_projection.py`/`executor/limits.py`, schemas/imports in
`events.py`, terminal fields/codecs in `records.py`/`store.py`, `schemas.py`,
`client.py`, `remote.py` and `api/routers/runs.py`.
Adapt value access in `inspection/trees.py` and CLI inspection/progress consumers;
keep their presentation and historical aggregation algorithms. Update Event
decoders/stream fixtures only as required by the TokenUsage/ModelCost field split.
Update `docs/executor.md`, `docs/execution.md`, `docs/api.md` and the implementation
changelog through the repository runnable. Document the new storage format and
fresh-data requirement. Limit Store changes to terminal value storage and its
schema version; preserve canonical retention and execution behavior.

| Scenario | Pass condition |
| --- | --- |
| Ownership and startup | Concurrent roots, ordinary children and spawn route controls correctly; pending work is visible before begin; rejected Task creation and pre-entry cancellation leave no live ghosts. |
| Structure | All actual Step kinds can be leaves; nested/parallel branches and terminal async launch ancestors retain real ownership; await does not duplicate/reparent work. |
| Actual invocation | Skipped batches, invalid arguments and pre-call cancellation add no calls; real model/tool invocations count once even when they fail; include runtime tools and compaction model calls but exclude synthetic read Steps. |
| Realtime counts | While a real model/tool call is blocked, snapshots already include its entry in the owning Run and ancestors; completion, failure and cancellation never count it again. |
| Usage/cost boundary | TokenUsage has exactly four token fields; ModelStepNoted directly holds TokenUsage/ModelCost. Events reuse TokenUsage and carry only final amount/approximation; no full ModelCost, duplicate DTO, meter scan, price selection or history read. Built-in clients consume the split Event fields. |
| Cost persistence | New Noted round-trips TokenUsage and direct ModelCost fields, preserving the selected currency, source, completeness and pricing/calculation evidence. There is no CostAmount wrapper or duplicate final amount. Unknown/zero and token-meter ambiguity retain their semantics; limits/inspection retain behavior. |
| Totals and pruning | Only the executing Run and ancestor Runs accumulate; pruning, child termination, same-Run exec and compaction continuation neither lose nor double-count contributions; missing usage/cost, unknown optional values and known zero follow the defined semantics. |
| Persistence and receipts | Failed acceptance/begin creates no live node or actual call; a failed end write does not undo an executed call; repeated terminal/receipt delivery performs no counter update and preserves existing causal events. |
| Terminal records | Model/tool Noted invocation facts and final per-Run calls/usage/cost match live values, including skip/failure/cancel/startup and async admission/exec paths; each uses its existing terminal transaction, with no additional write. |
| Record defaults | Pending/running Noted is absent; new zero-work terminals record zero calls; record selection/serialization exposes captured Noted without history reads. Old formats have no decoding, migration or compatibility path. |
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

The design was approved in PR #841. Implementation and verification follow the
contracts and acceptance scenarios above.
