# Unified Record Pointers and Inspection

## Goal

Make `Pointer` the single address for durable execution records and their
fields. Inspection resolves a Pointer, optionally focuses the result into a
derived semantic object, and renders the final object as human output or
schema-shaped JSON.

## Success Criteria

- One grammar addresses a Thread, Control, Run, or Step record, or any field in
  its canonical JSON document.
- Pointer text alone determines the record kind; no type prefix is required.
- A field ref uses RFC 6901 JSON Pointer syntax relative to the record.
- Pointer meaning is identical in runtime, inspection, and error handling.
- `--focus` changes the inspected semantic object; `--json` changes only its
  rendering.
- Whole-record JSON matches the canonical record schema without an inspect
  envelope or aggregate presentation fields.
- `too records` exposes the same schemas used by JSON output and field
  traversal.
- Old implicit output/local Pointer semantics are removed without migration or
  compatibility parsing.

## Current Behavior

Pointer currently has context-dependent value semantics:

```text
run_id             RunRecord.output value
run_id.1.2         StepRecord.output value
run_id^0/name      named Local value in control index 0
```

Run and step anchors separately mean errors during error resolution. Inspect
has its own target parser for threads, runs, steps, and model calls, and JSON
output uses inspect-specific detail envelopes rather than exact records.

## Scope

This feature covers Pointer grammar, canonical record JSON, record and field
resolution, explicit runtime value/error Pointers, inspect focus and rendering,
`too records`, CLI help, tests, and an incompatible RunStore schema boundary.

It does not add Pointer mutation, wildcard or query syntax, implicit reference
following, ejected-record audit access, provider request sending, HTTP API
changes, old-store migration, or legacy Pointer support.

## Pointer Syntax

`Pointer` remains the only value type. Record ref and field ref are ordinary
terms used to explain its canonical text; they do not introduce wrapper types,
schemas, or public objects. A record ref identifies one durable record. An
empty field ref selects the whole record; a non-empty field ref selects within
its canonical JSON document.

### Grammar

```text
POINTER     = RECORD_REF FIELD_REF
RECORD_REF  = THREAD_REF | RUN_REF | STEP_REF | CONTROL_REF
THREAD_REF  = THREAD_ID
RUN_REF     = RUN_ID
STEP_REF    = RUN_ID 1*( "." INDEX )
CONTROL_REF = ( THREAD_ID | RUN_ID ) "^" INDEX
FIELD_REF   = "" | 1*( "/" REFERENCE_TOKEN )
```

`RUN_ID` uses the reserved `run_` namespace. Thread IDs must not begin with
`run_`; the two namespaces remain globally disjoint. `INDEX` is `0` or an ASCII
decimal integer beginning with `1`.

```text
term_ab12                         ThreadRecord
run_ab12                          RunRecord
run_ab12.0                        StepRecord
run_ab12.0.1                      StepRecord
run_ab12^1                        ControlRecord
term_ab12^0                       ControlRecord

term_ab12/head/index              ThreadRecord.head.index
run_ab12/status                   RunRecord.status
run_ab12/output/value             RunRecord.output.value
run_ab12.0/given/call             StepRecord.given.call
run_ab12.0/output/value/0         first output value
run_ab12^1/payload/locals/0/value first control Local value
```

The first `/` separates the record ref from the field ref because execution IDs
cannot contain `/`.

### Field refs

A field ref follows RFC 6901:

- empty text selects the whole record;
- `/name` selects an object member and `/items/0` selects an array item;
- `~0` decodes to `~` and `~1` decodes to `/` inside a member name;
- `/` selects a member whose name is empty;
- `-` is not valid for reads;
- array indexes are canonical non-negative integers without leading zeroes.

Traversal never searches a collection by name or follows a reference. Control
locals are selected by immutable list index, not special local-name syntax.

## Canonical Record JSON

Each root uses the durable dataclass's top-level fields:

- `ThreadRecord`: `thread_id`, `origin`, `peer`, `created_by`, `head`, and
  timestamps;
- `RunRecord`: `id`, `parent`, `thread`, `control`, `output`, `occurrence`,
  `status`, `error`, `ejected_by`, and timestamps;
- `StepRecord`: `path`, `kind`, `input`, `given`, `output`, `occurrence`,
  `noted`, `status`, `error`, `ejected_by`, and timestamps;
