# Define Execution Value Provenance

## Goal

Represent accepted run arguments, step dependencies, and run/step results with
typed locals and immutable value pointers. Durable records must preserve the
adopted value type, execution dimension, source control, and transformation
boundaries without copying values between controls, child runs, and steps.

## Success Criteria

- Language-owned values use `T` for an arbitrary type and `S` for an authored
  Toolang `struct`; execution modules do not leak into `toolang.lang`.
- Authored struct names do not collide with built-in scalar, Json, Part, or
  concrete Part runtime types, so every stored type identifies one variant.
- Every accepted run preparation stores resolved resources, limits, runnable,
  model, and locals in one typed control payload.
- A run points to its current preparation control instead of duplicating an
  input, while a step records only pointers to values available at step start.
- Concrete values are stored once. Aliases, child arguments, selection,
  ordering, and dimension changes use immutable value pointers.
- Model, tool, scatter, keep, map, rank, and repeat records use the canonical
  local shapes defined below.
- Steer timing is applied at explicit model boundaries, including valid model
  history when outstanding tool calls are skipped.
- New control and execution-record schemas round-trip through SQLite, events,
  APIs, and inspection, and the default verification suite passes.

## Scope

This feature covers language value vocabulary, executor locals and pointers,
typed control payloads, the unified controls table, run and step records,
events, SQLite codecs, projections, retry/rerun/child derivation, steer timing,
repeat locals, and acceptance tests.

It does not type all `given` and `noted` variants, rename the public
`RunControlRecord` and `ThreadControlRecord` concepts, redesign resource
selection, or migrate legacy control and record tables. Caller-facing request
fields remain `request_id`; durable storage uses `request`.

## Values And Locals

The language package owns `Text`, `Number`, `Boolean`, `Json`, `Struct`,
`Array`, `Part`, and `Value`. Type descriptions use `T` for an arbitrary type
and `S` for a declared Toolang `struct`. `Struct` is an immutable Mapping-like
runtime object, and `Array` is an immutable Sequence-like runtime object that
retains its complete `T[]` type. `ToolCallPart` and `ToolResultPart` are concrete
`Part` subtypes alongside `TextPart`, `ImagePart`, `AudioPart`, and
`DocumentPart`. `Json` is the unknown type; Json scalars normalize to native
Text, Number, or Boolean values, while a containing Struct or Array retains its
declared boundary.

Execution owns these immutable types:

```python
@dataclass(frozen=True, slots=True)
class Pointer:
    value: str


@dataclass(frozen=True, slots=True)
class TypedPointer:
    type: str
    pointer: Pointer


@dataclass(frozen=True, slots=True)
class Local:
    value: Value | TypedPointer
    name: str | None = None
    dim: Literal[0, 1] = 0
```

`Local.type` is derived from its concrete runtime value or TypedPointer rather
than stored as an independent field. `Local.typed(type, value, ...)` applies an
explicit boundary when adapting executor values. `dim=0` treats the complete
value as one item. `dim=1` requires a type ending in `[]` and treats that outer
Array as the execution collection; its item type is obtained by removing the
rightmost `[]`. Thus `Part[]/dim=0` is one model response, `Part[]/dim=1` is a
collection of parts, and `Part[][]/dim=1` is a collection whose items are each
`Part[]`.

`Pointer` contains only the record address. `TypedPointer` combines the
expected result type with that address whenever a pointer occupies a value
position. A Local value may be concrete, one TypedPointer, or an Array/Struct
containing concrete values and TypedPointers.

The private durable Local object contains exactly `value`, `name`, and `dim`.
Its value is self-describing: Text/Number/Boolean use raw scalar shortcuts,
inline objects use `{"?": "T", ...}`, boxed values use
`{"?": "T!", "!": value}`, and pointers use `{"?": "T@pointer"}`.
For example, `Part@run_1.0/2` is the tag value for a pointer expected to resolve
to Part. `?` and `!` are reserved with no literal escape. These tags are private
to durable records. Events and APIs retain their existing Local projection
with `type`, `value`, `name`, and `dim`, their existing `$ptr` projection, and
the ordinary `type: text|image|audio|document|tool_call|tool_result` Part
representation. When an open Json or Struct field contains a typed Array,
Struct, Part, or pointer whose type cannot be inferred from the enclosing
Local, the protocol uses a readable `{"$type": "T", "$value": ...}` or
`{"$type": "T", "$ptr": "..."}` wrapper. Those two exact `$type` shapes
are reserved at protocol boundaries. Fields whose schema accepts only pointers
store canonical Pointer strings directly.

