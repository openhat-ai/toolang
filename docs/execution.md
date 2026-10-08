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
process exits. The execution store uses schema version 52: both
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

Each `StepRef` combines an owning Run with a Run-relative `StepPath`. SQLite
stores the canonical StepRef as `steps.id`, retains the Run and relative path as
private indexed columns, and protects both identities. One process owns
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
schemas. Local library consumers can use this client; execution owners such as
`AgentCore` continue to use `RunExecutor` directly.

`RemoteRunClient` implements the same boundary over an agent runtime's absolute
HTTP origin. It sends self-contained, materialized requests to
`POST /api/v1/runs/authored/stream`, consumes canonical `RunEvent` values from
the accepted run's SSE response, and uses the existing run detail, cancel, and
steer endpoints. The server owns setup/state snapshots, request validation,
authored-input resolution, and file includes; mutable session defaults and
fallback rules never cross the request boundary. After an interrupted stream,
the client makes up to three GET reconnection attempts using its committed
cursor (100 ms, 500 ms, and 1 s delays); it never repeats a POST. Structural
replacement clears unfinished Parts before presentation. Disconnecting detaches readers and
owned HTTP resources without canceling server runs or managing the server
process.

Terminal Chat ensures a compatible AgentServer is ready for any materialized
layout, starting a persistent host or guest runtime when needed. Concurrent
starters reuse the same runtime. Chat uses its API after endpoint health and
profile checks. Non-run HTTP operations remain in the Chat client:
runtime/model/runnable inspection, run-default
adoption, thread creation, and result reads. If cursor recovery fails after acceptance, Chat falls back to durable run detail
without retrying execution. Closing Chat leaves the runtime running.
Retry/rerun and steer/cancel/fork/rewind likewise ensure a hosted runtime and
perform mutations through its API. A retry/rerun observation failure reports the
accepted run ID without canceling execution; an explicit CLI interrupt requests
cancellation. Script lifecycle remains separate: an
inactive host runs embedded, and a script-created guest stops on exit.

The process-local executor remains the execution engine:

`RunExecutor` is the public run entry point:

```text
start()                                             -> None
run(RunSpec, run_id?, request_id?, tracer?)         -> LocalRunHandle
cancel(run_id, timing, request_id?, reason?)         -> ControlRecord
steer(run_id, message, timing, request_id?)          -> ControlRecord
cancel_control(run_id, index)                        -> ControlRecord
stop()                                               -> None
```

`start()` is an idempotent lifecycle hook. `run()` accepts durable truth,
creates the owner task, and immediately
returns an awaitable `LocalRunHandle`. Awaiting the handle returns the terminal
`RunRecord`; canceling one waiter does not cancel execution. The handle also
provides same-process `cancel()`, `steer()`, and
`cancel_control()` conveniences.
Cross-process callers address the run by ID through their local `RunExecutor`.

`steer()` and `cancel()` only accept durable controls; `cancel_control()` changes
one pending steer or cancel to `revoked`. These controls do not require the
target run to be owned by the submitting process. Run execution remains local:
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
and output edge. For accepted spawn Steps, the projector preserves successful
admission through canceled delivery and emits the persisted status/output. It
does not reconstruct runtime locals. `RunTracer.on_event()` is asynchronous. The executor
serializes tracer calls and awaits each one on the owner event loop, so tracers
never need to infer which worker thread emitted an event.


Spawned roots have their own thread and lifecycle. The host may configure
`RunExecutor.root_tracer` and `thread_listener`; the API uses these to route the
existing thread/run events to the new IDs. Foreground tracers keep observing only
the source run tree. Dispatch failure can emit a root RunEnd before any RunBegin.
Script and Chat progress use the ordinary StepEnd output/summary to show the new
run/thread identity without adopting background progress.

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

Preparation control kinds are `run` and `retry`; runtime control kinds are
`exec`, `chdir`, `recall`, `compact`, `steer`, and `cancel`. Rerun creates a new
Run with a `run` control. Control timing is:

```text
immediate | next_step | next_call
```

Statuses are:

```text
pending   external steer/cancel accepted and not applied
applied   applied by the runtime
wontapply no longer applicable because the run ended or the checkpoint vanished
revoked   explicitly withdrawn before application
```

`applied` means the control was applied; it does not mean the run succeeded.
Runtime-created controls are applied when their effects commit. In particular,
an applied run control confirms admission even while the Run is still pending.
A cancel control is therefore `applied` when it cancels a run. An unapplied steer
left behind by a terminal run is `wontapply`.

Every Run entry control stores its concrete runnable and model bindings,
limits, resources, and a flat `CallInput[Value | TypedRef]`. Optional
`authored_input` records the corresponding `CallInput[str]` source snapshot.
Steer stores a primary `Part[]` value under `_`; cancel stores optional primary
Text under `_`. Exec stores input references keyed by parameter name. Retry
inherits input from the entry control and records its effective settings.

Every Run entry stores its State revision. Root entries also store the accepted
sandbox; child entries omit the redundant sandbox. A durable run is a root exactly when
`parent is None`; callers derive its root by following parent-run ownership.

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
control. Before applying a steer or cancel, runtime atomically claims it in
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
active runs before retrying; `ThreadManager` never writes run controls. Rewind
records a closed range in a Thread control without modifying Runs or Steps.
Logical history applies that Thread's controls; physical records remain readable.
Forks capture the source view at its control head, so later source rewinds do not
change the inherited prefix. Forks and rewinds are serialized across
processes with an agent-local file lock. Anchor resolution, terminal checks,
the rewind idle check, and control insertion occur in one SQLite write
transaction. The store keeps expected-head comparison as an internal defensive
check; it is not part of the public manager API.


## State Capture

`RunSpec` carries one explicit immutable `AgentState`,
`toolang.setup.AgentSetup`, effective `RunBindings` and `RunLimits`, and zero
or more `AgentCeiling` restrictions. `AgentSetup` supplies the immutable
`AgentLayout`, installed runtime implementations, effective model/tool
collections, and captured policy defaults. `AgentState` includes the captured
workspace grants and effective per-module cap collections. Setup and State apply
their owned `[allow]` fields before publication; session and run policy only
narrow those bases.
Execution uses that layout directly for the agent identity, home, and runtime
rooms. `RunSpec.input` is a flat `CallInput[Value]`; `_`, when present, contains
`Part[]` or the explicitly declared input type, and other keys hold arguments.
Output coercion validates the final run value against the runnable's declared
output type. Setup and State remain
complete snapshots; the executor computes concrete `AgentResources` instead of
receiving filtered copies. A root starts from the State supplied in `RunSpec`.
Each physical Step retains its executing Run's State reference. A newly accepted
named child independently captures the latest published State and validates it
against the caller's contract. Inline bodies retain their containing plan.
Publication never rebinds accepted Runs. Invalid source is not published.