- `ControlRecord`: `target`, `index`, `kind`, `payload`, `request`,
  `status`, `timing`, `error`, and timestamps.

`ControlRecord` is the single domain model for controls. A run or thread control
ref resolves to the same record shape; `kind` discriminates its payload. There
are no scoped control record types and the shape adds no synthetic scope field.
Scoped payload unions may retain run/thread names because they describe only
the variants accepted by one operation. Nested values use stable protocol
projections rather than SQLite rows or private columns. Pointer fields are
strings, `StepPath` values are strings, `ControlRef` values retain `target` and
`index`, and `Local` values use one documented protocol shape.

A stored model step keeps `ModelCallRefs` in `given.call`; exact StepRecord JSON
does not rebuild prompt blobs. Reconstruction belongs to `model_call` focus.

Record JSON excludes runs embedded in threads, controls or steps embedded in
runs, synthetic child trees, summaries, counts, and inspect metadata. One
record-schema registry owns record serialization, schema-guided field
traversal, JSON Schema generation, and human schema metadata. It has no store
or CLI dependency.

## Resolution

Resolution is context-independent:

1. Parse and validate the complete Pointer.
2. Resolve the record ref through exactly one record lookup.
3. Adapt the record through its canonical schema.
4. Apply the field ref without following references.
5. Return the selected typed value and canonical JSON value.

Missing records, missing members, invalid array indexes, scalar traversal, and
explicit `null` values remain distinguishable.

Runtime `TypedPointer` values validate the selected value after universal
resolution.
Runtime-produced value and error addresses are explicit:

```text
run_ab12/output/value
run_ab12.0/output/value
run_ab12^1/payload/locals/0/value
run_ab12.0/error
```

No root implicitly means output, local, or error.

## Inspection

```text
Pointer? -> focus -> human | JSON
```

### Focus selection

No `--focus` means identity: the selected record or field remains the inspected
object. Initial semantic focuses are:

| Focus | Source | Result |
| --- | --- | --- |
| `model_call` | model `StepRecord` | reconstructed `ModelCall` |
| `model_request` | model `StepRecord` | provider `ModelRequest` |
| `tool_call` | tool `StepRecord` | reconstructed `ToolCall` |

Semantic focuses accept complete records only; combining one with a non-empty
field ref is invalid. `model_request` requires
`--default model=PROVIDER/MODEL_ID`.

Prospective `model_call` and `model_request` omit Pointer and require
`--default runnable=KIND:NAME`; existing prospective input, argument, history,
allow, and model bindings remain available. Omitting both Pointer and a
prospective-capable focus prints help.

### Output

Human is the default. `--json` renders the final focused object and never
changes target selection. JSON contains no inspect envelope.

Human renderers cover all four records, generic fields, `ModelCall`,
`ModelRequest`, and `ToolCall`. Record renderers may include bounded
related-record navigation tables, but that context never enters record JSON.
Prompt text uses verbatim, bounded head-and-tail output; `--full` removes the
human text limit.

### CLI

```text
too alice inspect run_ab12
too alice inspect run_ab12/status
too alice inspect run_ab12.0 --json
too alice inspect run_ab12.0 --focus model_call
too alice inspect run_ab12.0 --focus model_call --json
too alice inspect run_ab12.0 --focus model_request \
  --default model=openai/gpt-5

too alice inspect --focus model_call \
  --default runnable=agic:review
too alice inspect --focus model_request \
  --default runnable=agic:review \
  --default model=openai/gpt-5
```

`model_call@STEP_PATH`, bare `model_call` targets, and `--request` are removed.
Model call and request are focus names, not Pointer roots or renderers.

## Record Schema Discovery

`too records` is a global discovery command alongside `too models` and the
standalone caps surface. It needs no agent, RunStore, program, or provider.

```text
too records
too records --filter thread
too records --filter control
too records --filter run
too records --filter step
too records --filter thread --filter run
too records --json
too records --filter step --json
```

`--filter/-f` is the only selection input; record kinds are not positional
arguments. Records expose the canonical filter identities `thread`, `control`,
`run`, and `step`. Tokenization, repetition, composition, and pattern semantics
come from the shared CLI filter contract and are intentionally not redefined by
this feature; their broader normalization is a separate feature.

Human output shows the record name, record-ref pattern, top-level field refs,
types, nullability, and named unions. Filtering to one record expands its
record-owned Step `given` or Control `payload` variants.

