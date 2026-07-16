# Run and Step Records

This document defines durable execution records and the trace events from
which they are projected. Records favor storage and queries; events favor
causal execution and streaming.


## Paths And References

`StepPath` identifies a position in one run's step tree:

```text
StepPath = run[/step_index/...]
```

Examples:

```text
run_abc123        run root; not itself a StepRecord
run_abc123/0      first top-level step
run_abc123/0/0    first child of the first top-level step
run_abc123/0/1    second child of the first top-level step
```

Every step index is zero-based and local to its parent. The bare run id is a
valid root path, so a top-level step can use the run id as its `parent`.

Durable data edges use two small references:

```text
InputRef:   cmd, [part]
OutputRef:  step, [part]
```

`InputRef` points to one command input in the record's run. `OutputRef` points
to one step output globally through its `StepPath`.


## Status

```text
RunStatus      pending | running | finished | failed | canceled
StepStatus     running | finished | failed | canceled
CommandStatus  pending | finished | canceled
```

For a command, `finished` means applied, not merely accepted.


## Durable Records

`RunRecord`:

```text
id
parent
thread
input
output
context
status
error
created_at
started_at
finished_at
```

`StepRecord`:

```text
parent
index
kind
input
output
context
detail
status
error
created_at
started_at
finished_at
```

`CommandRecord`:

```text
run
index
kind
apply
input
context
status
error
created_at
finished_at
```

Field rules:

- `RunRecord.parent` is `StepPath | None`.
- `RunRecord.input` is normally `InputRef(cmd=0)`.
- `RunRecord.output` is `OutputRef | None`.
- `StepRecord.parent` is a `StepPath`; `index` is zero-based under it.
- A step's path is `parent / index`.
- `CommandRecord.kind` is `start | steer | stop`.
- `CommandRecord.apply` is `now | next_step | next_call`.
- `(run, index)` identifies one command within a run.
- Every run, including a child run, has command index `0` with kind `start`.

`RunRecord` is the aggregate root for its commands:

```text
RunRecord(id=run_abc123)
  CommandRecord(run=run_abc123, index=0, kind=start)
  CommandRecord(run=run_abc123, index=1, kind=steer)
  CommandRecord(run=run_abc123, index=2, kind=stop)
```

Only `RunRecord.parent` carries run-tree hierarchy. Commands never duplicate
that link.


## Context And Detail

`context` is structured information known when an operation begins. `detail`
is structured execution information known when it ends.

`input` and `output` remain top-level fields because they are data edges, not
metadata:

- `input` is data consumed by a command, run, or step.
- `output` is data produced by a run or step.

Common run context:

```text
source       user, scheduler, trigger, parent run, or another accepted source
executable   kind and name
selectors    resolved run selection
root         root run id when this is a descendant
```

Common command context:

```text
request_id   caller idempotency key when present
source       surface or parent operation that issued the command
```

Common step context:

```text
source       AST kind, name, and span
binding      "_", a named local, or null
reads        local names read by the operation
target       runnable, agent, model, or tool target
item         item index and count
lane         logical lane index and count
loop         loop iteration
body_index   nested statement position
```

Common step detail:

```text
shape        result shape
reshape      executor reshape applied after the operation
usage        token or runtime usage
child_runs   child run ids
items        input or result item count
iterations   loop iteration count
selected     selected item indexes
scores       item-aligned ranking scores
```

Fields that do not apply are omitted. Context and detail are typed per event or
step kind in code; the lists above are vocabulary, not one universal payload.


## Step Kinds

```text
run | agent | human | model | tool | par | loop | system
```

- `run` calls an agic or flow in the current program.
- `agent` calls an executable on another agent.
- `human`, `model`, and `tool` call external actors or capabilities.
- `par` and `loop` organize nested steps.
- `system` records executor-owned work that does not make a call.

Unfolding, folding, filtering, and ranking are result reshapes recorded in
context or detail. They are not separate step kinds. A flow body is already
sequential and does not require a synthetic `seq` step.


## Event Principle

Trace events are the only input to durable execution persistence:

```text
request handling / executor -> trace events -> PersistSink -> records
                                          -> ReplySink(s)
```

Request handling may allocate ids and retain in-memory queue state, but it must
not create or update `RunRecord`, `CommandRecord`, or `StepRecord` directly.
The same rule applies to top-level runs and child runs.

`PersistSink` processes events in order and owns any projection state needed
to build normalized refs. Replaying the same ordered stream must produce the
same durable records.

Projection is idempotent by durable identity:

```text
run      id
command  (run, index)
step     (parent, index)
part     (step, part)
```


## Command Events

Command events mean that the runtime accepted a command. They are not client
request messages.

```text
run_waiting:
  run, cmd, parent?, thread, input, context?, created_at

run_starting:
  run, cmd, parent?, thread, input, context?, created_at

run_steering:
  run, cmd, input, apply, context?, created_at

run_stopping:
  run, cmd, input?, apply, context?, created_at
```

Rules:

- `run_waiting` and `run_starting` have the same shape and identify the same
  start command. The start command uses `cmd=0` and `apply=now` implicitly.
- An external request may emit `run_waiting` followed by `run_starting`, or
  `run_starting` directly.
- Child runs normally emit `run_starting` directly.
- `run_steering` and `run_stopping` include `apply` because `PersistSink` must
  project the complete command without reading another store.

Reusing `(run, cmd)` is an idempotent replay only when immutable command fields
match. In particular, `run_waiting` followed by `run_starting` updates one run
and one start command; it never inserts a second command. A conflicting replay
is a trace error.

