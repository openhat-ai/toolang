# RunExecutor Design

`RunExecutor` executes one immutable `RunSpec` while keeping durable truth
ahead of external observation.


## Public Shape

```python
class RunExecutor:
    def __init__(self, store: RunStore, ids: IdIssuer) -> None: ...

    def start(self) -> None: ...

    def run(
        self,
        spec: RunSpec,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle: ...

    def rerun(
        self,
        source: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle: ...

    def retry(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        anchor: StepRef | str | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        limits: RunLimits | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle: ...

    def cancel(
        self,
        *,
        run_id: str,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlRecord: ...

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: ControlTiming,
        request_id: str | None = None,
    ) -> ControlRecord: ...

    def reload(
        self,
        *,
        run_id: str,
        state: AgentState,
        request_id: str | None = None,
    ) -> ControlRecord: ...

    def cancel_control(
        self,
        *,
        run_id: str,
        index: int,
    ) -> ControlRecord: ...

    async def stop(self) -> None: ...
```

Construction makes the executor immediately available. `start()` is an
idempotent lifecycle hook, while `stop()` is terminal: it cancels all run tasks
owned by the executor and stops its control monitor. The process composition
root owns and closes the shared `RunStore`; `RunExecutor` and `ThreadManager`
receive the same store and `IdIssuer` instances.

Process entry points also supply `setup`, `state`, and `load_state` snapshot
callbacks to the constructor. Retry retains the recorded State but checks its
workspace names and paths against the current State before changing records.
Removed or remapped grants reject retry; callers may use rerun with current
State instead. Retrying a Run with workspace grants requires the `state`
callback even when setup and recorded State are passed explicitly.

Control polling currently uses an internal default and can move into executor
options when runtime tuning becomes public.

`RunSpec` contains the immutable inputs required to start one invocation:

```python
@dataclass(frozen=True, slots=True)
class RunSpec:
    setup: AgentSetup
    state: AgentState
    thread: str
    bindings: RunBindings
    limits: RunLimits
    model_request: ModelRequest | None = None
    ceilings: tuple[AgentCeiling, ...] = ()
    input: RunnableInput = field(default_factory=CallInput)
```

`bindings.runnable` is required and resolves to exactly one public agic or flow
in the captured state's runnable catalog. Resolution also fixes the owner
program module. `bindings.model` is the effective singular model choice and
`model_request` retains its typed call parameters for validation, persistence,
accounting, retry, and rerun.
The spec does not carry an origin, run identity, request identity, or arbitrary
transport context. Each item in `ceilings` is one independently applied
collection-query restriction over the effective resources already published by
Setup and State. Retaining separate session and run restrictions preserves
intersection semantics when queries use OR matching. `input["_"]`, when
present, holds the typed primary input. Other keys hold values for the
runnable's declared `params`.
`RunnableInput` is the `CallInput[Value]` alias; absent `_` means no input.
The executor validates both before
accepting the run and constructs the user message internally. It
stores the accepted typed values in the run control and durable history.
Language-owned coercion supplies `_` with the runnable's declared input type.

`AgentSetup.limits` is the captured default for a new run. Policy resolution
produces the effective `RunSpec.limits` before `run()`. Config, CLI, chat, and
HTTP parsing remain caller concerns and are not part of the executor contract.

There is no `execute()` or `spawn()` variant. `run()`, `rerun()`, and `retry()`
create an owner task and return an awaitable `LocalRunHandle`.
`rerun()` loads the source invocation from durable truth and starts a new root
against the supplied current setup and state. `retry()` keeps the root ID,
reopens a terminal run, and resumes after its effective committed prefix. An
omitted retry anchor uses the latest visible failed, canceled, or running Step.
For a failed or canceled Run with no incomplete Step, it uses the latest visible
Step. For a succeeded Run, it prefers the latest non-value Step and falls back
to the latest value Step. The handle exposes its run ID, executor, and task, and
delegates same-process `cancel()`, `steer()`, `reload()`, and
`cancel_control()` operations.
Its await path shields the owner task so
canceling a waiting HTTP request or TUI action does not cancel the durable run.


## Acceptance

Binding validates explicit runtime inputs and asks `IdIssuer` for a run ID when
`run()` does not receive one. `RunSpec.thread` must identify an existing
thread. The executor never allocates or implicitly creates threads.