Control locals must have unique names. `_` is the primary argument. A step or
run output may use `name=None` to produce a value without updating the runtime
local table.

## Pointers

Canonical anchors are:

```text
run_id             RunRecord.output
run_id.1.2         StepRecord.output
run_id^0/name      named Local in control index 0
```

Step paths use dots. A trailing RFC 6901 JSON Pointer addresses the resolved
semantic value, for example `run_id.1/0` for the first output item and
`run_id^0/_/1` for the second primary control item. A control anchor must name
a local; bare `run_id^0` is invalid. Run and thread IDs must match
`[A-Za-z0-9][A-Za-z0-9_-]*`; pointer syntax does not define identifier
validity.

In value contexts, run and step anchors resolve their output and control
anchors resolve the named local. In error contexts, run and step anchors
resolve their error and control anchors are invalid. Pointers never address a
step input. Value and error resolution use separate entry points so the
pointer syntax needs no durable field selector.

## Controls

One `controls` table stores both run and thread controls. Its primary key is
`(target, index)`; run and thread IDs must therefore be globally disjoint. The
table stores `scope`, `target`, `index`, `kind`, `request`, `status`, `timing`,
`error`, `payload`, timestamps, `_claimed`, and `_revision`. `scope` is derived
from kind and is not part of the primary key. `request` is globally unique when
present. `_claimed` coordinates apply versus revoke; `_revision` is the
monotonic polling cursor for visible changes.

The common record fields are `target`, `index`, `kind`, `payload`, `request`,
`status`, `timing`, `error`, `created_at`, and `finished_at`. Timing defaults to
`immediate`; statuses are `pending`, `applied`, `wontapply`, and `revoked`.
`ControlKind` names the complete run and thread control kind union. Caller-facing
control projections use `ControlInfo`, not a run-only vocabulary.

Preparation payloads use this order:

```text
resources, limits, runnable, model, locals, rerun_from/retry_from
```

Start stores the first five values. Rerun adds `rerun_from`; retry adds
`retry_from` and permits `locals=None` for inheritance while `()` means an
explicit empty argument set. Runnable and model are non-empty concrete values
resolved from defaults and overrides before acceptance. `RunBindings` is
flattened into `runnable` and `model`. Agent ceilings and state fingerprints
are not durable run truth.

This implementation may copy adopted locals into a retry payload. When it
does, those locals belong to the new retry control and subsequent execution
points to that control; earlier controls remain immutable. Avoiding the retry
copy is a later storage optimization, not a reason to point new execution at
control index zero.

Steer and stop payloads contain only locals. Executor validation requires steer
to provide a concrete primary `Part[]` and stop to provide no local or one
concrete primary `Text`. Create has an empty payload. Fork stores `fork_from`
and `fork_at`. Rewind stores `rewind_from` and `rewind_if`; `rewind_if` is an
optimistic check against the target thread's current control head index.

## Run And Step Records

`RunRecord.control` references the current preparation control. Start, child
start, and rerun-created runs point to their accepted control; retry updates the
same run to its newest retry control. Steer and stop do not change this field.
The run does not duplicate input data: its arguments are the referenced
payload locals. `RunBegin` also uses `control`; `RunEnd` has no input field.

Run records contain identity, parent, thread, control, one optional Local
output, placement, status, error, `ejected_by`, and timestamps. They do not
store context, root, or attempt. `parent is None` defines a root.

Step records contain path, kind, `input: tuple[Pointer, ...]`, one optional
Local output, placement, the currently open `given` and `noted` mappings,
status, error, `ejected_by`, and timestamps. Input lists the durable values
actually read by the step at `StepBegin`; it is not a snapshot of every local,
is not a complete invocation, and does not repeat Local metadata. Exact model
requests remain in `given.call`.
Values created inside the step are represented by nested records and the final
output pointer.

Placement is shared by runs and steps and may contain `item`, `items`, `lane`,
`lanes`, `iter`, and `iters`. `iter=-1` identifies a repeat control check.
`ejected_by` is a control reference. Errors are a direct string or a Pointer
to a run/step error.

