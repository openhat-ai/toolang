# Run, Step, And Control Records

This document defines durable execution records and the transient events that
project run and step truth.


## Paths And References

`StepPath` globally identifies one step within its owning run:

```text
run_id.step_index[.step_index...]
```

Examples:

```text
run_abc123.0
run_abc123.0.1
```

`RunInputRef(index, part?)` references a control input in the current run.
`StepOutputRef(step, part?)` references step output. `ValueRef` is their union.
`RunControlRef(run, index)` globally references a durable run mutation.
`ThreadControlRef(thread, index)` references a durable thread mutation.


## Statuses

```text
RunStatus     pending | running | succeeded | failed | canceled
StepStatus    pending | running | succeeded | failed | canceled
ControlStatus pending | applied | wontapply | revoked
```

Both run and thread controls use `ControlStatus`. `StepStatus.pending` is
reserved for future use and is not currently emitted.


## RunRecord

```text
id
parent
thread
input
output
context
status
error
ejected
created_at
started_at
finished_at
```

`parent` is the calling `StepPath` for a child run. `input` normally references
the index-zero run control and remains persisted rather than inferred from
the first step. `output` is a `ValueRef`, so a pass-through run may point
directly to a control input while computed output points to a step.
`ejected` identifies the thread or run control that removed this run from the
effective projection.

Retry does not write `RunRecord.ejected`: it ejects steps. A child run whose
calling `StepPath` was ejected is consequently omitted from effective run and
thread projections while its durable run record remains available to audit
reads.

Run and step errors share one compact shape:

```text
ExecutionError = string | StepErrorRef(step: StepPath)
```

The step that directly catches an exception stores its message string. An
enclosing step or run stores `{"step": "run_id/path"}` to reference the step
that owns the next error detail. A runtime failure outside a step stores its
message directly on the run and does not create a synthetic diagnostic step.


## RunControlRecord

```text
run
index
kind
timing
input
source
anchor
request
context
status
error
created_at
finished_at
```

`(run, index)` is the durable identity. Kinds are `start`, `rerun`, `retry`,
`steer`, and `stop`.
Timing is `immediate`, `next_step`, or `next_call`. A non-null request is
unique across the `run_controls` table. Duplicate requests are rejected;
they do not replay a previous result.

The index-zero `start` or `rerun` control and `RunRecord` are inserted in one
transaction. A rerun control references its source root through `source`.
`retry` is appended to the existing root and records the resolved `StepPath` in
`anchor`.
Later control indexes are computed and inserted in one transaction; no API
reserves an index independently.

For a root run, its entry control and each retry control store the effective
`RunLimits`:

```text
limits:
  agic_model_calls
  agic_tool_calls
  tokens
  cost
  time
```

Child runs inherit root-tree limits during execution and do not repeat this
context. `cost` is decimal USD text; the other values are integers or null.

`runs.db` also stores an internal monotonic revision on every run-control
insert and status change. The revision is a polling cursor, not part of
`RunControlRecord`'s protocol shape. It lets an owning executor observe remote
steer, stop, and cancellation changes without rescanning controls for every
active run. A storage-only claim flag serializes runtime application against
cross-process cancellation; it is not another `ControlStatus`.


## StepRecord

```text
path
kind
input
output
given
noted
status
error
ejected
created_at
started_at
finished_at
```

`path` is the complete `StepPath`. SQLite stores its owning `run` and local
index path separately and uses `(run, path)` as the durable identity. Step
input may contain run-control references, step-output references, and inline
messages. Step output is ordered `MessagePart[]`: ordinary content uses
`PerceptPart` values (the runtime representation of language `Part`), model
steps may add `ToolCallPart`, and tool steps emit `ToolResultPart`. The
corresponding `PartEnd.data` event carries the same `MessagePart` value that is
later persisted in step output.

`given` contains information known when `StepBegin` is emitted. `noted`
contains additional information recorded by `StepEnd`. Neither repeats the
step's input, output, status, or error. Both fields use kind-specific typed
payloads rather than open dictionaries.

For a Tool Step, both lifecycle payloads use the same `summary` key:

```text
given:
  plugin
  call
  summary
noted:
  summary
```

`given.summary` is the running description known at `StepBegin`.
`noted.summary` is the succeeded, failed, or canceled description selected at
`StepEnd`. New execution always records both. Readers accept older Tool Steps
without either summary and use the legacy tool-name presentation.

A succeeded flow step also notes its typed local result (`shape`, `type`, and
`value`). This makes the step a reusable commit boundary for retry without a
separate checkpoint record. `ejected` is the `RunControlRef` of the retry that
removed the step from the effective projection. Normal inspection excludes
ejected steps; audit reads may include them.

For a completed model step, `noted` stores its accounting facts:

```text
tokens:
  input
  output
price:
  input
  output
cost
```

Token counts are integers. Input and output prices are decimal USD-per-token
text captured from the run's `AgentSetup`; cost is the decimal USD total
computed from those prices and counts. Missing usage or pricing is stored as
null. These facts are recorded whether or not the root run has token or cost
limits.

Step kinds remain intentionally small:

```text
run | agent | human | model | tool | par | loop | value
```


## ThreadRecord

```text
thread_id
origin
peer
created_by
head
created_at
updated_at
```

`created_by` references the create or fork control at index zero. `head`
references the latest successful thread control and is used for optimistic
concurrency.


## ThreadControlRecord

```text
thread
index
kind
source
anchor
request
expected_head
context
status
created_at
finished_at
```

Kinds are `create`, `fork`, and `rewind`. `(thread, index)` is the durable
identity. Thread mutations are synchronous, so callers observe their success or
failure directly. Only successful mutations produce thread events.
Non-null requests are unique across the `thread_controls` table. Clients
must keep requests globally unique across both control tables or pass
`None`.


## Projection Ownership

Control acceptance is not event projection:

```text
RunExecutor.run/rerun/retry/steer/cancel/cancel_control
    -> RunStore -> RunControlRecord
ThreadManager operations     -> RunStore -> ThreadControlRecord
```

Run and step facts are event projection:

```text
runtime -> RunEvent -> RunExecutor persistence -> RunRecord / StepRecord
```

For model steps, `given` references the normalized `ModelCall`:
content-addressed instructions, ordered canonical messages, one
content-addressed toolset, and opaque adapter state. It also records a
non-secret effective model-target snapshot, including the provider, model,
adapter, base URL, options, and streaming mode. API keys and headers are never
stored. `RunStore.rebuild_model_call()` resolves the call references without
depending on a provider-specific HTTP payload.

Durable `StepRecord.given["call"]` keeps those compact references.
Caller-facing inspection resolves the same field back to normalized
`ModelCall` data, so storage details do not become a second public shape.

The runtime projects each event fact and its referenced control transitions in
one SQLite write transaction:

```text
RunBegin.input  -> apply run control
StepBegin.input -> apply steer controls
RunEnd.input    -> apply cancel control
RunEnd          -> mark remaining pending controls wontapply
```

The executor's private event projector owns no control transitions.


## Idempotency And Concurrency

SQLite primary keys protect run, control, thread, and step identities.
Non-null `request` values are unique within their control table; duplicate
submissions are rejected rather than replayed. Index allocation and insertion use one
`BEGIN IMMEDIATE` transaction, and every process owns its own SQLite
connection. A configured busy timeout allows concurrent local processes to
serialize writes.

The process that successfully inserts a run entry or retry control owns its
execution attempt. Toolang currently leaves records as-is if that process
exits; automatic owner-loss recovery remains separate from explicit retry.
