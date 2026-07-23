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


## Durable Truth

`runs.db` contains:

- `ThreadRecord` and `ThreadControlRecord`;
- `RunRecord` and `RunControlRecord`;
- `StepRecord` and complete step outputs;
- deduplicated prompt bodies and existing agent-local updates.

Run events are transient facts emitted during execution. They are never stored
as `EventRecord` rows. A reconnecting caller reconstructs current state from
records and observes only new live events.

Persistence makes completed history available after process restart and for
later model calls. Toolang does not resume an unfinished run after its owner
process exits.


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

Step indexes are local to their parent step path and are protected by the
`(parent, index)` primary key. One process owns execution of a run tree.


## Run Execution

`RunExecutor` is the public run entry point:

```text
start(AgentSetup, AgentState, RunRequest, tracer?) -> RunRecord
stop(run_id, timing, request_id?, reason?)         -> RunControlRecord
steer(run_id, message, timing, request_id?)        -> RunControlRecord
shutdown()                                         -> None
```

`start()` runs to completion. It does not create a background task for its
caller. `steer()` and `stop()` only accept durable controls; they do not need
the target run to be owned by the submitting process. The executor is ready
after construction and therefore has no separate `open()` method. `shutdown()`
is terminal and cancels the run tasks owned by that executor instance.
The process owner closes the shared `RunStore` after the executor shuts down.

`start()` requires an existing thread. Thread creation belongs to
`ThreadManager` or to the package that owns a deterministic external thread id.

The start request atomically inserts the pending run and its index-zero start
control. Only the process that performs the first insertion obtains execution
ownership. A retry with the same `(run_id, request_id)` returns existing truth
without executing the run twice.


## Mandatory Persistence And Tracing

`RunExecutor` constructs one internal `PersistSink` from its `RunStore`.
Persistence cannot be replaced or disabled by callers.

For every `RunEvent`, ordering is:

```text
runtime produces event
  -> PersistSink projects RunRecord or StepRecord
  -> runtime updates referenced RunControlRecord statuses
  -> optional RunTracer observes the event
```

`PersistSink` never creates or updates run controls. Tracer failures are logged
and isolated from execution. One tracer observes the complete run tree started
by its `start()` call, including child runs, steps, parts, and terminal events.


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
- `StepBegin.input` references applied steer controls;
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

The run owner polls pending controls from SQLite. Local and remote submissions
use the same path; there is no process-local wake optimization. An immediate
stop cancels the owning task after the owner observes the durable control.


## Threads

`ThreadManager` synchronously performs `create()`, `fork()`, and `rewind()`.
Create and fork return the newly allocated thread id. Rewind changes the
specified thread in place and returns nothing. Fork and rewind accept an
optional run id; omission selects the last visible top-level run. Recursive
child runs are never thread anchors. An empty thread has no implicit anchor and
cannot be forked or rewound.

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
run-control rows. Its inherited history includes the anchor. A rewind discards
its anchor and the visible suffix after it. Runs owned by the rewound thread
are marked with `superseded_by`; an inherited source run is never modified.
Affected active runs are stopped through durable controls written directly by
the manager. Forks and rewinds are serialized across processes with an
agent-local file lock. The store keeps expected-head comparison as an internal
defensive check; it is not part of the public manager API.


## State Capture

Each accepted run captures one explicit immutable `AgentState` and
`toolang.up.setup.AgentSetup`. `AgentSetup` supplies the agent home and installed
runtime implementations. Child runs inherit both. Source changes affect
only runs accepted after the new state is observed.

Scheduled work also captures its effective job definition in run context.
Execution never rereads authored task or chore files after acceptance.
