# Execution Model

This document defines Toolang's runtime execution boundaries. Exact durable
record and event fields are specified in
[run-step-records.md](./run-step-records.md); executor control flow is specified
in [executor.md](./executor.md).


## State Forms

Toolang uses three source-state forms:

| State Form | Meaning |
| --- | --- |
| `durable` | Authored files discovered under the root and agent home |
| `prepared` | Immutable root and home generations derived from authored source |
| `agent` | One exact `RootPrepared + HomePrepared` pair used by execution |

A run captures immutable `AgentState` and `AgentSetup` values when accepted,
and child runs inherit them from their parent. Later source updates therefore
affect only requests accepted later.

Scheduled and manually triggered jobs also capture their `JobDefinition` as
run metadata when claimed. Execution consumes that metadata and never reads
task or chore files, so a job edit cannot change an accepted run.


## Durable Store

`runs.db` owns durable thread and execution truth:

- threads
- runs
- commands
- steps and output parts
- agent-local updates
- deduplicated prompt bodies

`.runtime/jobs.db` is a scheduler projection for ready task and chore
documents. `.runtime/files.db` is a file-request claim projection. Neither is a
transcript or run-history store.


## Durable Execution Records

The three execution records are:

```text
RunRecord
  id, parent, thread, input, output, context, status, error,
  created_at, started_at, finished_at

CommandRecord
  run, index, kind, apply, input, context, status, error,
  created_at, finished_at

StepRecord
  parent, index, kind, input, output, context, detail, status, error,
  created_at, started_at, finished_at
```

`RunRecord` is the aggregate root. Every run has command index `0` with kind
`start`; later `steer` and `stop` commands belong to the same run. Child runs
use `RunRecord.parent` to point at the calling `StepPath`.

Step indexes are zero-based and local to their parent. A full step path is:

```text
StepPath = run[/step_index/...]
```

Durable data edges use `InputRef` and `OutputRef`. Runtime `Local` values do not
contain these refs.


## Record Projection

Trace events are the only source of durable execution records:

```text
request handling / executor -> TraceEventHandler -> PersistSink -> runs.db
                                                -> ReplySink(s)
                                                -> resource event bus
```

Request handling may allocate run and command ids. Neither request handling nor
the executor may create or update execution records directly.

`PersistSink` consumes the ordered trace, creates records, applies lifecycle
updates, accumulates parts, and maintains projection-only input/output
provenance. Replaying the same trace must produce the same record state.
Resource event streaming is a separate trace projection and never persists
run, command, or step truth.


## Run Lifecycle

An external start may wait:

```text
run_waiting
run_starting
run_begin input={cmd: 0}
step and part events
run_end
```

Waiting is optional. Child runs normally start directly:

```text
parent step_begin[run]
child run_starting
child run_begin input={cmd: 0}
...
child run_end
parent step_end[run]
```

`run_waiting`, `run_starting`, `run_steering`, and `run_stopping` are runtime
acceptance facts, not client requests. Existing execution events record command
application through their input edges.

`run_begin` means execution started. `run_end` is emitted exactly once by the
executor and carries the terminal status:

```text
finished | failed | canceled
```

The accepted run context records the immutable agent-state fingerprint used by
that run. Child runs inherit the same fingerprint.


## Step Lifecycle

Every real operation emits:

```text
step_begin
[part_begin, part_delta..., part_end]...
step_end
```

Step kinds are intentionally small:

```text
run | agent | human | model | tool | par | loop | system
```

- `run` and `agent` create child runs.
- `human`, `model`, and `tool` call actors or capabilities.
- `par` and `loop` organize child steps.
- `system` records executor-owned work.

Flow transformations such as unfold, fold, filtering, and ranking are result
reshapes, not additional step kinds.


## Executor Locals

Each run owns:

```python
locals: dict[str, Local]
```

One `Local` contains only runtime data:

```text
value
shape = none | item | list
```

`_` is the primary local. Run input initializes it, ordinary statements replace
it, and run output reads it. Named parameters and `let` bindings use other
local names.

A step reads an immutable snapshot and returns a `Local`. The surrounding flow
updates its locals only after the step succeeds. Nested and parallel work uses
private copies, so a failed child or sibling branch cannot partially update
parent state.


## Agic And Flow

An `AgicDecl` executes the dynamic model/tool loop. Model responses may create
additional model and tool steps at runtime. Its terminal assistant parts are
decoded according to the declared output type and become the primary local.

A `FlowDecl` executes its authored statements in source order. Each statement
may read current locals, produces one result, and then applies its independent
binding:

```text
statement       write `_`
let name = ...  write `name`
let ...         discard the result
```

Runnable flow statements recursively start agic or flow child runs. The
top-level executable itself is never wrapped in a synthetic step.


## Commands And Cancellation

Command application modes are:

```text
now | next_step | next_call
```

Agic loops and flows consume accepted commands at explicit checkpoints.
`run_begin.input` references the applied start command, `step_begin.input`
references applied steer commands, and `run_end.input` references the stop
command that caused cancellation. When a run ends, `PersistSink` marks any
accepted but unapplied commands as canceled.

Command status is therefore derived rather than announced by another event.
The consuming event supplies `CommandRecord.finished_at`; repeated references
do not change an already terminal command.

Stopping cancels the active task. Cancellation unwinds from child steps and
child runs toward the root, emitting terminal events in that order. The parent
run ends only after visible descendants have ended.


## Canonical Content

Toolang uses the shared content model from `toolang.base`:

- `Message`: one role plus ordered parts
- `Part`: one complete content part
- `Delta`: one streaming part update

`Part[]` is a Toolang value type. It normally occupies one `Local` with
`shape=item`; it is not a flow collection merely because its value is an
array. Content parsing and executable signatures are defined in
[input-syntax.md](./input-syntax.md) and [program.md](./program.md).


## Replies

Reply sinks project the trace for callers without changing durable truth.

Examples include:

- buffered script results
- SSE/public trace streams
- AI SDK UI message streams
- channel messages
- terminal UI updates

The AI SDK stream is a transport projection containing events such as text
deltas, tool input/output, step boundaries, finish, and error. It is not the
durable execution model.


## Live UI Projection

The TUI maps trace events to mutable blocks. A block owns only the state needed
to render itself and exposes:

```text
create(event)
update(event)
render()
```

The event handler creates or finds a matching live block, updates it, and asks
the application to finalize it when the terminal event arrives. Finalization
is framework behavior: the block's current render is written to scrollback and
the live window is removed.

Typical mappings are:

```text
run_starting   create RunStartBlock
run_begin      update and finalize RunStartBlock
run_steering   create RunSteerBlock
step_begin     finalize referenced RunSteerBlock; create a step-kind block
part events    update the matching step block
step_end       update and finalize the matching step block
run_stopping   update RunStopBlock to canceling
run_end        update and finalize RunStopBlock
```

Multiple live blocks may exist for nested or parallel activity. Block keys use
run and step identity so interleaved events update the correct block. The TUI
does not infer execution state that was not emitted by the runtime.


## Inspection Views

Run detail is exposed as:

- `info`
- `input`
- `output`

Thread detail is exposed as:

- `info`
- `runs`

These views project from `runs.db`; there is no separate durable chat store.
