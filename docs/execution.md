# Execution Model

This document defines the ownership and lifecycle boundaries of
`toolang.execution`. Detailed record shapes live in
[run-step-records.md](./run-step-records.md), and runtime behavior lives in
[executor.md](./executor.md).


## Responsibilities

`toolang.execution` owns:

- accepting, controlling, and executing agic and flow runs;
- mandatory durable run and step history in `runs.db`;
- durable run controls that can be submitted by another process;
- thread creation, fork, and rewind semantics;
- caller-facing projections of durable execution truth.

It does not own API streaming protocols, CLI rendering, agent events, event
hubs, or exact historical event replay.

`RunHistory` is the read-only caller-facing entry point over durable execution
truth. In addition to thread and run listing/detail, `get_run_result()` resolves
one output edge and `latest_thread_result()` selects the newest succeeded root
run with a nonempty output. It remains independent from `RunExecutor` and
`ThreadManager`, so callers can inspect `runs.db` while no agent process is
running. `RunStore` performs raw and batched record reads; execution schemas
perform the final pure record-to-schema conversion.


## Durable Truth

`runs.db` contains:

- `ThreadRecord`, `RunRecord`, and `StepRecord`;
- one `ControlRecord` model for run- and thread-scoped controls;
- complete typed step outputs;
- content-addressed model instructions, messages, and toolsets.

Run events are transient facts emitted during execution. They are never stored
as `EventRecord` rows. A reconnecting caller reconstructs current state from
records and observes only new live events.

Persistence makes completed history available after process restart and for
later model calls. Toolang does not resume an unfinished run after its owner
process exits. Schema version 32 is an incompatible Pointer boundary: both
read-only and writable opens reject every other version unchanged. This build
does not migrate older stores.


## IDs And Indexes

Run and interactive-thread IDs are issued through one process-owned `IdIssuer`
from `toolang.common.ids`. The process composition root gives the same issuer
and `RunStore` to `RunExecutor` and `ThreadManager`. Separate processes use
separate objects over the same files. Allocation is serialized with an
inter-process file lock. Unused IDs are allowed; duplicates are not.

Control indexes are local to their Run or Thread target. They are allocated and
inserted under `BEGIN IMMEDIATE` in `runs.db`; index reservation is never a
separate operation. Non-null request ids are globally unique in the unified
Control table.

Runs within one thread use their durable SQLite acceptance order for history,
fork, and rewind boundaries. Wall-clock timestamps remain display metadata and
are not used to decide which runs follow an anchor.

Each `StepPath` includes its owning run. SQLite stores the run and local index
path separately and protects the `(run, path)` primary key. One process owns
execution of a run tree.


## Run Execution

`RunClient` is the transport-neutral caller boundary used by Terminal Chat. It
accepts self-contained `RunRequest` values, exposes asynchronous connect, run,
cancel, steer, and disconnect operations, and returns a transport-neutral
`RunHandle` plus
caller-facing `RunDetail` and `ControlInfo` values. The boundary deliberately
excludes stores, setup and state snapshots, local tasks, and durable records so
local and remote execution preserve the same interaction shape. Clients are
disconnected after construction and reject operations until `connect()`.

`LocalRunClient` implements that boundary over a `RunExecutor`. It reads the
current setup and state once for each run, validates the request's concrete
runnable, model parameters, materialized policy, and authored input, and
converts terminal and control records through the existing caller-facing
schemas. Terminal Chat still owns its session defaults, watchers, store, thread
manager, result inspection, and event-loop thread. Other local execution owners
continue to use `RunExecutor` directly.

`RemoteRunClient` implements the same boundary over an agent runtime's absolute
HTTP origin. It sends self-contained, materialized requests to
`POST /api/v1/runs/authored/stream`, consumes canonical `RunEvent` values from
the accepted run's SSE response, and uses the existing run detail, cancel, and
steer endpoints. The server owns setup/state snapshots, request validation,
authored-input resolution, and file includes; mutable session defaults and
fallback rules never cross the request boundary. The client never
retries a run or reconnects an incomplete stream because the live event
protocol has no replay cursor. Disconnecting the client detaches its readers and
owned HTTP resources without canceling server runs or managing the server
process.

