# RunExecutor Design

`RunExecutor` executes one immutable `AgentSetup`, `AgentState`, and
`RunRequest` combination while keeping durable truth ahead of external
observation.


## Public Shape

```python
class RunExecutor:
    async def start(
        self,
        setup: AgentSetup,
        state: AgentState,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunRecord: ...

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: RunControlTiming,
        request_id: str | None = None,
    ) -> RunControlRecord: ...

    def stop(
        self,
        *,
        run_id: str,
        timing: RunControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord: ...
```

`RunRequest` contains only values selected for one invocation:

```python
@dataclass(frozen=True, slots=True)
class RunRequest:
    origin: str
    input: Message = field(default_factory=lambda: Message.user(""))
    run_id: str | None = None
    thread_id: str | None = None
    executable_kind: Literal["agic", "flow"] = "agic"
    executable_name: str | None = None
    model_selector: str | None = None
    request_id: str | None = None
    context: dict[str, object] = field(default_factory=dict)
```

The request does not duplicate installed tools, model providers, or model
adapters from `AgentSetup`, nor the program and effective caps from
`AgentState`. Agic directives select from those captured resources. The
optional singular model selector is the caller's per-run model choice;
available and default model selectors remain process-level executor inputs.

There is no `run()`, `execute()`, `spawn()`, or `start()` background-task
variant. The caller decides whether to await `start()` directly or place it in
an application-owned task.


## Acceptance

Binding resolves explicit runtime inputs and allocates a process-safe run ID
and thread ID when absent. A missing thread is synchronously created before run
acceptance.

`RunStore.accept_start()` uses `BEGIN IMMEDIATE` to:

1. validate the thread;
2. reject a conflicting run ID;
3. insert the pending `RunRecord`;
4. insert start `RunControlRecord(index=0)`;
5. commit ownership to exactly one process.

A retry with the same run and request ID returns existing truth. The retrying
process never executes the run.


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

- `common.py` binds `RunRequest` to immutable state, setup, and durable IDs;
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
`RunEnd(failed)`. Cancellation unwinds steps and child runs before the root
`RunEnd(canceled)`.

If cancellation came from a stop control, `RunEnd.input` references it. The
runtime marks that control `finished` and marks all other pending controls
`failed` because they can no longer reach an applicable checkpoint.


## Persistence

`RunExecutor` always constructs `PersistSink(store)`. History is required for
later model calls, so persistence is not a pluggable executor capability.

`PersistSink` projects only run and step facts. It does not accept controls,
change control statuses, store live events, or translate events into API/CLI
protocols.


## Streaming

Model providers stream deltas whenever supported. The executor never disables
provider streaming based on caller type. A tracer can ignore `PartDelta` and
observe only step or run boundaries.

API SSE, TUI rendering, channel replies, and buffered responses are tracer or
transport implementations outside `toolang.execution`.
