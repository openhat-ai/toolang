# Run and Step Records

This document captures the proposed run, step, command, and trace-event model.
It is intentionally narrow: records are designed for durable storage and
queries, while trace events are designed for readable streaming.


## Core Types

`TracePath` identifies a run or a step globally:

```text
TracePath = run[/step/...]
```

Examples:

```text
run_a        # run
run_a/1      # top-level step
run_a/1/2    # nested step
```

Reference shapes:

- `InputRef`: `cmd`, optional `part`
- `OutputRef`: `step`, optional `part`

`InputRef` points to command input. `OutputRef` points to step output.


## Status

Run status:

```text
pending | running | finished | failed | canceled
```

Step status:

```text
running | finished | failed | canceled
```

Command status:

```text
pending | finished | canceled
```

For commands, `finished` means the command was applied.


## Records

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

Field notes:

- `RunRecord.parent` is `TracePath | None`.
- `RunRecord.input` is an `InputRef`.
- `RunRecord.output` is `OutputRef | None`.
- `StepRecord.parent` is a `TracePath`.
- `StepRecord.index` is local under `parent`.
- A step path is derived as `parent / index`.
- `CommandRecord.kind` is `start | steer | stop`.
- `CommandRecord.apply` is `now | next_step | next_call`.


## Context and Detail

`context` is begin-time structured information. It is known before execution
starts.

`detail` is end-time structured execution detail. It is known when the step
finishes.

`input` and `output` remain top-level fields because they are data edges:

- `input` is consumable input to a run, step, or command.
- `output` is consumable output from a run or step.

Suggested context shapes:

- `source`: source/program link such as kind, name, index, label, span
- `shape`: current shape information
- `item`: item index and count
- `lane`: lane index and count
- `loop`: loop index
- `target`: target kind and name
- `refs`: related `InputRef` or `OutputRef`
- `params`: structured parameters

Suggested detail shapes:

- `usage`: token or runtime usage
- `child_runs`: child run ids
- `pending`: pending item-aligned values produced by `par`
- `selected`: selected item indexes
- `scores`: score values or score refs
- `preview`: compact value preview


## Step Kinds

Control steps:

```text
seq
par
```

Call steps:

```text
run
agent
```

Action steps:

```text
model
tool
unfold
map
filter
sort
fold
system
```


## Trace Events

Trace events use globally readable names. Record fields may stay normalized for
storage, but stream events should be easy for UI consumers to read.

Run command acceptance events:

```text
run_waiting:   run, input, context?
run_starting:  run, input, context?
run_steering:  run, input, context?
run_stopping:  run, reason?, context?
```

These events are stream signals emitted after the runtime accepts a command.
They do not duplicate the durable command record. The event type already
identifies the command kind, and command application policy remains on
`CommandRecord.apply`.

`run_waiting` and `run_starting` have the same shape. Some requests emit
`run_waiting` and then `run_starting`; others emit `run_starting` directly.
Some runs, including child runs, may begin without a waiting event.

Run lifecycle events:

```text
run_begin:     run, parent?, thread?, input?, context?
run_end:       run, status, output?, detail?, error?
```

Step events:

```text
step_begin:    step, kind, input?, context?
step_end:      step, kind, status, output?, detail?, error?
```

Part events:

```text
part_begin:    step, part, type
part_delta:    step, part, delta
part_end:      step, part, data
```

When discussing event streams informally, step events may be abbreviated as
`step_begin[KIND]` or `step_end[KIND]`. The serialized event still carries
`kind` as a field. `part_begin.type` is a part type.

Names:

- `run` is a run id.
- `step` is a full `TracePath`.
- `part` is an output part index.

Part data shapes:

```text
text:        type, text
image:       type, image_url?, file_id?, detail, filename?, media_type?
audio:       type, data, format, filename?, media_type?
file:        type, file_data?, file_url?, file_id?, filename?, media_type?
tool_call:   type, tool_call_id, tool_name, tool_family, input, call_id?
tool_result: type, tool_call_id, tool_name, tool_family, output, call_id?
```

Delta shapes:

```text
text:      kind, text
tool_call: kind, text, tool_call_id
```


## Child Runs

A child run is linked by `RunRecord.parent`.

```text
step_begin[run] step=run_a/1/1
run_begin run=run_b parent=run_a/1/1
...
run_end run=run_b
step_end[run] step=run_a/1/1 status=finished
```

Child run input is stored as a normal start command:

```text
CommandRecord(run=run_b, index=0, kind=start, ...)
RunRecord(id=run_b, input={cmd: 0}, parent=run_a/1/1, ...)
```


## Streaming Projection

Durable records store the complete tree. Streaming is a linear projection of
that tree.

Default UI streams include only the subscribed root run's events. The parent
run still includes `step_begin[run]` and `step_end[run]` call steps, but child
run lifecycle and child internal steps are hidden by default.

Optional stream scopes may include:

- child run lifecycle events
- child run internal step events
- descendant runs up to a chosen depth