Terminal Chat first resolves a CLI-owned `ExecutionRuntime`. It attaches to a
compatible running AgentServer for any materialized layout, uses embedded host
execution when no server is active and `host` is selected, or starts a
command-owned temporary AgentServer for a non-host sandbox. Remote execution is
used only after endpoint health and profile checks. Non-run HTTP operations
remain in the Chat client: runtime/model/runnable inspection, run-default
adoption, thread creation, and result reads. A stream failure after acceptance is
recovered from durable run detail without retrying the run or synthesizing
missing `RunEvent` values. Closing Chat never stops an attached server and
stops and releases only a temporary server created by that command.

The process-local executor remains the execution engine:

`RunExecutor` is the public run entry point:

```text
start()                                             -> None
run(RunSpec, run_id?, request_id?, tracer?)         -> LocalRunHandle
cancel(run_id, timing, request_id?, reason?)         -> ControlRecord
steer(run_id, message, timing, request_id?)          -> ControlRecord
reload(run_id, state, request_id?)                   -> ControlRecord
cancel_control(run_id, index)                        -> ControlRecord
stop()                                               -> None
```

`start()` is an idempotent lifecycle hook. `run()` accepts durable truth,
creates the owner task, and immediately
returns an awaitable `LocalRunHandle`. Awaiting the handle returns the terminal
`RunRecord`; canceling one waiter does not cancel execution. The handle also
provides same-process `cancel()`, `steer()`, `reload()`, and
`cancel_control()` conveniences.
Cross-process callers address the run by ID through their local `RunExecutor`.

`steer()` and `cancel()` only accept durable controls; `cancel_control()` changes
one pending reload, steer, or cancel to `revoked`. Steer, cancel, and control
revocation do not need the target run to be owned by the submitting process;
reload does. Run execution remains local:
the process that calls `run(spec)` accepts and executes that run. `stop()` is
terminal and cancels the run tasks owned by that executor instance. The process
owner closes the shared `RunStore` after the executor stops.

Callers resolve the captured `AgentSetup` defaults and any session or run
policy into `RunSpec.limits` before `run()`. Per-agic model and tool call
limits reset on each agic invocation, while token, cost, and time limits are
shared by all recursive runs. Effective limits are stored on the root run
control and on each retry control.

`run()` requires an existing thread. Thread creation belongs to
`ThreadManager` or to the package that owns a deterministic external thread id.

The run operation atomically inserts the pending run and its index-zero run
control. Run IDs are globally unique within `RunStore`; duplicates are
rejected. A non-null request ID is unique within its control table and is never
treated as a replay key. Clients either generate a globally unique request ID
across run and thread controls or pass `None`.


## Mandatory Persistence And Tracing

`RunExecutor` constructs one private event projector from its `RunStore`.
Persistence cannot be replaced or disabled by callers.

For every `RunEvent`, ordering is:

```text
runtime produces event
  -> one transaction projects RunRecord or StepRecord
     and updates referenced ControlRecord statuses
  -> optional RunTracer observes the event
```

The private projector never creates or updates run controls. Tracer failures
are logged and isolated from execution. One tracer observes the complete run
tree started by its `run()` call, including child runs, steps, parts, and
terminal events. Each event already contains its complete durable references
and output edge; the private projector does not reconstruct runtime locals or
infer alternate output. `RunTracer.on_event()` is asynchronous. The executor
serializes tracer calls and awaits each one on the owner event loop, so tracers
never need to infer which worker thread emitted an event.


## Run Events

The canonical run trace is intentionally small:

```text
RunBegin
StepBegin
PartBegin
PartDelta
PartEnd
StepEnd
RunEnd
```

There are no waiting, starting, steering, or stopping events. Control
acceptance is durable record truth. Control application is represented by data
edges:

- `RunBegin.control` references the run control;
- `StepBegin.input` references every run control or prior step output consumed by
  the step;
- `RunEnd.control` references the cancel control that canceled the run.

Providers stream deltas when supported. A tracer may ignore `PartDelta` and
observe only higher-level events.


## Run Controls

Preparation control kinds are `run`, `rerun`, and `retry`; runtime control
kinds are `reload`, `steer`, and `cancel`. Control timing is:

```text
immediate | next_step | next_call
```

Statuses are:

```text
pending   newly accepted and not applied
applied   applied by the runtime
wontapply no longer applicable because the run ended or the checkpoint vanished
revoked   explicitly withdrawn before application
```

