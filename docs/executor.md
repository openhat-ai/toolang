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
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    def rerun(
        self,
        source: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        ceiling: CeilingSpec = CeilingSpec(),
        model: str | None = None,
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    def retry(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        anchor: StepPath | str | None = None,
        ceiling: CeilingSpec = CeilingSpec(),
        model: str | None = None,
        limits: RunLimits | None = None,
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

    def cancel_control(
        self,
        *,
        run_id: str,
        index: int,
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

`RunSpec` contains the immutable inputs required to start one invocation:

```python
@dataclass(frozen=True, slots=True)
class RunSpec:
    setup: AgentSetup
    state: AgentState
    thread: str
    runnable: str
    ceiling: CeilingSpec = CeilingSpec()
    input: Percept = ()
    model: str | None = None
    args: Mapping[str, object] | None = None
```

`runnable` is required and resolves to exactly one agic or flow in the captured
program. The spec does not carry an origin, executable kind, run identity,
request identity, or arbitrary transport context. The optional singular model
selector is the caller's per-run model choice; aliases and defaults are parsed
from the captured state config. `ceiling` is the caller's immutable
selector-based specification for the complete root run tree. `input` is the
primary multimodal input; `args`
contains values for the runnable's declared `params`. The executor validates
both before accepting the run and constructs the user message internally. It
keeps the original canonical `Percept` for the start control and durable
history, while language-owned input coercion initializes `_` with the
runnable's declared primary type.

`AgentSetup.limits` is the captured default for a new run. Passing `limits` to
`start()` replaces that default for one root run tree. Config, CLI, and HTTP
parsing remain caller concerns and are not part of the executor contract.

There is no `run()`, `execute()`, or `spawn()` variant. `start()`, `rerun()`,
and `retry()` create an owner task and return an awaitable `RunHandle`.
`rerun()` loads the source invocation from durable truth and starts a new root
against the supplied current setup and state. `retry()` keeps the root ID,
reopens a terminal run, and resumes after its effective committed prefix. An
omitted retry anchor prefers the latest visible failed, canceled, or running
non-system step and falls back to a runtime system-failure step. The handle
exposes its run ID, executor, and task, and
delegates same-process `stop()`, `steer()`, and `cancel_control()` operations.
Its await path shields the owner task so
canceling a waiting HTTP request or TUI action does not cancel the durable run.


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

Rerun acceptance atomically inserts the new root and its index-zero `rerun`
control, then marks the source root tree as ejected by that control. The source
records remain available for audit but disappear from effective thread and run
projection. The source must be the latest visible root in its thread.
It must also be terminal, so rerun cannot detach a still-owned execution.

Retry acceptance atomically appends a finished `retry` control to the existing
root, resolves and records its anchor, ejects the invalid structural step
suffix, fails stale pending controls, and reopens the root as pending. New
steps use fresh physical indexes; ejected steps are never overwritten. A flow
retry restores typed locals from the finished top-level prefix and begins at
the first invalid statement. When a nested failed step is selected, its
unfinished containing step is also invalidated because only a finished
container is a reusable commit. Agic retry invalidates the agic step sequence
and starts a fresh model-tool cycle under the same root.


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
- `ceiling.py` resolves the public `CeilingSpec` into private `_AgentCeiling`
  and per-run `_RunCeiling` values;
- `prepare.py` resolves an agic's model, tools, caps, prompt, history, and
  adapter in one pass;
- `runs/agic.py` owns the fixed model-tool cycle for one agic;
- `runs/flow.py` advances through lowered flow statements and updates locals;
- `stmts/` implements lowered statement semantics and chooses a step type;
- `steps/` owns executable step boundaries and their `StepBegin`, part, and
  `StepEnd` events.

Preparation produces one private `_AgicFrame` consumed directly by the agic
run. Adapters never observe that frame; their boundary remains one
`ModelTarget` and one normalized `ModelCall` per model step.
There is no loop plugin or public run-context protocol, and there are no
separate effective-resource, invocation, model-call assembly, or tool-snapshot
layers.

`CeilingSpec` contains stable selector lists, not resolved resources. At
`start()`, the executor resolves the spec against the captured `AgentSetup` and
`AgentState` into `_AgentCeiling`, the absolute resource limit for a recursive
run tree. An invalid spec is rejected before the run is durably accepted.
Every flow invocation resets its `_RunCeiling` from
`_AgentCeiling`, whether or not that flow declares directives. Agics derive
their `_RunCeiling` from the nearest containing flow ceiling, or directly from
`_AgentCeiling` at the root. Agic directives affect only that agic. A nested
flow does not inherit its caller's flow correction; returning from it naturally
restores the caller's immutable flow ceiling.

Agic and flow bodies run natively on the owner event loop. Model adapters,
tool invocation, step emission, and run tracing are asynchronous. A wrapper
may move an explicitly synchronous Python tool callable to a worker thread,
but the agic loop itself never moves between threads.


## Run Limits

`RunLimits` has one compact shape:

```python
@dataclass(frozen=True, slots=True)
class RunLimits:
    agic_model_calls: int | None = 200
    agic_tool_calls: int | None = None
    tokens: int | None = None
    cost: Decimal | None = None
    time: int | None = None
```

`agic_model_calls` and `agic_tool_calls` reset for every agic invocation.
`tokens`, USD `cost`, and wall-clock `time` cover the complete recursive root
run tree. `None` disables a limit and zero prohibits use of the corresponding
resource.

Call limits are checked before invoking a model or tool. Token and cost totals
are charged from each completed model result. Every completed model step notes
its input and output token counts, captured USD-per-token prices, and computed
USD cost when available. The model step remains finished when its usage or cost
crosses a total, then the owning run fails. A token or cost limit requires model
usage, and a cost limit also requires captured input and output prices for every
selected model. Time expiry cancels an in-flight operation while recording
affected runs as failed rather than user-canceled.

The effective value is persisted on the root entry control and on every retry
control. Child runs inherit the same in-memory value and their start controls
do not duplicate it. Decimal cost is serialized as text so durable round trips
do not lose precision.

A retry restores global token and cost accounting from effective, non-ejected
finished model steps. Ejected attempts remain auditable but do not consume the
retried execution's effective totals. Per-agic call counters restart only for
agics that execute again.

The `_Execution` object emits `RunBegin` and `RunEnd` and dispatches each
accepted executable to the appropriate run body. Input coercion initializes the
primary local before the body starts, and output coercion validates its final
value before `RunEnd`. A top-level agic or flow has no containing step. When a
flow statement invokes another agic or flow, `steps/run.py` emits the containing
run-step events around the child run:

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

Any process may call `steer()`, `stop()`, or `cancel_control()` because these
operations only mutate shared SQLite truth. `start(spec)` is also process-safe,
but the process that calls it owns and executes that run; start is not a
cross-process dispatch queue.

Every inserted or changed control receives a global monotonic revision inside
the same SQLite write transaction. The owner process polls only revisions
newer than its cursor. An unchanged control table returns no rows. Changed
pending controls are merged into the matching `_ActiveRun` cache, while
`finished`, `canceled`, and `failed` controls are removed. Runtime checkpoints
read the cache rather than querying SQLite once per active run.

An `immediate` stop cancels the owner task. `next_step` and `next_call` controls
are consumed at runtime checkpoints. Flows check before statements and calls;
agics check before model and tool calls.

Local submissions update the cache immediately after their durable write.
Remote submissions and cancellations use the same revision feed, so SQLite
remains the cross-process source of truth without an additional wake channel.
Runtime atomically claims a pending steer or stop immediately before applying
it. A cancellation updates only an unclaimed pending control, making
application and cancellation linearizable without exposing an intermediate
public status.


## Event Ordering

Runtime emits only canonical `RunEvent` values. `RunExecutor` handles each one
in this order:

1. in one transaction, project the event into the `RunStore` and update
   referenced control statuses;
2. update the in-memory control cache;
3. maintain the local active-run index;
4. await the optional `RunTracer.on_event()`.

Tracer exceptions are logged and ignored. Persistence or control-transition
errors remain execution failures because they compromise durable truth.
An `asyncio.Lock` serializes the complete handler sequence across parallel
child runs, preserving one ordered tracer stream without requiring a
thread-safe tracer.


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

`RunExecutor` always constructs its private event projector. History is
required for later model calls, so persistence is not a pluggable executor
capability.

The private projector handles only run and step facts. It does not accept controls,
change control statuses, store live events, or translate events into API/CLI
protocols. It persists event payloads directly and owns no parallel local or
output projection state.


## Streaming

Model providers stream deltas whenever supported. The executor never disables
provider streaming based on caller type. A tracer can ignore `PartDelta` and
observe only step or run boundaries.

`ModelAdapter.invoke()` and `ModelAdapter.stream()` are asynchronous.
Streaming adapters await each `ModelStreamHandler` update before requesting
the next provider chunk, which makes ordering and backpressure explicit.

API SSE, TUI rendering, channel replies, and buffered responses are tracer or
transport implementations outside `toolang.execution`.
