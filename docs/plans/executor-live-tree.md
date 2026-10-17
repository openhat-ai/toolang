# Executor LiveTree and snapshot access

Status: proposed; awaiting implementation approval. This PR contains no implementation.

## Goal and scope

Give every root execution one memory-only `LiveTree` containing its current
Run/Step structure and cumulative statistics for each retained Run. Expose detached
snapshots through the executor, local/remote RunClient and HTTP for Top and other
consumers. Keep execution records, transactional Store and canonical publication.

Success means reads use only resident state, completed branches release their
nodes without losing ancestor totals, and observation does not reconstruct calls
or affect execution outcomes. This is a new, limited live-state contract following
the [compact-core removal](compact-execution-core.md) and
[Event/persistence separation](executor-record-event-boundary.md).

In scope: root ownership naming, LiveTree maintenance, per-Run execution totals,
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
| New `tree` | Own one `LiveTree`. It alone owns the nodes and Run statistics; add no parallel `_RootRun.nodes`, `_RootRun.stats` or global graph. |

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
| `RunNode` | Discriminator `type="run"`, `id`, `parent`, current `runnable`, `status`, `created_at`, optional `started_at`/`finished_at`, `occurrence`, and `stats`. |
| `StepNode` | Discriminator `type="step"`, `id`, `parent`, `kind`, compact operation/model/tool identity, target and summary, `status`, timestamps and `occurrence`. No cumulative stats. |

Use existing scalar status/kind/occurrence vocabulary and necessary fields from
the existing compact StepInfo shapes. Do not expose Records, full calls, ASTs,
input/output values, messages, provider state, arguments, error bodies or content
references. Bound free-text names/targets/summaries to 240 characters in snapshots while
preserving complete structural IDs. Consumers format labels.

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

## Per-Run statistics

Each retained `RunNode.stats: RunStats` covers that physical Run's execution and
its transitively owned children, including branches already pruned. Root and child
Runs use the same implementation. Step nodes do not keep subtree accumulators.

| RunStats fields | Meaning |
| --- | --- |
| `model_calls`, `tool_calls` | Integer counts defined below. |
| `input_tokens`, `cache_read_tokens`, `output_tokens` | Known integer subtotals, or `None` when contributions exist but none has a known value; an empty contribution set is zero. |
| `cost_usd` | Known subtotal as normalized decimal text, with the same unknown/empty rule. |
| `usage_complete`, `cost_complete`, `cost_approximate` | Coverage for the token fields and selected cost, plus existing cost-approximation provenance. |

- `model_calls` and `tool_calls` count committed physical model/tool Step begins.
  They count started Steps, including subsequently skipped/canceled Steps with
  committed begins, rather than guaranteeing provider dispatch or billing.
  Advertised calls without a Step begin do not count.
- Settle input, cache-read and output tokens and selected USD cost from the
  existing `ModelAccounting` at Step end, including available partial accounting.
  Reuse accounting normalization, cost selection and money helpers; keep pricing
  details and continuation data out of RunStats.
- Carry known subtotals with usage/cost completeness and approximate-cost flags.
  An unsettled or unreported contribution makes coverage incomplete; absent
  accounting is not a known zero. Cache-read tokens are already part of input.
- Apply each begin/end delta once to the owning Run and its ancestor Runs, skipping
  intervening Steps. Never add a child's full totals again when it ends or prune
  by subtracting its consumption. Spawn has its own totals; await adds no ownership.
- Current call counts can be derived from unfinished Steps during snapshot copy.
  Elapsed Run time uses its own timestamps, not summed child durations. Retain
  current limit enforcement and compaction-continuation accounting semantics;
  do not substitute observation totals for `_RunLimitState` in this change.

Same-Run exec changes the current runnable without resetting its totals; continuing
an existing compaction Run preserves its resident contributions without reseeding
them from history. A new Run starts at zero; imported history adds no consumption.

Only current nodes and their small counters are added. There is no lifetime
contribution log, per-viewer accumulator, second normalized-usage store or new
set of consumed event IDs.

## Commit and snapshot boundaries

Update the tree directly from existing acceptance/begin/end facts and write
results, not by feeding RunEvents back into execution. Stage required node and
accounting changes alongside the existing transaction; apply them after commit
and before releasing the canonical publication gate. Rollback changes neither
tree nor totals. A committed boundary contributes once even when delivery later
fails or the Step's terminal call is repeated.

Cover root/child acceptance before RunBegin, normal begins/ends, async admission
that atomically ends a launch Step, spawn/exec receipts, automatic compaction,
early cancellation and terminal fallback. Preserve the existing causal event order.
Install an accepted async child before pruning its atomically finished launch Step
in the same committed batch, so its ancestor cannot disappear between transitions.
Observer failures cannot roll back execution or gate tree maintenance.

Capture root membership, nodes, Run stats and epoch under the same existing
publication synchronization, with consistent registry lock ordering. Add no new
lock or awaited work to that critical section. Copy only current immutable values;
do not copy the canonical event ring. Serialize and perform network work outside
the gate. Read cost follows exported live structure, not completed history.
LiveTree pruning is not proof of execution completion: preserve write guards
separately rather than interpreting a missing node as a terminal record.

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
RunStats vocabulary in `execution/types.py`; schemas do not depend on LiveTree.

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
`execution/live.py`, `types.py`, `schemas.py`, `client.py`, `remote.py`, and
`api/routers/runs.py`. Update `docs/executor.md`, `docs/execution.md`, `docs/api.md`
and the implementation changelog through the repository runnable. Keep Store SQL,
record codecs, canonical retention and existing public execution behavior unchanged.

| Scenario | Pass condition |
| --- | --- |
| Ownership and startup | Concurrent roots, ordinary children and spawn route controls correctly; pending work is visible before begin; rejected Task creation and pre-entry cancellation leave no live ghosts. |
| Structure | All actual Step kinds can be leaves; nested/parallel branches and terminal async launch ancestors retain real ownership; await does not duplicate/reparent work. |
| Totals and pruning | Every retained Run reports inclusive totals; pruning, child termination, same-Run exec and compaction continuation neither lose nor double-count contributions; partial/unknown/estimated accounting remains explicit. |
| Transactions and receipts | Rolled-back acceptance/begin/end changes no tree/counter; repeated terminal delivery and early-committed receipts contribute once and preserve causal events. |
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