Supplying a run ID is intentionally limited to one-shot script invocation so
logging can be configured at a path containing that ID before execution.
Interactive, job, file, channel, and API callers let the executor allocate the
ID. The supplied or allocated ID must be globally unique in `RunStore`.

`RunStore.accept_run()` uses `BEGIN IMMEDIATE` to:

1. validate the thread;
2. reject a conflicting run ID;
3. insert the pending `RunRecord`;
4. insert `run` `ControlRecord(index=0)` with effective `bindings`,
   `limits`, `input`, final `resources`, and canonical root sandbox snapshots;
5. commit the accepted run before its owner task is scheduled.

Duplicate run IDs and duplicate non-null request IDs are rejected. Request IDs
are globally unique in the unified Control table; `None` disables request
identity without weakening the run or Control primary keys.

Rerun acceptance atomically inserts the new root and its index-zero `rerun`
control with the current canonical sandbox. It does not modify the terminal
source root or its history membership.

Retry acceptance atomically appends an applied `retry` control to the existing
root, resolves and records its anchor, physically deletes the invalid structural
step suffix and its child Runs, fails stale pending controls, and reopens the
root as pending. New steps reuse the trimmed indexes. Before mutation, retry
rejects any root tree captured by a durable fork prefix, as well as applied
reload and execute-control history
because it cannot replay either prior execution timeline. It also requires the
source preparation to have sandbox
provenance and requires it to equal the current canonical sandbox. Accepted
retry controls repeat that value. A flow retry restores typed locals from the
succeeded top-level prefix and begins at the first invalid statement. When a nested failed
step is selected, its unfinished containing step is also invalidated because
only a succeeded container is a reusable commit. Agic retry invalidates the
agic step sequence and starts a fresh model-tool cycle under the same root.


## Active Ownership

The local active registry maps every active run ID in one run tree to the
top-level owner task and tracer. It is a task-ownership index, not the durable
source of admission truth.

Child runs receive their own process-safe IDs, run records, and index-zero
run controls before `RunBegin`. They execute in the top-level owner task and
inherit its setup and tracer. Each child binds the immutable State object and
control reference captured by its calling step.


## Execution Structure

`RunExecutor` is the process-level singleton. Each owned root run creates one
private `_Execution` that carries the current `(AgentState, ControlRef)` pair
for its recursive run tree.
The implementation is divided by semantic level:

- `executor.py` binds `RunSpec` to immutable execution state and durable IDs;
- `resources.py` builds and filters `AgentResources`, then applies runnable
  directives to the selected resource base;
- `frame.py` builds an agic's execution frame from bound resources and runtime
  facts; it does not materialize Agent State;
- `runs/agic.py` owns the fixed model-tool cycle and output-repair requests for
  one agic;
- `runs/flow.py` advances through lowered flow statements and updates locals;
- `stmts/` implements lowered statement semantics and chooses a step type;
- `steps/` owns execution step boundaries and their `StepBegin`, part, and
  `StepEnd` events.

`execution/assembly/` groups model-call content assembly:

- `prompting.py` builds complete, provider-neutral `ModelCall` inputs for
  adapters. `prepare_prompt()` renders instructions, context, and initial
  messages for the frame cache; `build_model_call()` accepts finished messages
  and adds structured tool definitions, output schema, continuation, and budget.
  It also owns the cached `PreparedPrompt` value;
- `messages.py` constructs and orders messages, selects and reconstructs history,
  frames controls, and reconciles supplied resource declarations without I/O;
- `tool_replies.py` constructs individual control receipts and intercepted-call
  replies, shared by live delivery and history reconstruction;
- `utils.py` handles text escaping, Part normalization, message joining, and
  delta generation/rendering without selecting history or loading prompts;
- `prompts/` holds static text; `prompts/defaults/` contains the default
  instruct, context, and compact program.

Tool-use conventions belong to the static `prompts/protocol.md`. Protocol stays
first and unchanged across tool selection, `instruct: none`, and output repair.
Tool definitions remain structured `ToolDefinition` values; adapters choose their
provider-specific representation. The model step decides whether tools are enabled.

