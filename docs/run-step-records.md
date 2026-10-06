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
run_ab12.0/output/value          FieldRef
run_ab12.0/output/value:Part[]   TypedRef
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
triggered_by
```

`id` is the complete `ControlRef`. Its target determines whether the Control
belongs to a Run or Thread; canonical JSON does not repeat target, index, or a
synthetic scope field. SQLite retains private scope, target, and index columns
for queries.

Current kinds are:

```text
run | retry | exec | chdir | recall | compact | steer | cancel
create | fork | rewind
```

Control status is `pending`, `applied`, `wontapply`, or `revoked`. Timing is
`immediate`, `next_step`, or `next_call`. Private claim and revision columns
support concurrency and polling.

Only external `steer` and `cancel` requests can be pending. Other controls are
persisted as applied with their committed effects and equal creation/finish times.
In particular, an applied `run` control can refer to a still-pending Run.
Step adoption and subsequent Run failure never change that control's outcome.

Run, Exec, Steer, and Cancel payloads store flat `input` objects, keyed by
`_` and argument names. Run entries may also store flat `authored_input` source
text. Retry inherits entry input; rerun creates a new Run entry. Each persisted
value retains its self-describing codec, without a Local/name/dim wrapper.
References address `payload/input/_` or `payload/input/argumentName`.

RunStore schema 52 rejects older SQL schemas before mutation and leaves them
intact for their matching runtime. There is no migration for older SQL schemas.
Control kinds `execute` and `cwd` are renamed to `exec` and `chdir`, with no
old-name aliases. The working-directory payload field remains `cwd`. Update
control filters and consumers to the new kinds; use a fresh store for new runs.

`Output` contains a complete `value: Value | TypedRef | RunHandle` and a
`binding: str | None`. `"_"` is an ordinary binding name for the current local;
`None` leaves the result unbound. `StepRecord.input` remains a list of dependency
references. Executor locals separately retain evaluation and provenance metadata.

```python
output = Output(value=input_value, binding="_")
```

Map/reduce/keep/drop/sort consume only outer array items, using the complete
value type.
An absent output (`None`) differs from `Output(value=None)`, whose value is
JSON null. There is no Local wrapper or shape/dim flag.

Durable and HTTP/event outputs share the `type`, `value`, and `binding` envelope,
for example `{"type": "Text", "value": "result", "binding": "_"}`. Ordinary
durable values retain the self-describing value codec; historical outputs without
an explicit type remain readable. Type is derived from the value or typed
reference. References use `output/value` for the value,
`output` for the complete Output, and `output/binding` for the destination.
Removed wrapper fields and old reference paths have no aliases. Removed Flow
statement records are rejected, and executable snapshots require migrated source
and a newly prepared state.

### Spawn admission and handles

Spawn atomically records a new thread/create control, an independent root/run
control, and the originating Step output. Both controls have `triggered_by` set
to that physical Step; the root's `parent` is null. Entry `spawn_context` stores
inherited settings, workspace bindings, iteration data, and the accepted output
contract; ordinary entries omit it from storage. Resources, limits, model request,
State, and cwd use existing entry fields.

Flow's spawn-kind Step carries a `SpawnStmt`. Its durable and event output is
`{"type": "_Run<Text>", "value": {"id": "run_…", "thread": "spawn_…"}, "binding": "job"}`.
The runtime tag is `_Run<T>` when the target's result type T is known, otherwise
`_Run`. Its value contains identity only; references use the same `output/value`
path as ordinary outputs. The complete accepted result contract, including struct
definitions, belongs to the root's entry `spawn_context`. Status is read from the
Run record. Neither status nor the produced result is stored in the handle.
`_Run<T>` is protocol vocabulary, not an authored language type; user struct names
cannot start with `_`. An authored struct named `Run` remains ordinary data.

Agic's ordinary Tool Step instead stores a `ToolResultPart` whose `output` is
`{id, thread, status: "pending"}`. The tool returns that exact committed snapshot;
reconstructed model history retains it even after the root finishes. This view
is ordinary data and does not become a native handle. The eventual result belongs
to the spawned Run's output and remains inspectable by its ID.

Admission atomically commits the thread, pending root, applied create/run controls,
and succeeded source Step with its output and finish time. Accepted spawn Steps
remain succeeded through interrupted delivery or caller cancellation;
StepEnd carries the persisted status, output, and finish time. Before acceptance, errors or
cancellation create no root. A dispatch failure marks the admitted root failed while
preserving its handle and applied controls. Reprocessing one physical Step returns its original
admission; conflicting requests fail. Recovery does not relaunch a root.
Retry rejects cuts through a surviving root's origin or retained input references;
use rerun instead. Retry after the origin restores handle locals, and rewind can
hide source history without deleting its records or stopping the root.

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

`parent` identifies the enclosing Step for a child Run. Flow `run` and
`_toolang/run` both keep that Step open through the child's `RunEnd`. Independent
roots have no parent; their entry control's `triggered_by` records the source.
`thread`, `control`, and `state` are typed references. `output` is an
Output whose `value` may be concrete or a `TypedRef` to an explicit
`/output/value` field.

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