`applied` means the control was applied; it does not mean the run succeeded.
A cancel control is therefore `applied` when it cancels a run. An unapplied steer
left behind by a terminal run is `wontapply`.

Reload always uses `immediate` timing. It is process-local: the accepting
executor must own the active run tree and retain the concrete durable
`AgentState`. A child run ID is normalized to its root. There is no HTTP, CLI,
Chat, scheduler, or model-facing reload endpoint.

Every newly accepted preparation control stores top-level `RunBindings`,
`RunLimits`, `RunnableInput`, and final `AgentResources` snapshots. Steer stores a
`Message`; cancel stores an optional reason. Root preparation controls also store
the canonical sandbox in which the executor accepted them. Nested runs omit
that redundant value, while its absence on a legacy root means unknown rather
than `host`. Root `run` and `rerun` controls store the State revision; new
child `run` and `retry` controls omit it. A `reload` control stores only the new
revision. These values are not duplicated in the control context. A durable
run is a root exactly when `parent is None`; callers that need a root run ID
derive it by following parent-run ownership.

Every run-control insert or status change receives a monotonically increasing
SQLite revision. Each executor remembers the latest revision it observed and
loads only rows changed after that cursor. An unchanged table returns no rows
and causes no active-run processing. Changed pending controls are merged into
the matching locally owned run tree; changed terminal controls are removed.
Runtime checkpoints read this in-memory view instead of repeatedly querying
all pending controls for every active run.

Local submissions update the same cache immediately after their durable write.
Remote submissions and cancellations arrive through revision polling. An
immediate cancel cancels the owning task after the owner observes the durable
control. Before applying a reload, steer, or cancel, runtime atomically claims it in
SQLite. Cancellation is allowed only while a control remains unclaimed, so a
cross-process claim/cancel race has exactly one winner without adding another
public control status.


## Threads

`ThreadManager` synchronously performs `create()`, `fork()`, and `rewind()`.
Create and fork return the newly allocated thread id. Rewind changes the
specified thread in place and returns nothing. Fork and rewind accept an
optional run id; omission selects the last visible top-level run. Recursive
child runs are never thread anchors. An empty thread has no implicit anchor and
cannot be forked or rewound. Every selected anchor must be terminal.

Every successful mutation has a durable `ControlRecord` and produces one
success event:

```text
ThreadCreated
ThreadForked
ThreadRewound
```

Failures are returned or raised by the synchronous operation and do not
produce failure events.

A fork stores its source thread and anchor run but does not copy run, step, or
run-control rows. Its inherited history includes the anchor. It may select an
earlier terminal anchor even when the source thread has a later active run. A
rewind discards its anchor and the visible suffix after it, and is rejected
while any visible top-level run is pending or running. The caller must cancel
active runs before retrying; `ThreadManager` never writes run controls. Runs
owned by the rewound thread are marked with `ejected`; an inherited
source run is never modified. Forks and rewinds are serialized across
processes with an agent-local file lock. Anchor resolution, terminal checks,
the rewind idle check, and control insertion occur in one SQLite write
transaction. The store keeps expected-head comparison as an internal defensive
check; it is not part of the public manager API.


## State Capture

`RunSpec` carries one explicit immutable `AgentState`,
`toolang.setup.AgentSetup`, effective `RunBindings` and `RunLimits`, and zero
or more `AgentCeiling` restrictions. `AgentSetup` supplies the immutable
`AgentLayout`, root-scoped installed runtime implementations, and captured
policy defaults. `SetupWatcher` resolves
root and agent-home `[allow]`, `[default]`, and `[limit]` config on every
refresh, then applies frozen field-level environment/CLI overrides before
publishing the setup snapshot.
Execution uses that layout directly for the agent identity, home, and runtime
rooms. `RunSpec.input.primary` is one protocol-level `Percept`;
after runnable resolution, input coercion exposes that value as `Part[]` or
another explicitly declared primary type. Output coercion validates the final
run value against the runnable's declared output type. Setup and State remain
complete snapshots; the executor computes concrete `AgentResources` instead of
receiving filtered copies. A root starts from the State supplied in `RunSpec`.
An explicit reload changes the State/ref pair at a serialized execution
boundary; each physical step captures that pair, and a child run inherits its
calling step's pair. Independent roots remain isolated. Source changes have no
effect until a caller starts a new root or explicitly reloads an owned active
tree. Invalid later setup config does not replace the last valid snapshot.
