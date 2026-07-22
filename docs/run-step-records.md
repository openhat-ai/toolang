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
RunStatus           pending | running | finished | failed | canceled
StepStatus          running | finished | failed | canceled
RunControlStatus    pending | finished | canceled | failed
ThreadControlStatus pending | finished | canceled | failed
```

For a control, `finished` means applied rather than merely accepted.


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
unique within its run and provides idempotent acceptance.

The index-zero start control and `RunRecord` are inserted in one transaction.
Later control indexes are computed and inserted in one transaction; no API
reserves an index independently.


## StepRecord

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

`(parent, index)` is the durable identity. A step path is `parent / index`.
Step input may contain run-control references, output references, and inline
messages.

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
result_run
message
request_id
expected_head
context
status
error
created_at
finished_at
```

Kinds are `create`, `fork`, and `rewind`. `(thread, index)` is the durable
identity. Thread mutations are synchronous, so callers observe their success or
failure directly. Only successful mutations produce thread events.


## Projection Ownership

Control acceptance is not event projection:

```text
RunExecutor.start/steer/stop -> RunStore -> RunControlRecord
ThreadManager operations     -> RunStore -> ThreadControlRecord
```

Run and step facts are event projection:

```text
runtime -> RunEvent -> PersistSink -> RunRecord / StepRecord
```

After `PersistSink` commits an event fact, runtime updates referenced controls:

```text
RunBegin.input  -> finish start control
StepBegin.input -> finish steer controls
RunEnd.input    -> finish stop control
RunEnd          -> fail remaining pending controls
```

`PersistSink` owns no control transitions.


## Idempotency And Concurrency

SQLite primary keys protect run, control, thread, and step identities.
Request IDs protect caller retries. Index allocation and insertion use one
`BEGIN IMMEDIATE` transaction, and every process owns its own SQLite
connection. A configured busy timeout allows concurrent local processes to
serialize writes.

The first process to insert a start control owns execution. A second process
with the same run and request ID reads existing records and must not execute the
run again. Toolang currently leaves records as-is if the owner process exits;
it does not resume execution.