## Execution Semantics

The executor maintains one local table per flow. Controls establish locals;
step outputs update the binding named by their Local. Consumers point to the
most recent Local that established the relevant type and dimension instead of
collapsing the pointer chain to the original concrete value.

Canonical outputs are:

- model: `Local.typed("Part[]", parts, name="_", dim=0)`;
- tool: input points to one `ToolCallPart`, output is
  `Local.typed("ToolResultPart", ..., name=None, dim=0)`;
- scatter: a pointer to one `Part[]` with `dim=1`, without copying parts;
- keep/rank: `Local.typed("T[]", [pointers...], ..., dim=1)`;
- map/parallel: child control locals point to source items, and the parent
  collection points to child run outputs.

Repeat body steps update the current flow local table directly. Primary and
named bindings survive iterations and remain visible after repeat. The repeat
wrapper is structural and has no output. Zero iterations leave locals
unchanged. Known counts populate `iter` and `iters`; an until result uses
`name=None` and `iter=-1` because it affects control flow rather than locals.

Steer applies only to agic runs, whether root or child. `immediate` interrupts
the active step and starts a model step. `next_step` waits for the active step
to finish and replaces the normally scheduled next step with a model step.
When this happens before a tool batch starts, all outstanding tool calls are
skipped. `next_call` waits for the normal next model boundary. Before a
replacement model call, every skipped call receives a synthetic canceled
`ToolResultPart` in the exact `given.call`; no tool step is created for a call
that never started. The model step input points to the skipped ToolCallParts
and steer control locals, and its durable StepBegin atomically marks the steer
applied.

For this change, a tool batch is committed once its first tool step begins. A
`next_step` steer accepted during that batch waits for the normal model boundary
after the batch; interrupting the remaining calls is deferred.

Claims are exclusive: only a pending control whose `_claimed` value is false
may be claimed, and concurrent claimers must observe at most one winner.

Error pointers are resolved through the store across run and step records for
caller-facing summaries. A parent step points to a failed child run instead of
copying the child's runtime error. Ejection scope is read from the referenced
control record and is never inferred from an identifier prefix.

## Implementation Touchpoints

- `src/toolang/lang/input.py` and a language-owned value vocabulary module;
- `src/toolang/execution/types.py`, `records.py`, `events.py`, `schemas.py`, and
  `store.py`;
- `src/toolang/execution/executor`, including model/tool steps, flow statements,
  child preparation, retry, steer, and repeat;
- execution, store, schema, API, inspection, CLI, and integration tests.

## Acceptance Tests

- Local, Pointer, and TypedPointer codecs round-trip scalar, Struct, every Part
  variant, nested Array, mixed-pointer collection, heterogeneous Json, and
  invalid type/dim cases; private value tags never leak through events or APIs.
- Root, child, rerun, and repeated retry records point to the correct control;
  when retry copies adopted locals, the new copies belong to the retry control.
- Initial model steps point only to the primary and named control locals they
  actually read, including the current retry control rather than control zero.
- Model part pointers address ToolCallParts; tool results and later model calls
  preserve valid history.
- Scatter changes only dimension; keep and rank select or reorder by pointer;
  map and parallel outputs point to child values.
- Repeat updates primary and named locals across iterations, records no wrapper
  output, preserves zero-iteration state, and reconstructs visible locals.
- All steer timings work for root and child agics, flow steer is rejected,
  next-step steering skips a not-yet-started tool batch, and steering during an
  active batch waits for its normal model boundary.
- Unified control status transitions, request uniqueness, claim/revoke races,
  exclusive claim races, revisions, fork, and rewind compare-and-swap behavior
  are covered.
- Control records and caller-facing `ControlInfo` values use outer `kind` to
  round-trip the concrete payload variant.
- Runtime error chains resolve across child runs without copying their messages,
  and ejection projections preserve the referenced control scope.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest` pass.

## Risks

- This is an intentional durable-schema break; all repository consumers must
  update atomically and existing databases are not migrated.
- Pointer resolution must reject cycles, missing records, type mismatches, and
  invalid JSON paths without partially updating projections.
- Retry and repeat reconstruction depend on append-only ordering and the newest
  non-ejected Local for each binding.
- Model adapters require complete tool-call/result pairing even when steering
  skips execution; synthetic results must be deterministic and provider-neutral.

## Open Questions

None.