Acceptance and application remain separate. This is required for `next_step`
and `next_call`.


## Run And Step Events

Run lifecycle:

```text
run_begin:  run, input, context?, started_at
run_end:    run, status, input?, output?, detail?, error?, finished_at
```

The start event already carries `parent`, `thread`, and command input, so
`run_begin` does not repeat them. Its `input` is normally `InputRef(cmd=0)`.
When a stop command causes cancellation, `run_end.input` references that stop
command. A run that ends without consuming a stop command omits this field.
`run_end.detail` remains trace metadata; `RunRecord` deliberately has no
terminal-detail field and does not merge it into begin-time `context`.

Step lifecycle:

```text
step_begin:  step, kind, input?, context?, started_at
step_end:    step, kind, status, output?, detail?, error?, finished_at
```

`step_begin.input` carries explicit semantic inputs and normalized refs. A
steer command is applied when a later step includes its `InputRef`. The
`context.reads` field identifies additional locals read by the operation;
`PersistSink` combines those names with its local-provenance projection to
construct the complete `StepRecord.input`.

The same input-edge rule completes commands for every lifecycle:

```text
run_begin.input   -> finish referenced start command
step_begin.input  -> finish referenced steer commands
run_end.input     -> finish referenced stop command
```

At `run_end`, any command that remains pending is marked `canceled`.

Command transitions are monotonic:

```text
pending -> finished
pending -> canceled
```

The first consuming input edge finishes a pending command. Later references to
the same command are valid data dependencies but do not change its status or
timestamp again.

Part lifecycle:

```text
part_begin:  step, part, type
part_delta:  step, part, delta
part_end:    step, part, data
```

Informal traces may write `step_begin[KIND]` or `step_end[KIND]`. Serialized
events retain `kind` and `status` as fields.

Names are deliberately short:

- `run` is a run id.
- `cmd` is a zero-based command index within that run.
- `step` is a full `StepPath`.
- `part` is a zero-based output part index within that step.

Part data uses the canonical `Part` shapes from `toolang.base`. Delta data uses
the corresponding canonical `Delta` shapes.


## Persistence Projection

`PersistSink` applies events as follows:

```text
run_waiting / run_starting
  upsert pending RunRecord and its pending start CommandRecord

run_steering / run_stopping
  create the pending CommandRecord identified by (run, cmd)

run_begin
  finish commands referenced by input and change RunRecord to running

step_begin
  finish commands referenced by input and create a running StepRecord

part events
  stream canonical in-progress output; step_end carries the complete durable
  output

step_end
  finish the StepRecord and update local provenance for its binding

run_end
  finish commands referenced by input, finish the RunRecord, set output from
  primary-local provenance, and cancel any remaining pending CommandRecords
```

The sink maintains projection-only provenance for each run's local names:

```text
start input         `_` -> InputRef(cmd=0)
successful binding  name -> OutputRef(step=step_path)
discarded binding   no change
```

This state is not executor `Local` data and is not visible to flow semantics.


## Projection Timestamps

Record timestamps come from the event that establishes the corresponding
fact:

```text
event                         projected timestamp
run_waiting / run_starting    RunRecord.created_at, CommandRecord.created_at
run_begin                     RunRecord.started_at
step_begin                    StepRecord.created_at and started_at
step_end                      StepRecord.finished_at
run_end                       RunRecord.finished_at
```

When an event input first consumes a command, that event's lifecycle timestamp
becomes `CommandRecord.finished_at`:

```text
run_begin.started_at   start command
step_begin.started_at  steer command
run_end.finished_at    stop command
```

Commands canceled by `run_end` also use `run_end.finished_at`. Replayed or
duplicate events must not replace an already established timestamp.


## Causal Order

Events for a direct start appear in this order:

```text
[run_waiting]
run_starting
run_begin input={cmd: 0}
...
run_end
```

A steer command appears before the first step that observes it:

```text
run_steering run=run_abc123 cmd=1
step_begin step=run_abc123/2 input=[{cmd: 1}, ...]
```

Applying steer without such a step input is invalid. A runtime that cannot
reach an applicable checkpoint leaves the command pending; `run_end` then
cancels it.

A stop command appears as:

```text
run_stopping
terminal child step and run events
run_end[canceled] input={cmd: 2}
```

Terminal child steps and child runs must be emitted before the parent
`run_end[canceled]`.

A waiting run may be canceled before it begins:

```text
run_waiting run=run_abc123 cmd=0
run_stopping run=run_abc123 cmd=1
run_end[canceled] input={cmd: 1}
```

The stop command becomes `finished`; the unapplied start command becomes
`canceled`.


## Child Runs

A child run is linked through its start event and resulting `RunRecord.parent`:

```text
step_begin[run] step=run_abc123/0/0
run_starting run=run_def456 cmd=0 parent=run_abc123/0/0
run_begin run=run_def456 input={cmd: 0}
...
run_end run=run_def456 status=finished
step_end[run] step=run_abc123/0/0 status=finished
```

The child uses the same record shape as a top-level run:

```text
RunRecord(id=run_def456, parent=run_abc123/0/0, input={cmd: 0}, ...)
CommandRecord(run=run_def456, index=0, kind=start, ...)
```


## Streaming Projection

Durable records keep the complete tree. A public stream is a linear projection
of that tree.

The default UI stream includes the subscribed root run. Parent `run` or `agent`
steps remain visible, while child lifecycle and child internal steps may be
hidden. Optional scopes may include child lifecycle, child steps, and
descendants up to a selected depth.