Authored messages are resolved before reusable prompt preparation so the frame
can record prompt invocations before rendering instructions and context.
Control-message framing and its short steer/cancel descriptions live in
`messages.py`; replay uses recorded templates, not current wording. Output-repair wording lives directly
beside its policy in `executor/runs/agic.py`.

Assembly consumes prepared data and records; it does not execute tools or read
the store. `executor/message_buffer.py` holds the live message sequence and
pending delta; `execution/records.py` owns delta serialization.

Instructions contain `toolang:protocol`, optional `toolang:instruct`, resident
`toolang:psyche` bodies, and `toolang:skill-trigger`/`toolang:service-trigger`
descriptions. Triggers advertise availability, not loaded guidance.
Using a skill or service requires its current visible `skill-guidance` or
`service-guidance` user message; `_toolang__pick` loads it on demand.
Revisions derive from immutable definition fingerprints and metadata, without
reading skill/service bodies during assembly.

Resource declarations use replacement by kind/ref and self-closing
`removed="true"` withdrawals. Capability changes retract stale guidance.
The executor reconciles adopted State with resident, historical, and pending
declarations at model-call boundaries; watcher changes alone do not wake runs.
New control templates escape literal text while preserving nontext Parts.
Optional persisted recall references track bodyless declarations; old deltas
retain their original framing and body-reference visibility semantics.

Every model call sees the available workspaces, including its first call:
`<toolang:workspace ref="project"/>`. Names are the refs; host roots and binding
revisions stay internal. Assembly does not scan rules. Preflight blocks a
path-aware operation until applicable rules are current and model-visible.
On a remap, honor withdraws old scoped rules and presents the new binding before
new rules; loading failure never permits the original operation. Compaction and
`recall = none` re-present bindings when the previous declarations are no longer visible.

Default instruct retains the agent name. Default context contains only date,
timezone, model provider, and model name. Authored instruct/context selection
remains unchanged; no default agent-home path, model-family label, or run-info is emitted.

`execution/inspection/` groups durable history queries, inspection types,
Run/Thread views, and execution-tree projections. Its package facade exposes
only lightweight types and helpers, so basic inspection does not load tree
projection. Persistence and model-call reconstruction remain in `RunStore`.

Built-in tool modules match their registered toolset names:
`execution/tools/_toolang.py` and `execution/tools/me/`.

`build_agic_frame()` produces one private `_AgicFrame` consumed directly by the agic
run. Adapters never observe that frame; their boundary remains one
`ModelTarget` and one normalized `ModelCall` per model step.
Assembly helpers add no separate execution state or model-call lifecycle.
There is no loop plugin, public run-context protocol, or separate
effective-resource, invocation, or tool-snapshot layer.

The frame holds one selected tool mapping and effective Agic routes. Every
ordinary tool-capable Agic call receives `_toolang__run`,
`_toolang__execute`, `_toolang__reload`, and `_toolang__pick`. `hands` and `handoffs` authorize run and
execute targets; they do not select definitions. All tools use plugin registration
and the same Tool Step lifecycle. Bounded `toolang:runnable-info` declarations
describe authorized targets only when `hands` or `handoffs` is present,
preserving run/execute distinctions. With neither directive, no runnable-info
is emitted. The limit is 64 entries and 32,768 UTF-8 bytes, including escaped
framing. Removed authorization retracts previously presented routes.
Runtime calls in one model batch
use that Model Call's captured routes, even if reload and ordinary tools adopt
new State between calls. The next Model Call captures the new routes.

`AgentSetup.models` and `AgentSetup.tools` are already filtered immutable
collections. `AgentState.caps_for(module)` returns precomputed filtered caps.
At `run()`, the executor intersects every
`RunSpec.ceilings` restriction and creates tree-level `AgentResources` using
stable model entry keys. A ceiling cannot expand the published base. Invalid
queries are rejected before the run is durably accepted.
Every flow invocation starts from the tree-level agent resources and applies
its own directives, whether or not the flow declares any. Agics start from the
nearest containing flow resources, or directly from the agent resources at the
root. Agic directives affect only that agic. A nested flow does not inherit its
caller's flow restriction; returning from it naturally restores the caller's
immutable flow resources.

Input structs, prompts, static child calls, and `here` caps resolve against the
bound owner module. Static child calls stay within that module; private helpers
cannot be selected as top-level public runnables.

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
USD cost when available. The model step remains succeeded when its usage or cost
crosses a total, then the owning run fails. A token or cost limit requires model
usage, and a cost limit also requires captured input and output prices for every
selected model. Time expiry cancels an in-flight operation while recording
affected runs as failed rather than user-canceled.

