# Executor

`RunExecutor` owns process-local execution. It accepts durable work, runs the
bound plan, writes complete facts directly to RunStore, and publishes value-only
notifications after commit. [Execution](execution.md) owns the public lifecycle,
[Records](records.md) the storage contract, and [Events](events.md) observation.

## Public boundary

The composition root gives RunExecutor and ThreadManager the same RunStore and
IdIssuer. It supplies paired Setup/State callbacks for request resolution and
concrete lifecycle/workspace inputs. The process owner closes the Store only
after executor shutdown. Exact signatures live in
[executor.py](../src/toolang/execution/executor/executor.py).

| Operation | Responsibility |
| --- | --- |
| `start()` | Idempotent lifecycle initialization |
| `run(RunSpec or RunRequest)` | Resolve/validate, atomically admit, launch ownership, return LocalRunHandle |
| `rerun(source or RerunRequest)` | Bind the source invocation to fresh Setup/State and accept a new root |
| `steer()`, `cancel()`, `cancel_control()` | Persist controls, optionally guarded by the expected live invocation |
| `live_trees()` | Sample resident execution structure without Store reads |
| `stop()` | Stop admission/control observation and drain/cancel owned tasks and observers |

RunSpec captures Setup, State, thread, bindings, limits, typed CallInput, and any
workdir, resource ceilings, authored provenance, horizon, launch, driver, or
conversation context. These are execution values; transport parsing and session
state stay with callers. A resolved spec must satisfy the target contract.

A LocalRunHandle exposes its Run ID and same-process controls. Awaiting it returns
the terminal RunRecord and shields execution from cancellation of one waiter.
Transport disconnection does not cancel accepted work.

## Admission and task ownership

Admission validates the target and input, reserves an ID, verifies the thread is
idle for an independent root, and commits the pending Run plus applied index-zero
run control before scheduling the owner task. Request IDs are unique control
identifiers, not arbitrary retry keys. A failed ID reservation/write can leave a
gap; it never returns an ID to circulation.

If task launch fails after admission, the ordinary terminal path persists a
failed Run, closes pending controls, and publishes RunEnd before surfacing the
startup error. It does not remove the accepted receipt or invent a RunBegin.
Shutdown drains terminal fallback, resource cleanup, and observers even when a
waiter is canceled. Stop is terminal for that executor instance.

A synchronous call keeps its source Step open until the child terminates.
Async run commits child admission and the source handle/output, then closes the
launch Step while retaining parent-tree ownership. The parent tree shares its
budgets and cannot complete while owned async descendants remain active.
Await records dependencies and the target result/error without reparenting the
child or charging its usage a second time.

Spawn accepts an independent root atomically with its source Step outcome and,
when necessary, a new `work_` thread. Model-requested spawn can reuse a permitted
idle local thread. Source-Step receipts deduplicate admission; delivery failure
or caller cancellation after commit does not undo acceptance. Failed dispatch
terminalizes the admitted root and preserves its handle. No recovery relaunches
accepted work after a process exit. [Records](records.md#spawn-admission-and-handles)
owns the persisted representation.

## Execution structure

| Owner | Responsibility |
| --- | --- |
| [common.py](../src/toolang/execution/executor/common.py) | BoundRun and runtime local/value context |
| [resources.py](../src/toolang/execution/executor/resources.py) | Effective resources, ceilings, and module-scoped selectors |
| [frame.py](../src/toolang/execution/executor/frame.py) | Agic frame, model/adapter selection, templates, tools, and budgets |
| [assembly](../src/toolang/execution/assembly/) | Provider-neutral instructions/messages, recall, tool replies, and run results |
| [steps](../src/toolang/execution/executor/steps/) | Model, tool, child Run, flow parallel/loop/value, agent, and human units |
| [awaitables.py](../src/toolang/execution/executor/awaitables.py), [spawn.py](../src/toolang/execution/executor/spawn.py) | Async results and independent-root admission |
| [limits.py](../src/toolang/execution/executor/limits.py) | Per-agic call limits and shared root budgets |
| [live.py](../src/toolang/execution/live.py) | Resident live nodes and aggregate propagation |

Accepted code, output contracts, Setup, and workspace grants stay bound. Named
calls adopt a compatible latest State; inline bodies stay with their containing
plan. Model-call resources use current State under the active selector path.
Exec replaces the current runnable in the same Run, replaces its locals and
rules, and keeps the accepted output contract. See [State capture](execution.md#state-capture)
and [Agic](agic.md).

## Controls and terminalization

External steer/cancel controls are durable before adoption. Polling notices
cross-process writes; local controls need not be submitted through the owner
object. Expected-control guards require the addressed invocation to be live
locally. Immediate cancellation interrupts the owner after observation;
`next_step` and `next_call` defer application to their checkpoints.

Control effects and their source/consuming references commit together. An
applied control reports its effect, not Run success. Pending controls become
`wontapply` when no matching checkpoint remains. A failure has one original
error source; references preserve propagation without copying messages into
unrelated records. Terminal Steps and Runs are finalized once.

Every write transaction stages notifications. Commit publishes them; failure
publishes nothing. Tracers and network subscribers observe independently. Their
failure cannot replace persistence, change control application, or fail the Run.
[Events](events.md) owns ordering and reconnect normalization.

## Resident LiveTree

Executor memory owns the active forest and its indexes. Under the publication
gate, callers copy parent-first Run/Step nodes and current subtree aggregates.
`live_trees(root_run_id=None)` returns a LiveSnapshot with epoch and observation
time; an unknown, completed, or child filter has no active tree. A stopped
executor rejects reads. [API](api.md#live-execution-snapshots) exposes this sample.

Nodes contain identities, operations, statuses/times, compact labels, occurrence,
and invocation/usage/USD-cost facts. They exclude arguments, complete calls,
output bodies, provider metadata, references, and mutable implementation objects.
Completed leaves are pruned; async launch ancestors remain until their children
finish. Aggregate deltas preserve contributions of pruned children. Independent
spawn roots aggregate separately.

Sampling performs no historical Store reads and adds no viewer-owned task or
subscription. Polling can miss short-lived Steps; use events for transitions and
records for history. Terminal reports capture final aggregates before resident
nodes are discarded. [Top](top.md) and [statistics](execution-statistics.md) consume
their respective live and durable views.

## Verification anchors

- [Startup/admission](../tests/integration/execution/test_run_startup.py), [async work](../tests/integration/execution/test_async_run_scenarios.py)
- [Spawn](../tests/integration/execution/test_spawn_scenarios.py), [controls](../tests/integration/execution/test_control_scenarios.py)
- [LiveTree](../tests/integration/execution/test_live_tree.py), [resident state](../tests/unit/execution/test_live_tree_state.py)
- [Store atomicity](../tests/integration/execution/test_store_atomicity_scenarios.py), [runtime control commits](../tests/integration/execution/test_runtime_control_commit.py)
