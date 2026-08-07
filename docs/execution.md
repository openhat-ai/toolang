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
truth. Its public surface is limited to `list_threads()`, `get_thread()`,
`list_runs()`, and `get_run()`. It remains independent from `RunExecutor` and
`ThreadManager`, so callers can inspect `runs.db` while no agent process is
running. `RunStore` performs raw and batched record reads; execution schemas
perform the final pure record-to-schema conversion.


## Durable Truth

`runs.db` contains:

- `ThreadRecord` and `ThreadControlRecord`;
- `RunRecord` and `RunControlRecord`;
- `StepRecord` and complete step outputs;
- content-addressed model instructions, messages, and toolsets.

Run events are transient facts emitted during execution. They are never stored
as `EventRecord` rows. A reconnecting caller reconstructs current state from
records and observes only new live events.

Persistence makes completed history available after process restart and for
later model calls. Toolang does not resume an unfinished run after its owner
process exits. Schema upgrades migrate supported versions in place and fail
without deleting records when an unsupported or conflicting schema is found.
A store never opens a newer schema by rebuilding it as an older one. Read-only
inspection opens SQLite in read-only mode, requires the current schema, and
never applies migrations; the agent runtime performs supported forward
migrations when it opens the store for execution.


## IDs And Indexes

Run and interactive-thread IDs are issued through one process-owned `IdIssuer`
from `toolang.common.ids`. The process composition root gives the same issuer
and `RunStore` to `RunExecutor` and `ThreadManager`. Separate processes use
separate objects over the same files. Allocation is serialized with an
inter-process file lock. Unused IDs are allowed; duplicates are not.

Run-control indexes are local to a run. Thread-control indexes are local to a
thread. Both are allocated and inserted under `BEGIN IMMEDIATE` in `runs.db`.
Index reservation is never a separate operation.

Runs within one thread use their durable SQLite acceptance order for history,
fork, and rewind boundaries. Wall-clock timestamps remain display metadata and
are not used to decide which runs follow an anchor.

Each `StepPath` includes its owning run. SQLite stores the run and local index
path separately and protects the `(run, path)` primary key. One process owns
execution of a run tree.


## Run Execution

`RunExecutor` is the public run entry point:

```text
start(RunSpec, limits?, run_id?, request_id?, tracer?) -> RunHandle
stop(run_id, timing, request_id?, reason?)     -> RunControlRecord
steer(run_id, message, timing, request_id?)    -> RunControlRecord
cancel_control(run_id, index)                  -> RunControlRecord
shutdown()                                     -> None
```

`start()` accepts durable truth, creates the owner task, and immediately
returns an awaitable `RunHandle`. Awaiting the handle returns the terminal
`RunRecord`; canceling one waiter does not cancel execution. The handle also
provides same-process `stop()`, `steer()`, and `cancel_control()` conveniences.
Cross-process callers address the run by ID through their local `RunExecutor`.

`steer()` and `stop()` only accept durable controls; `cancel_control()` changes
one pending steer or stop to `canceled`. None of these operations needs the
target run to be owned by the submitting process. Start remains local: the
process that calls `start(spec)` accepts and executes that run. The executor is
ready after construction and therefore has no separate `open()` method.
`shutdown()` is terminal and cancels the run tasks owned by that executor
instance. The process owner closes the shared `RunStore` after the executor
shuts down.

The captured `AgentSetup` supplies the default `RunLimits`; `start()` may
replace it for one root run tree. Per-agic model and tool call limits reset on
each agic invocation, while token, cost, and time limits are shared by all
recursive runs. Effective limits are stored on the root start control and on
each retry control.

`start()` requires an existing thread. Thread creation belongs to
`ThreadManager` or to the package that owns a deterministic external thread id.

The start operation atomically inserts the pending run and its index-zero start
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
     and updates referenced RunControlRecord statuses
  -> optional RunTracer observes the event
```

The private projector never creates or updates run controls. Tracer failures
are logged and isolated from execution. One tracer observes the complete run
tree started by its `start()` call, including child runs, steps, parts, and
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

- `RunBegin.input` references the start control;
- `StepBegin.input` references every run control or prior step output consumed by
  the step;
- `RunEnd.input` references the stop control that canceled the run.

Providers stream deltas when supported. A tracer may ignore `PartDelta` and
observe only higher-level events.


## Run Controls

Control kinds are `start`, `steer`, and `stop`. Control timing is:

```text
immediate | next_step | next_call
```

Statuses are:

```text
pending  newly accepted and not applied
finished applied by the runtime
canceled explicitly withdrawn before application
failed   no longer applicable because the run ended or the checkpoint vanished
```

`finished` means the control was applied; it does not mean the run succeeded.
A stop control that cancels a run is therefore `finished`. An unapplied steer
left behind by a terminal run is `failed`.

Every run-control insert or status change receives a monotonically increasing
SQLite revision. Each executor remembers the latest revision it observed and
loads only rows changed after that cursor. An unchanged table returns no rows
and causes no active-run processing. Changed pending controls are merged into
the matching locally owned run tree; changed terminal controls are removed.
Runtime checkpoints read this in-memory view instead of repeatedly querying
all pending controls for every active run.

Local submissions update the same cache immediately after their durable write.
Remote submissions and cancellations arrive through revision polling. An
immediate stop cancels the owning task after the owner observes the durable
control. Before applying a steer or stop, runtime atomically claims it in
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

Every successful mutation has a durable `ThreadControlRecord` and produces one
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
while any visible top-level run is pending or running. The caller must stop
active runs before retrying; `ThreadManager` never writes run controls. Runs
owned by the rewound thread are marked with `ejected`; an inherited
source run is never modified. Forks and rewinds are serialized across
processes with an agent-local file lock. Anchor resolution, terminal checks,
the rewind idle check, and control insertion occur in one SQLite write
transaction. The store keeps expected-head comparison as an internal defensive
check; it is not part of the public manager API.


## State Capture

`RunSpec` carries one explicit immutable `AgentState`,
`toolang.setup.AgentSetup`, and `CeilingSpec`. `AgentSetup` supplies the
immutable `AgentLayout`, root-scoped installed runtime implementations, and
captured default `RunLimits`. `SetupWatcher` resolves root and agent-home
`[run.limits]` config before constructing the setup snapshot; a server launch
may provide a complete explicit default instead.
Execution uses that layout directly for the agent identity, home, and runtime
rooms. Its primary input is one protocol-level `Percept`;
after runnable resolution, input coercion exposes that value as `Part[]` or
another explicitly declared primary type. Output coercion validates the final
run value against the runnable's declared output type. Setup and state remain
complete snapshots; the executor computes private run ceilings instead of
receiving filtered copies. Child runs inherit setup and state. Source changes
affect only runs accepted after the new state is observed.