The effective value is persisted on every preparation control. Child runs
inherit the same in-memory value and their run controls repeat it so each
accepted preparation is self-contained. Decimal cost is serialized as text so
durable round trips do not lose precision.

`AgentSetup.limits` is rebuilt from dynamic root and agent `[limit]` config plus
frozen process overrides. A request may overlay individual fields for one root
run without changing the setup snapshot.

A retry restores global token and cost accounting from retained succeeded model
steps. Deleted attempts do not consume the retried execution's effective totals.
Per-agic call counters restart only for agics that execute again.

The `_Execution` object emits `RunBegin` and `RunEnd` and dispatches each
accepted runnable to the appropriate run body. Input coercion initializes the
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

A successful `_toolang__execute` records one applied execute control during its
Tool Step, then finishes that Step before transferring to the target. It creates
no child Run, extra transition Step, or additional `RunBegin`:

```text
RunBegin(entry)
  caller Model Step
  execute Tool Step → applied execute control
  target Steps
RunEnd(final target result)
```

The control's `triggered_by` points to the Tool Step. Its payload records the
Tool Step's captured State, the qualified runnable, and raw `Json` input pointers
into the originating Model ToolCall. Input, resources, authorization, and active
runnable lineage are validated before commit. Validation failure creates no
control and returns a correlated tool error. Success returns a control receipt;
the target starts with fresh continuation and local call counters, while prior
Steps and message deltas remain in Run history.
The entry runnable's output type remains the final Run contract. Repeated
identities in the current or an active ancestor lineage are rejected.

Progress starts the execute marker at Tool Step begin, confirms the transfer from
its result, and attaches a handoff divider to the target's first Step. A receipt
still confirms commit if result delivery was canceled; cancellation does not
undo the control. No separate control event is needed for presentation.


## Control Observation

Any process may call `steer()`, `cancel()`, or `cancel_control()` because these
operations only mutate shared SQLite truth. `run(spec)` is also process-safe,
but the process that calls it owns and executes that run; run is not a
cross-process dispatch queue.

`reload()` is intentionally different: only the owning executor accepts it.
It requires a concrete durable State from the same `AgentLayout`, normalizes a
child ID to the active root, retains the object, and writes a root-targeted
`reload` control with `immediate` timing.

Every inserted or changed control receives a global monotonic revision inside
the same SQLite write transaction. The owner process polls only revisions
newer than its cursor. An unchanged control table returns no rows. Changed
pending controls are merged into the matching `_ActiveRun` cache, while
`applied`, `wontapply`, and `revoked` controls are removed. Runtime checkpoints
read the cache rather than querying SQLite once per active run.

An `immediate` cancel cancels the owner task. `next_step` and `next_call` controls
are consumed at runtime checkpoints. Flows check before statements and calls;
agics check before model and tool calls.

Local submissions update the cache immediately after their durable write.
Remote submissions and cancellations use the same revision feed, so SQLite
remains the cross-process source of truth without an additional wake channel.
Runtime atomically claims a pending steer or cancel immediately before applying
it. A cancellation updates only an unclaimed pending control, making
application and cancellation linearizable without exposing an intermediate
public status.

Reload application and every physical `StepBegin` use the same root
`asyncio.Lock`. Applying a reload claims and marks its control `applied`, then
swaps the in-memory State/ref pair without awaiting. Beginning a step persists
the current ref and captures the matching immutable object before releasing
the lock. Parallel Flow branches serialize only this short boundary; their work
remains concurrent. A started step never changes State, and a child accepted by
that step uses the captured object and ref.


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

Successful execution emits one `RunEnd(succeeded)`. Runtime exceptions emit one
`RunEnd(failed)`. A directly failing step stores the message; enclosing steps
and runs reference that step. An exception outside a step stores its message
directly on the run without a synthetic step. Cancellation unwinds steps and
child runs before the root `RunEnd(canceled)`.

If cancellation came from a cancel control, `RunEnd.control` references it. The
runtime marks that control `applied` and marks all other pending controls
`wontapply` because they can no longer reach an applicable checkpoint.


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