`--json` emits deterministic JSON Schema Draft 2020-12. All-record output is a
bundle of named definitions; selected output is one root schema plus referenced
definitions. The schema must validate the exact output of
`inspect POINTER --json`.

`inspect --help` documents record refs, field refs, focus, human/JSON output,
and points to `too records` for complete schemas.

## Compatibility Boundary

This is an intentional breaking change. There is no migration and no legacy
Pointer parser.

Increment the RunStore schema version and reject every previous-version store
before any read or write, including read-only inspection. Never reinterpret,
rewrite, delete, or partially open old state. Users must explicitly preserve or
remove old state and create a new store.

This boundary prevents an old persisted `run_id` value Pointer from being
silently interpreted as a `RunRecord` Pointer.

## Errors

Parsing distinguishes an invalid record ref from an invalid field ref.
Resolution distinguishes missing record, missing field, invalid array index,
and scalar traversal. Focus errors distinguish an unsupported record, non-empty
field ref, missing runnable, and missing model. Old stores report unsupported
RunStore schema.

CLI syntax and option errors exit 2. Missing data, resolution, focus, and store
errors exit 1. No error path mutates the store.

## Design Touchpoints

- `src/toolang/execution/types.py`: the single Pointer value and its canonical
  grammar parser; do not add wrapper types for record or field refs.
- `src/toolang/execution/records.py`: canonical record and Pointer-bearing
  payload codecs; remove scoped control record types and use one
  `ControlRecord`.
- `src/toolang/execution/schemas.py`: record schemas, traversal metadata, and
  JSON Schema generation without store dependencies.
- `src/toolang/execution/store.py`: exact record lookup, universal resolution,
  explicit runtime value/error selection, and schema rejection without
  migration.
- `src/toolang/execution/history.py`: bounded human navigation context.
- `src/toolang/cli/common/inspection.py`: Pointer and focus parsing.
- `src/toolang/cli/toolang/commands/thread.py`: inspection orchestration and
  renderers; remove model-call targets and `--request`.
- `src/toolang/cli/toolang/commands/records.py`: global schema discovery.
- `src/toolang/cli/toolang/main.py` and routing: command registration and
  prospective focus preparation.
- Tests and CLI documentation: replace old syntax and document the breaking
  boundary.

## Acceptance Tests

1. Round-trip every record root, empty and nested field refs, escaped and empty
   members, and array indexes; reject every non-canonical form.
2. Resolve whole records and representative scalar, object, sequence, null,
   `Local`, Pointer, `ControlRef`, error, payload, given, and noted fields.
3. Assert selected references are not followed implicitly and `TypedPointer`
   validation occurs only after universal resolution.
4. Assert every produced value/error Pointer contains its explicit field ref
   and no implicit form is produced.
5. Reject an old store unchanged in read-only and writable modes; perform no
   migration or deletion.
6. Cover identity focus and human/JSON rendering for every record and field
   category; assert JSON has no inspect envelope or navigation context.
7. Cover historical and prospective `ModelCall` and `ModelRequest`, historical
   `ToolCall`, unsupported sources, missing bindings, field-ref rejection,
   truncation, and `--full`.
8. Reject `model_call@STEP_PATH`, bare `model_call`, and `--request`.
9. Cover `too records` tables, repeated `--filter/-f`, and schemas for all
   record families, and validate emitted record JSON against the generated
   schemas.
10. Verify inspect help and run the complete default offline verification.

## Risks

- Reject old stores before opening them to prevent silent Pointer
  reinterpretation.
- Generate codecs, traversal metadata, and JSON Schema from one registry to
  prevent record-shape drift.
- Apply existing `TypedPointer` validation after resolution to preserve runtime
  type safety.
- Keep related-record data in human renderer context so machine output remains
  exact.
- Keep focus names identity-free so they cannot become a second target grammar.

## Decisions

- Pointer text consists of a record ref and optional field ref; neither term is
  a separate type.
- Field-ref syntax is RFC 6901 relative to canonical record JSON.
- Record kind is inferred from root syntax; type prefixes are forbidden.
- Roots never imply output, local, or error fields.
- Identity is the default focus; semantic focus is separate from rendering.
- Human is default and `--json` is the only output-mode switch.
- `too records` is the global schema-discovery command.
- Old semantics are removed with no migration or compatibility layer; old
  RunStore schemas are rejected unchanged.
- The definition has no open questions and requires human approval before
  implementation.
