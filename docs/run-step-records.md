# Execution Records And References

Execution records are the durable source of truth. Run events are their
transient projection input.

## References

Durable references have explicit types:

```text
RecordRef = ThreadRef | RunRef | StepRef | ControlRef | ContentRef
FieldRef  = RecordRef + JsonPointer
TypedRef  = FieldRef + RuntimeType
```

`StepPath` is only the Run-relative tuple of Step indexes. `StepRef` combines it
with a `RunRef`. `Pointer` is the generic facade used where the concrete Ref
kind is not known before parsing, such as CLI inspection.

```text
term_ab12                              ThreadRef
run_ab12                               RunRef
run_ab12.0.1                           StepRef
term_ab12@0                            ControlRef
run_ab12@1                             ControlRef
sha256_<64 lowercase hex digits>       ContentRef
run_ab12.0/output/value                FieldRef
run_ab12.0/output/value:Part[]         TypedRef
```

Run IDs reserve `run_`; content IDs reserve `sha256_`; Thread IDs may use
neither prefix. Step and Control indexes use canonical non-negative decimal
text. Field tokens use RFC 6901 escaping. `:` is reserved for the TypedRef
suffix; durable record field names are assumed not to contain it. Legacy forms
are rejected.

## Canonical Records

Canonical JSON contains the record itself, without an inspection envelope,
embedded children, summaries, or SQLite-only columns. Every record exposes its
canonical identity as `id: str`. Fields referring elsewhere use their concrete
Ref type and serialize to canonical text.

### ThreadRecord

```text
id
origin
peer
created_by
head
created_at
updated_at
```

`created_by` identifies the successful create or fork Control at index zero.
`head` identifies the latest successful thread Control and supports optimistic
concurrency for rewind and fork operations.

### ControlRecord

```text
id
kind
payload
request
status
timing
error
created_at
finished_at
```

`id` is the complete `ControlRef`. Its target determines whether the Control
belongs to a Run or Thread; canonical JSON does not repeat target, index, or a
synthetic scope field. SQLite retains private scope, target, and index columns
for queries.

Current kinds are:

```text
run | rerun | retry | reload | execute | steer | cancel
create | fork | rewind
```

Control status is `pending`, `applied`, `wontapply`, or `revoked`. Timing is
`immediate`, `next_step`, or `next_call`. Private claim and revision columns
support concurrency and polling.

### RunRecord

```text
id
parent
thread
control
state
output
occur
status
error
created_at
started_at
finished_at
```

`parent` is the calling `StepRef` for a child Run. `thread`, `control`, and `state`
are typed references. `output` is a Local and may contain a
`TypedRef` to an explicit `/output/value` field.

### StepRecord

```text
id
kind
input
given
state
output
occur
noted
status
error
created_at
started_at
finished_at
```

`id` is the complete `StepRef`. `input` is an ordered tuple of `FieldRef`s.
`state` identifies the immutable State Control used for the Step. `given`
contains facts known at `StepBegin`; `noted` contains kind-specific facts
committed at `StepEnd`. Neither repeats input, output, status, or error.

Model `given` data contains normalized-call references. Large instructions,
messages, and toolsets are content-addressed; provider request bodies and
credentials are not stored. Model `noted` records continuation and accounting.

## Content And Errors

The `contents` table stores `(id, value)` only. `id` is the complete
`ContentRef`, `sha256_<digest>`, and `value` is the raw blob. Writes and reads
verify the digest, and identical bytes deduplicate. The referring field supplies
the text, JSON, or file codec.

Run and Step errors use exactly one of:

```json
null
{"type": "message", "message": "model request timed out"}
{"type": "ref", "ref": "run_ab12.0/error"}
```

An `ErrorRef` can target only `/error` on a Run or Step. Resolution follows the
chain to an `ErrorMessage` and rejects missing targets, null targets, and cycles.

## Resolution And Inspection

Resolution parses a Pointer, fetches its owning record by canonical ID, converts
the record to canonical JSON, and traverses slash tokens. It never follows a Ref
stored as data unless the caller explicitly requests value or error resolution.
Missing records, missing members, invalid array indexes, scalar traversal, and
explicit `null` remain distinct outcomes.

`toolang AGENT inspect POINTER` opens the store read-only. Human output shows
one structural level; `--json` returns canonical JSON without following stored
Refs. Physical Runs and Steps remain inspectable after Thread rewind; only
Thread-selected collections apply logical history membership. Retry physically
deletes its invalid Steps and child Runs.

Ownership can be inspected without constructing an execution tree:

```text
THREAD runs
  -> RUN steps
       -> STEP runs
       -> LOOP_STEP steps
```

`inspect RUN tree` produces a transactionally consistent structural projection.
`inspect STEP call` exposes normalized model or tool calls and structural calls
for run, par, and loop Steps. These projections are not persisted event journals
or exact timelines.

## Persistence

SQLite uses canonical primary keys:

```text
threads.id
runs.id
steps.id
controls.id
contents.id
```

Private indexed columns may retain a Step's Run and relative path or a Control's
scope, target, and index. Record selection still uses the canonical ID.
All other reference-bearing columns store the complete canonical Ref string.
`BEGIN IMMEDIATE` serializes local index allocation and related mutations.

The current RunStore schema is version 36. Every older or newer version is
rejected before reading or writing. There is no migration or legacy reference
parser at this boundary, and incompatible stores remain unchanged.
