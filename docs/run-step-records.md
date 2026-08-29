# Execution Records And Pointers

This document describes durable execution records, their canonical JSON, and
the Pointers used to address them. Run events remain the transient projection
input; records are the durable source of truth.

## Pointer

One `Pointer` addresses any Thread, Control, Run, or Step record and any field
inside its canonical JSON:

```text
POINTER = THREAD_ID [ "@" INDEX ] *( "/" TOKEN )
        | RUN_ID [ 1*( "." INDEX ) | "@" INDEX ] *( "/" TOKEN )
```

Run ids occupy the reserved `run_` namespace. Thread ids cannot begin with
`run_`. `.` enters the Step hierarchy, `@` selects a Control index, and `/`
enters a field. A Control belongs to a Thread or Run, never a Step.

```text
term_ab12                         ThreadRecord
term_ab12@0                       ControlRecord
run_ab12                          RunRecord
run_ab12@1                        ControlRecord
run_ab12.0.1                      StepRecord
run_ab12/output/value             Run field
run_ab12.0/output/value/0         Step field
run_ab12@1/payload/locals/0/value Control field
```

Field tokens use RFC 6901: `~0` represents `~`, `~1` represents `/`, and an
empty token addresses an empty member name. Array indexes are canonical
non-negative decimal integers. `-` is not a readable index. `:` is reserved and
cannot occur in a field name stored in execution history.

A `TypedPointer` adds the runtime type expected after resolution:

```text
run_ab12.0/output/value:Part[]
```

The canonical form is always `POINTER:TYPE`. The former implicit output/local
semantics and `TYPE@POINTER` form are not accepted. Record roots mean records;
runtime value and error Pointers therefore name `/output/value` or `/error`
explicitly.

## Canonical JSON

Canonical record JSON is the record itself, without an inspection envelope,
embedded children, summaries, or SQLite-only columns. `StepPath` and `Pointer`
fields are strings. `ControlRef` retains `target` and `index`. `Local` uses:

```text
type
value
name
dim
```

A `TypedPointer` inside a Local value uses its canonical `POINTER:TYPE` text.
Every object member name, including names in user JSON and structured values,
is checked for the reserved `:` before the value enters durable state.

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

`created_by` identifies the successful create or fork Control at index zero.
`head` identifies the latest successful thread Control and provides optimistic
concurrency for rewind and fork operations.

## ControlRecord

Run and thread Controls share one durable model:

```text
target
index
kind
payload
request
status
timing
error
created_at
finished_at
```

`target` plus `index` is the identity. The target namespace determines whether
the Control belongs to a Run or Thread; canonical JSON does not add a synthetic
scope field.

Current kinds are:

```text
run | rerun | retry | reload | execute | steer | cancel
create | fork | rewind
```

`kind` selects the typed payload variant. Run preparation payloads contain the
resolved resources, limits, State revision, runnable, model, input Locals, and
sandbox. Retry and rerun add their source boundary. Reload stores a State
revision. Execute stores the source ToolCall Pointer and Pointer-backed input
Locals. Steer and cancel store injected Locals. Create has an empty payload;
fork and rewind store their thread-history boundaries.

Control status is `pending`, `applied`, `wontapply`, or `revoked`. Timing is
`immediate`, `next_step`, or `next_call`. The database also maintains claim and
change-revision columns for concurrency and polling; these are not part of
canonical record JSON.

## RunRecord

```text
id
parent
thread
control
state
output
occurrence
status
error
ejected_by
created_at
started_at
finished_at
```

`parent` is the calling `StepPath` for a child Run. `control` is the preparation
Control for the attempt. `state` identifies the preparation or reload Control
that supplied immutable Agent State. `output` is a typed Local and may contain
a `TypedPointer` to an explicit `/output/value` field. `error` is either a
message or a Pointer to an explicit `/error` field. `ejected_by` identifies the
Control that removed the Run from the visible projection.

## StepRecord

```text
path
kind
input
given
state
output
occurrence
noted
status
error
ejected_by
created_at
started_at
finished_at
```

`path` is the complete `StepPath`. `input` is an ordered tuple of explicit
Pointers. `state` captures the immutable State Control used for the entire
Step. `output` is a typed Local. `given` contains facts known at `StepBegin`;
`noted` contains kind-specific facts committed at `StepEnd`. Neither repeats
input, output, status, or error.

Step kinds are:

```text
run | agent | human | model | tool | par | loop | value
```

Model `given` data contains the durable normalized-call references. Large
instructions, messages, and toolsets are content-addressed; provider request
bodies and credentials are not stored. Model `noted` data records continuation
and accounting facts. Preparing or sending a provider request is outside
historical record inspection.

## Resolution And Inspection

Resolution parses the Pointer, loads its one owning record, converts the record
to canonical JSON, and traverses slash tokens. Canonical traversal never
follows a Pointer stored as data. Missing records, missing members, invalid
array indexes, scalar traversal, and explicit `null` are distinct outcomes.

`toolang AGENT inspect POINTER` opens the store read-only. Human output starts
with a dim line containing the selected Pointer and displayed type, then shows
one structural level using relative field suffixes with TYPE in the second
column. Human strings have no JSON quotes, multiline Parts align within VALUE
without a bullet, and resolved rows carry a separate `→` marker. `--json` never
follows a stored Pointer, and `--type` is not an option. Ejected Runs and Steps
remain hidden from ordinary inspection.

## Projection Ownership

Control acceptance is direct durable mutation:

```text
RunExecutor operations    -> RunStore -> ControlRecord
ThreadManager operations  -> RunStore -> ControlRecord
```

Run and Step facts are event projection:

```text
runtime -> RunEvent -> executor persistence -> RunRecord / StepRecord
```

The runtime projects event facts and referenced Control transitions in one
SQLite transaction. Primary keys protect record identities; non-null request
ids are unique, and `BEGIN IMMEDIATE` serializes index allocation across local
processes.

## Compatibility

The current RunStore schema is version 32. It intentionally rejects every
older and newer version before reading or writing. There is no migration or
legacy Pointer parser at this boundary; incompatible stores remain unchanged.
