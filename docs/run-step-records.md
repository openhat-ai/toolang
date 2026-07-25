# Run, Step, And Control Records

This document defines durable execution records and the transient events that
project run and step truth.


## Paths And References

`StepPath` identifies a position in one run tree:

```text
run_id[/step_index/...]
```

Examples:

```text
run_abc123
run_abc123/0
run_abc123/0/1
```

`RunControlRef(index, part?)` references a control input in the current run.
`OutputRef(step, part?)` references step output. `ThreadControlRef(thread,
index)` references a durable thread mutation.


## Statuses

```text
RunStatus     pending | running | finished | failed | canceled
StepStatus    running | finished | failed | canceled
ControlStatus pending | finished | canceled | failed
```

Both run and thread controls use `ControlStatus`. For a control, `finished`
means applied rather than merely accepted.


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
superseded_by
created_at
started_at
finished_at
```

`parent` is the calling `StepPath` for a child run. `input` normally references
the index-zero start control. `superseded_by` is a `ThreadControlRef` when a
rewind replaces this run.


## RunControlRecord

```text
run
index
kind
timing
input
request_id
context
status
error
created_at
finished_at
```

`(run, index)` is the durable identity. Kinds are `start`, `steer`, and `stop`.
Timing is `immediate`, `next_step`, or `next_call`. A non-null request ID is
unique across the `run_controls` table. Duplicate request IDs are rejected;
they do not replay a previous result.

The index-zero start control and `RunRecord` are inserted in one transaction.
Later control indexes are computed and inserted in one transaction; no API
reserves an index independently.

`runs.db` also stores an internal monotonic revision on every run-control
insert and status change. The revision is a polling cursor, not part of
`RunControlRecord`'s protocol shape. It lets an owning executor observe remote
steer, stop, and cancellation changes without rescanning controls for every
active run. A storage-only claim flag serializes runtime application against
cross-process cancellation; it is not another `ControlStatus`.


## StepRecord

```text
parent
index
kind
input
output
given
noted
status
error
created_at
started_at
finished_at
```

`(parent, index)` is the durable identity. A step path is `parent / index`.
Step input may contain run-control references, output references, and inline
messages. Step output is ordered `MessagePart[]`: ordinary content uses
`PerceptPart` values (the runtime representation of language `Part`), model
steps may add `ToolCallPart`, and tool steps emit `ToolResultPart`. The
corresponding `PartEnd.data` event carries the same `MessagePart` value that is
later persisted in step output.

`given` contains information known when `StepBegin` is emitted. `noted`
contains additional information recorded by `StepEnd`. Neither repeats the
step's input, output, status, or error.

Step kinds remain intentionally small:

```text
run | agent | human | model | tool | par | loop | system
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
source_thread
anchor_run
request_id
expected_head
context
status
created_at
finished_at
```

Kinds are `create`, `fork`, and `rewind`. `(thread, index)` is the durable
identity. Thread mutations are synchronous, so callers observe their success or
failure directly. Only successful mutations produce thread events.
Non-null request IDs are unique across the `thread_controls` table. Clients
must keep request IDs globally unique across both control tables or pass
`None`.


## Projection Ownership

Control acceptance is not event projection:

```text
RunExecutor.start/steer/stop/cancel_control -> RunStore -> RunControlRecord
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
RunBegin.input  -> finish start control
StepBegin.input -> finish steer controls
RunEnd.input    -> finish stop control
RunEnd          -> fail remaining pending controls
```

The executor's private event projector owns no control transitions.


## Idempotency And Concurrency

SQLite primary keys protect run, control, thread, and step identities.
Non-null request IDs are unique within their control table; duplicate
submissions are rejected rather than replayed. Index allocation and insertion use one
`BEGIN IMMEDIATE` transaction, and every process owns its own SQLite
connection. A configured busy timeout allows concurrent local processes to
serialize writes.

The process that successfully inserts a start control owns execution. Toolang
currently leaves records as-is if the owner process exits; it does not resume
execution.
