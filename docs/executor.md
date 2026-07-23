# RunExecutor Design

`RunExecutor` executes one immutable `RunSpec` while keeping durable truth
ahead of external observation.


## Public Shape

```python
class RunExecutor:
    def __init__(self, store: RunStore, ids: IdIssuer) -> None: ...

    def start(
        self,
        spec: RunSpec,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    def stop(
        self,
        *,
        run_id: str,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord: ...

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: ControlTiming,
        request_id: str | None = None,
    ) -> RunControlRecord: ...

    async def shutdown(self) -> None: ...
```

Construction makes the executor immediately available; there is no separate
`open()` phase. `shutdown()` is terminal, cancels all run tasks owned by the
executor, and stops its control monitor. The process composition root owns and
closes the shared `RunStore`; `RunExecutor` and `ThreadManager` receive the same
store and `IdIssuer` instances.

Control polling currently uses an internal default and can move into executor
options when runtime tuning becomes public.

`RunSpec` contains only execution inputs selected for one invocation:

```python
@dataclass(frozen=True, slots=True)
class RunSpec:
    setup: AgentSetup
    state: AgentState
    thread: str
    runnable: str
    input: Message = field(default_factory=lambda: Message.user(""))
    model: str | None = None
    params: Mapping[str, object] | None = None
```

`runnable` is required and resolves to exactly one agic or flow in the captured
program. The spec does not carry an origin, executable kind, run identity,
request identity, or arbitrary transport context. The optional singular model
selector is the caller's per-run model choice; aliases and defaults are parsed
from the captured state config.

There is no `run()`, `execute()`, or `spawn()` variant. `start()` creates the
owner task and returns an awaitable `RunHandle`. The handle exposes its run ID,
executor, and task, and delegates same-process control operations. Its await
path shields the owner task so canceling a waiting HTTP request or TUI action
does not cancel the durable run.


## Acceptance

Binding validates explicit runtime inputs and asks `IdIssuer` for a run ID when
`start()` does not receive one. `RunSpec.thread` must identify an existing
thread. The executor never allocates or implicitly creates threads.

Supplying a run ID is intentionally limited to one-shot script invocation so
logging can be configured at a path containing that ID before execution.
Interactive, job, file, channel, and API callers let the executor allocate the
ID. The supplied or allocated ID must be globally unique in `RunStore`.

`RunStore.accept_start()` uses `BEGIN IMMEDIATE` to:

1. validate the thread;
2. reject a conflicting run ID;
3. insert the pending `RunRecord`;
4. insert start `RunControlRecord(index=0)`;
5. commit the accepted run before its owner task is scheduled.

Duplicate run IDs and duplicate non-null request IDs are rejected. Request IDs
are unique within each control table. Clients that use them must keep them
globally unique across both run and thread controls; `None` disables request
identity without weakening the run or control primary keys.


## Active Ownership

The local active registry maps every active run ID in one run tree to the
top-level owner task and tracer. It is a task-ownership index, not the durable
source of admission truth.

Child runs receive their own process-safe IDs, run records, and index-zero
start controls before `RunBegin`. They execute in the top-level owner task and
inherit its setup, state, and tracer.


## Execution Structure

`RunExecutor` is the process-level singleton. Each owned root run creates one
private `_Execution` that carries the state shared by its recursive run tree.
The implementation is divided by semantic level:

- `executor.py` binds `RunSpec` to immutable execution state and durable IDs;
- `prepare.py` resolves an agic's model, tools, caps, prompt, history, and
  adapter in one pass;
- `runs/agic.py` owns the fixed model-tool cycle for one agic;
- `runs/flow.py` advances through lowered flow statements and updates locals;
- `stmts/` implements lowered statement semantics and chooses a step type;
- `steps/` owns executable step boundaries and their `StepBegin`, part, and
  `StepEnd` events.

Preparation produces one `PreparedAgic` consumed directly by the agic run.
There is no loop plugin or public run-context protocol, and there are no
separate effective-resource, invocation, model-call assembly, or tool-snapshot
layers.

The `_Execution` object emits `RunBegin` and `RunEnd` and dispatches each
accepted executable to the appropriate run body. A top-level agic or flow has
no containing step. When a flow statement invokes another agic or flow,
`steps/run.py` emits the containing run-step events around the child run:

```text
StepBegin(run)
  RunBegin(child)
    child steps
  RunEnd(child)
StepEnd(run)
```

This distinction is made at the event source. The sink and tracer observe the
same canonical event sequence and never filter a synthetic top-level step.


## Control Observation

Any process may call `steer()` or `stop()` because both only write to shared
SQLite truth. The owner process polls pending controls for locally active run
IDs at a short fixed interval.

An `immediate` stop cancels the owner task. `next_step` and `next_call` controls
are consumed at runtime checkpoints. Flows check before statements and calls;
agics check before model and tool calls.

Local submissions deliberately use the same SQLite polling path as remote
submissions. There is no `asyncio.Event`, wake channel, or process-local fast
path whose behavior can diverge.


## Event Ordering

Runtime emits only canonical `RunEvent` values. `RunExecutor` handles each one
in this order:

1. `PersistSink.on_event(event)`;
2. update control statuses referenced by the persisted event;
3. maintain the local active-run index;
4. call the optional `RunTracer`.

Tracer exceptions are logged and ignored. Persistence or control-transition
errors remain execution failures because they compromise durable truth.


## Terminal Behavior

Successful execution emits one `RunEnd(finished)`. Runtime exceptions emit one
`RunEnd(failed)`. If the exception occurred outside an existing step boundary,
the runtime first emits a failed system `StepBegin` / `StepEnd` pair. The
persistence layer never synthesizes steps. Cancellation unwinds steps and child
runs before the root `RunEnd(canceled)`.

If cancellation came from a stop control, `RunEnd.input` references it. The
runtime marks that control `finished` and marks all other pending controls
`failed` because they can no longer reach an applicable checkpoint.


## Persistence

`RunExecutor` always constructs `PersistSink(store)`. History is required for
later model calls, so persistence is not a pluggable executor capability.

`PersistSink` projects only run and step facts. It does not accept controls,
change control statuses, store live events, or translate events into API/CLI
protocols. It persists event payloads directly and owns no parallel local or
output projection state.


## Streaming

Model providers stream deltas whenever supported. The executor never disables
provider streaming based on caller type. A tracer can ignore `PartDelta` and
observe only step or run boundaries.

API SSE, TUI rendering, channel replies, and buffered responses are tracer or
transport implementations outside `toolang.execution`.
