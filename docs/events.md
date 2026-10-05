# Events

Execution events describe live Run/Step activity and committed thread mutations.
`ExecutionEvent` is the union of `RunEvent` and `ThreadEvent`. They are observation
contracts, not a stored event log: [records](records.md) are durable truth, while
[execution](execution.md) owns acceptance, controls and lifecycle transitions.

## Run events

| Event | Protocol `type` | Meaning |
| --- | --- | --- |
| `RunBegin` | `run_begin` | Execution begins under a Run entry Control, with optional parent Step and occurrence. |
| `StepBegin` | `step_begin` | A Step begins with its kind, typed `given` facts, State and data/control references. |
| `PartBegin` | `part_begin` | A streamed output Part starts, identified by Step, ordinal and `part_type`. |
| `PartDelta` | `part_delta` | A typed delta updates that live Part. |
| `PartEnd` | `part_end` | The Part completes with its canonical `data`. |
| `StepEnd` | `step_end` | A Step reaches a terminal status with output, typed `noted` facts and optional error/cancellation cause. |
| `RunEnd` | `run_end` | A Run reaches a terminal status with output and optional error/cancel Control. |

A Run's events enclose its execution; a Step's begin/end delimit its activity.
Part ordinals are local to their Step's output. Deltas may be absent when a
provider or tool supplies a complete Part. Reasoning uses the same Part event
family, and provider metadata stays on canonical Parts; presentation decides
what to display.

Parallel branches can interleave. Observer delivery is serialized within one
root tree, but sibling completion order is not statement order and independent
root Runs have no shared event ordering guarantee. A child Run's parent
reference expresses ownership, not necessarily overlapping lifetimes: model
`_toolang/run` starts its child after the triggering Tool Step has finished.
See [child invocation timing](execution.md#execution-and-assembly).

## Persistence and tracing

`RunExecutor` owns a mandatory private record projector and an optional
`RunTracer`. At each event boundary it applies applicable Run/Step changes and
executor-owned Control status changes in one store transaction, then awaits
`RunTracer.on_event()`. The projector itself never authors Controls. Part
events do not create event rows or a durable delta log; terminal Step output
retains the completed or available partial result.

One tracer observes the recursive tree started by its caller. Tracer callbacks
run serially on the owner event loop, after durable projection, regardless of
which worker performed a synchronous tool call. Ordinary tracer exceptions are
logged and isolated from execution. A tracer cannot replace or disable
persistence and need not infer missing output or execution identities.

## Controls and data edges

Control acceptance is a durable write, not a separate Run event. There are no
waiting, starting, steering or stopping variants. Events retain references to
the facts they consume or apply:

- `RunBegin.control` identifies the Run's entry Control.
- `StepBegin.input` identifies consumed data, including prior outputs and
  Control input fields; `preceded_by` identifies preceding applied Controls.
- `StepEnd.aborted_by` identifies the Control that interrupted a canceled Step,
  when present.
- `RunEnd.control` identifies the cancel Control that canceled the Run, when
  present.

Use [Control records](records.md#controlrecord) to inspect status and payload.
Do not derive control acceptance from display phases or invent extra events for
pending controls.

## Thread events

`ThreadManager` emits one success event after committing each mutation:

| Event | Protocol `type` | Additional facts |
| --- | --- | --- |
| `ThreadCreated` | `thread_created` | Origin, peer and the create Control. |
| `ThreadForked` | `thread_forked` | Source thread, terminal anchor Run and fork Control. |
| `ThreadRewound` | `thread_rewound` | Anchor, ejected logical Run IDs and rewind Control. |

Every event identifies its thread and Control. `ThreadListener.on_event()` is
synchronous and runs after commit. Ordinary listener exceptions are logged and
do not roll back the mutation. Failed operations raise/return their errors and
emit no failure event. Rewind removes logical history membership, not the
underlying stored Runs and Steps. [Thread lifecycle](execution.md#threads) owns
anchor selection, branchability and concurrency checks.

## Protocol and consumers

`event_to_data()` serializes either event family; `run_event_to_data()` and
`run_event_from_data()` handle the Run subset. The `type` discriminator selects
the variant. References, typed Step facts, occurrences and output edges use the
same canonical protocol encodings as records.

[HTTP streaming](api.md#live-streaming) transports these events as SSE and owns
subscription/disconnection behavior. There is no historical event replay or
replay cursor. Reconnecting clients read durable records for missed state and
observe new events; they cannot recover missed deltas or exact interleaving.
[Execution presentation](execution-presentation.md) derives terminal output from
Run events. CLI preparation `ProgressEvent` values are a separate presentation
contract, not execution lifecycle events.

## Implementation and verification

[Event types/codecs](../src/toolang/execution/events.py),
[executor delivery](../src/toolang/execution/executor/executor.py),
[record projection](../src/toolang/execution/executor/_persist.py), and
[thread notifications](../src/toolang/execution/threads.py) own these contracts.
[Codec tests](../tests/unit/execution/test_events.py),
[event scenarios](../tests/integration/execution/test_event_scenarios.py), and
[thread controls](../tests/integration/execution/test_thread_control_scenarios.py)
cover serialization, ordering, post-commit observation and listener failures.
