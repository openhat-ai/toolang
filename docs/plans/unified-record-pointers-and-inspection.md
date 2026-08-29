# Unified Record Pointers and Historical Inspection

## Goal

Make `Pointer` the single address for durable execution records and their
fields. `inspect` becomes a read-only historical browser that resolves one
Pointer and displays its value, canonical JSON, or declared type.

## Success Criteria

- One Pointer grammar addresses a Thread, Control, Run, or Step record, or any
  field in its canonical JSON document.
- Pointer text alone determines the record kind; no type prefix is required.
- The only user-facing addressing terms are record, field, and Pointer.
- `TypedPointer` has the canonical `POINTER:TYPE` form.
- Field names cannot contain `:`; the character is reserved for the
  `TypedPointer` suffix.
- Inspection requires an existing historical Pointer and never prepares future
  work.
- Human output supports progressive, one-level traversal with complete,
  copyable Pointers.
- Human output resolves Pointer-valued fields, marks the displayed field with
  `→`, and renders the resolved value through a type-owned human renderer.
- `--human`, `--json`, and `--type` are the complete, mutually exclusive display
  modes; human is the default.
- `--type` prints only the current code-level name of the selected declared
  type; it does not expose that type's definition or fields.
- Whole-record JSON matches the canonical record shape without an inspect
  envelope or aggregate presentation fields.
- Record and value-type evolution is absorbed by shared schema metadata and
  generic fallbacks rather than record-specific inspect code.
- Old implicit output/local Pointer semantics are removed without migration or
  compatibility parsing.

## Current Behavior

Pointer currently has context-dependent value semantics:

```text
run_id             RunRecord.output value
run_id.1.2         StepRecord.output value
run_id^0/name      named Local value in control index 0
```

A whole Control record cannot be addressed. Run and Step roots separately mean
errors during error resolution. `TypedPointer` combines type and Pointer with
`TYPE@POINTER`, overloading the same separator selected for the new Control
syntax. `inspect` has its own thread/run/step parser, supports aggregate thread
output through `--limit`, and emits inspect-specific documents instead of
exact records.

The RunStore schema is version 31 on this definition's base. Main also has
separate `RunControlRecord` and `ThreadControlRecord` classes even though both
share one durable table and the same top-level field shape.

## Scope

This feature covers Pointer and `TypedPointer` grammar, canonical record JSON,
one public `ControlRecord`, record and field resolution, explicit runtime
value/error Pointers, historical inspection, human summaries, type display,
CLI help, tests, and an incompatible RunStore schema boundary.

It does not add Pointer mutation, wildcard or query syntax, Pointer following
to canonical JSON or type display, ejected-record audit access, prospective
model-call preparation, provider request construction or sending, semantic
focus, HTTP API changes, old-store migration, legacy Pointer support, or a
separate record-schema command.

Future model-call preview and sending belong to one separate command and are
not defined here.

## Vocabulary

The user-facing vocabulary has three terms:

- **Record**: one durable Thread, Control, Run, or Step object.
- **Field**: one member or array item inside a record's canonical JSON.
- **Pointer**: the complete address of a record or field.

Do not introduce generic terms such as record ref, field ref, reference,
record path, field path, or inspect target. `StepPath`, `ControlRef`, and
`TypedPointer` remain because they name persisted domain values; they are not
generic inspection-address terms.

## Pointer Syntax

`Pointer` remains the only public address value. Its complete grammar is:

```text
POINTER = THREAD_ID [ "@" INDEX ] *( "/" TOKEN )
        | RUN_ID [ 1*( "." INDEX ) | "@" INDEX ] *( "/" TOKEN )
```

`RUN_ID` uses the reserved `run_` namespace. Thread IDs must not begin with
`run_`; the namespaces remain globally disjoint. `INDEX` is `0` or an ASCII
decimal integer beginning with `1`.

The forms are:

```text
term_ab12                         Thread record
run_ab12                          Run record
run_ab12.0                        Step record
run_ab12.0.1                      nested Step record
term_ab12@0                       Control record
run_ab12@1                        Control record

term_ab12/head/index              field
run_ab12/status                   field
run_ab12/output/value             field
run_ab12.0/given/call             field
run_ab12.0/output/value/0         field
run_ab12@1/payload/locals/0/value field
```

`.` expresses Step hierarchy, `@` selects a Control index attached to a Thread
or Run, and `/` enters a field. A Control cannot target a Step. Execution IDs
cannot contain `.`, `@`, or `/` in positions that would make these forms
ambiguous.

Slash segments follow RFC 6901:

- `/name` selects an object member and `/items/0` selects an array item;
- `~0` decodes to `~` and `~1` decodes to `/` inside a member name;
- `/` selects a member whose name is empty;
- `-` is not valid for reads;
- array indexes are canonical non-negative integers without leading zeroes;
- a member token containing `:` is invalid because field names cannot contain
  the reserved character.

Canonical slash traversal never searches a collection by name or follows a
stored Pointer. Control locals are selected by immutable list index.

### Typed pointers

`TypedPointer` is a Pointer paired with the type expected by runtime
resolution:

```text
TYPED_POINTER = POINTER ":" TYPE
```

Examples:

```text
run_ab12.0/output/value:Text
run_ab12/output/value:Text[]
run_ab12@1/payload/locals/0/value:Json
```

Neither `POINTER` nor `TYPE` can contain `:`, so a canonical `TypedPointer`
contains exactly one colon. `TYPE` uses the existing runtime type grammar. `@`
occurs only inside the Pointer and only selects a Control. Canonical protocol
and durable projections use the same `POINTER:TYPE` text; the old
`TYPE@POINTER` form has no compatibility parser.

`inspect` accepts a Pointer, not a `TypedPointer`. Canonical traversal treats a
`TypedPointer` inside a record as data. Human display may resolve it under the
explicit rules below.

## Canonical Record JSON

Each record uses its durable domain fields:

- `ThreadRecord`: `thread_id`, `origin`, `peer`, `created_by`, `head`, and
  timestamps;
- `RunRecord`: `id`, `parent`, `thread`, `control`, `state`, `output`,
  `occurrence`, `status`, `error`, `ejected_by`, and timestamps;
- `StepRecord`: `path`, `kind`, `input`, `given`, `state`, `output`,
  `occurrence`, `noted`, `status`, `error`, `ejected_by`, and timestamps;
- `ControlRecord`: `target`, `index`, `kind`, `payload`, `request`, `status`,
  `timing`, `error`, and timestamps.

`ControlRecord` is the single public and internal durable control model. Its
`kind` discriminates all current run- and thread-scoped payloads, including
`run`, `rerun`, `retry`, `reload`, `execute`, `steer`, `cancel`, `create`,
`fork`, and `rewind`. Scoped payload unions may retain names when they express
real payload subsets; they do not require separate record classes or schemas.
The public shape adds no synthetic scope field.

Nested values use stable protocol projections rather than SQLite rows or
private columns. Pointer fields are strings, `TypedPointer` values use
`POINTER:TYPE`, `StepPath` values are strings, `ControlRef` values retain
`target` and `index`, and `Local` values use one documented protocol shape.
Every object member name in canonical record JSON, including names inside
user-provided JSON and structured values, must reject `:` when the value enters
durable execution state. String values may contain colons.

Record JSON excludes runs embedded in threads, controls or steps embedded in
runs, synthetic child trees, summaries, counts, and inspect metadata. One
record-schema registry owns record serialization, schema-guided traversal, and
type metadata without store or CLI dependencies. No `too records` command is
added.

## Resolution

Resolution is context-independent:

1. Parse and validate the complete Pointer.
2. Resolve its record through exactly one record lookup.
3. Adapt the record to canonical JSON and its schema.
4. Traverse slash segments without following stored Pointer values.
5. Return the selected historical value and declared schema location.

Missing records, missing members, invalid array indexes, scalar traversal, and
explicit `null` values remain distinguishable.

These steps never follow a Pointer stored as the selected value. JSON and type
display stop here. Human display performs its separate, visibly marked
resolution after canonical selection.

Runtime `TypedPointer` values validate their expected type only after universal
Pointer resolution. Runtime-produced value and error Pointers are explicit:

```text
run_ab12/output/value
run_ab12.0/output/value
run_ab12@1/payload/locals/0/value
run_ab12.0/error
```

No record Pointer implicitly means output, local, or error.

## Historical Inspection

The complete command is:

```text
too AGENT inspect POINTER [--human | --json | --type]
```

`POINTER` is required. Omitting it prints help. The three display flags are
mutually exclusive, and omitting all three selects `--human`.

Inspection only opens historical execution state. It does not load or resolve
a runnable, prepare a Step, reconstruct a future model call, apply input or
policy overrides, create a Run, construct a provider request, or send data.
The command has no `--focus`, `--full`, `--limit`, `--input`, `--arg`,
`--thread`, `--allow`, `--default`, `--request`, or `--send` options.

### Human display

Human display is schema-directed and does not show type names. It uses the
generic record browser for structure and a human renderer registry for values.

For a canonical object or array, it lists exactly the direct children. Compact
values use two columns:

```text
POINTER                     VALUE
run_ab12/status             "succeeded"
run_ab12/control            {target: "run_ab12", index: 0}
run_ab12/output             {name: "_", value: {...}}
run_ab12/error              null
```

The Pointer token at the start of every first-column value is complete and can
be copied unchanged into the next `inspect` invocation. Pointers are plain text
and are never shortened, truncated, or split by styling. Member names use their
RFC 6901 escaped form; array children use their canonical numeric indexes. The
optional `→` is rendered after a separating space outside the Pointer text. It
is a human marker, is not accepted by the Pointer parser, and means that the
shown value came from resolving a Pointer-valued field.

For example, inspecting a Step's input array renders its Pointer-valued direct
children after resolution:

```text
POINTER                     VALUE
run_ab12.0/input/0 →         Review the changes
```

Human resolution follows these rules:

- schema metadata, not string shape, decides whether a field contains
  `Pointer` or `TypedPointer`;
- both forms resolve repeatedly until they reach a non-Pointer value;
- every `TypedPointer` validates its expected type after resolving its Pointer;
- one `→` marks the source field even when resolution crosses multiple links;
- a visited set detects cycles and reports the complete cycle;
- a missing target, hidden/ejected target, cycle, or type mismatch is a data
  error and exits 1;
- JSON and type display never perform this resolution.

Human Pointer resolution applies to the selected value and to each direct child
being printed. It does not recursively resolve Pointer values hidden inside a
container summary.

Resolution changes the value sent to the human renderer, not the canonical
field. It does not invent child Pointers below the source field. The raw Pointer
text remains available through `--json`; inspecting that target directly is the
way to traverse fields inside a resolved container.

Human renderer registration is optional and additive: an owning type package
may provide a more natural display for its resolved declared or runtime type,
and inspect otherwise uses the generic fallback. The inspect command contains
no type switch. A new record, field, or value type is therefore inspectable
without adding an inspect branch. Initial specialized behavior is:

- `Part` and `Part[]` use the same deterministic, non-live content rendering as
  Chat UI responses, including Markdown text and the existing structured-Part
  fallback;
- `Text` renders as natural text, with multiline content preserved;
- `Number`, `Boolean`, and `null` render as compact literals;
- `Local` delegates to the renderer for its contained runtime value, while its
  metadata remains available through JSON or deeper inspection;
- `ExecutionError` renders its resolved human message;
- records, `Json`, structured values, and unknown types use the generic object,
  array, or scalar fallback.

A block renderer prints the Pointer heading, including `→` when applicable,
and places the natural value below it instead of forcing content into a table
cell. For example, a resolved `Part[]` uses the Chat UI response form:

```text
run_ab12.0/output/value →

• **Review complete**

  Two issues remain in the parser.
```

The generic fallback follows these rules:

- scalars use JSON literals;
- an object uses a deterministic one-line `{key: value}` summary of its direct
  members in canonical order;
- nested objects appear as `{...}` and arrays as `[N items]`;
- arrays use `[N items]` as their parent summary and each item gets its own
  Pointer and value summary when the array is inspected;
- empty containers appear as `{}` and `[]`;
- `null` remains visible;
- only the summary may be truncated from the right with `...`; its Pointer is
  never truncated.

Selecting a scalar prints its complete value without truncation. A multiline
string prints the Pointer followed by the natural string on subsequent lines.
Progressive traversal replaces a separate full-output option.

### JSON display

`--json` emits the exact canonical JSON selected by the Pointer. It adds no
envelope, type, summary, navigation context, or terminal formatting. For every
valid slash suffix `FIELD`:

```text
inspect RECORD/FIELD --json
=
RFC6901(inspect RECORD --json, /FIELD)
```

### Type display

`--type` prints the current code-level name of the declared type at the selected
Pointer and no historical value. It emits one plain-text name; it does not echo
the selected Pointer or add a table, type definition, child fields, union
branch expansion, or terminal styling. For example:

```text
too alice inspect run_ab12 --type
RunRecord

too alice inspect run_ab12.0/input --type
Pointer[]
```

The name comes directly from the schema descriptor owned beside the current
record or value type; inspect does not maintain a parallel type-name map. Named
domain types retain their code names, container and union syntax is generated
from their current declaration, and anonymous JSON values use the current
schema descriptor's ordinary name. Nullable and union declarations are not
narrowed from the historical value. The exact name is diagnostic output, not a
stable serialized contract, and may change when the code declaration changes.
This intentionally does not replace full schema documentation.

### Examples

```text
too alice inspect term_ab12
too alice inspect run_ab12/status
too alice inspect run_ab12.0
too alice inspect run_ab12@1/payload
too alice inspect run_ab12.0/output --human
too alice inspect run_ab12.0/output --json
too alice inspect run_ab12.0/output --type
```

## Compatibility Boundary

This is an intentional breaking change. There is no migration and no legacy
Pointer or `TypedPointer` parser.

Increment the current RunStore schema version at implementation time and
support only that new version. Version 31 and every other previous-version
store must be rejected before any read or write, including read-only
inspection. Never reinterpret, rewrite, delete, or partially open old state.
Users must explicitly preserve or remove old state and create a new store.

This boundary prevents an old persisted `run_id` value Pointer from being
silently interpreted as a `RunRecord` Pointer and prevents old
`TYPE@POINTER` text from colliding with Control syntax.

## Errors

Parsing reports one invalid Pointer without introducing separate record-ref or
field-ref terminology. Resolution distinguishes missing record, missing field,
invalid array index, and scalar traversal. Human Pointer resolution also
distinguishes a missing or hidden target, a Pointer cycle, and a `TypedPointer`
mismatch.

Type display reports missing or inconsistent schema metadata as an
implementation error. Old stores report an unsupported RunStore schema.

CLI syntax and option errors exit 2. Missing historical data, resolution, and
store errors exit 1. No inspection or error path mutates the store.

## Design Touchpoints

- `src/toolang/execution/types.py`: the single Pointer value, `@` Control
  syntax, `POINTER:TYPE` parsing, slash-segment parsing, and explicit Pointer
  construction.
- `src/toolang/execution/records.py`: canonical record, Pointer, and
  `TypedPointer` payload codecs; replace run/thread control record classes with
  one `ControlRecord` while retaining meaningful payload subsets.
- `src/toolang/execution/schemas.py`: canonical record serialization,
  traversal, and code-owned declared type descriptors without store or CLI
  dependencies. Inspect discovers record fields and type names through these
  descriptors rather than duplicating them.
- `src/toolang/execution/store.py`: exact record lookup, universal resolution,
  explicit runtime value/error selection, cycle-safe Human Pointer resolution,
  and schema rejection without migration.
- `src/toolang/execution/executor` and current run/control clients: replace all
  implicit Pointer producers with explicit field Pointers without reverting
  newer RunClient, Agent State, reload, execute, or runtime-tool behavior.
- `src/toolang/cli/toolang/commands/thread.py`: replace aggregate inspection
  with one historical Pointer command, generic record traversal, optional human
  renderer dispatch, and the three display modes. It must not switch on record
  classes or enumerate record fields.
- `src/toolang/cli/toolang/commands/chat/blocks.py` and a shared CLI rendering
  helper: factor the deterministic, non-live `Part`/`Part[]` presentation so
  Chat UI and inspect use one implementation without changing live Chat
  behavior.
- `src/toolang/cli/toolang/main.py` and routing: retain only resident/visiting
  historical store routing; do not prepare prospective work or register a
  `records` command.
- Tests and CLI documentation: replace old syntax, cover progressive traversal,
  and document the breaking boundary.

## Acceptance Tests

1. Parse every record form, nested slash segment, escaped and empty member, and
   canonical array index; reject ambiguous and non-canonical forms.
2. Parse `POINTER:TYPE`, including array types, require exactly one colon, and
   reject colon-bearing field names and the old `TYPE@POINTER` form without
   reinterpretation.
3. Resolve whole Thread, Control, Run, and Step records and representative
   scalar, object, array, null, `Local`, Pointer, `TypedPointer`, `ControlRef`,
   error, payload, `given`, state, and noted fields.
4. Serialize every record and every current Control payload variant to its
   exact canonical JSON shape.
5. Assert canonical selection and JSON/type display never follow selected
   Pointer values; Human display follows `Pointer` and `TypedPointer` chains
   based on schema metadata and validates each typed expectation after
   resolution.
6. Assert every runtime-produced value/error Pointer names its explicit field
   and uses `@` for Controls; no implicit or `^` form is produced.
7. Assert human object and array output lists one level, includes nulls, omits
   types, summarizes containers deterministically, and emits complete copyable
   Pointer tokens with correct RFC 6901 escaping.
8. Assert Human Pointer resolution appends exactly one separate `→` marker,
   leaves the source Pointer and canonical field unchanged, and reports missing,
   hidden, cyclic, and type-mismatched Pointer values without mutation.
9. Assert `Part` and `Part[]` reuse the deterministic Chat UI response renderer;
   cover natural text, literals, `Local`, `ExecutionError`, and the generic
   fallback for an unregistered structured type.
10. Assert selected scalar human output is complete and multiline strings
    retain their natural multiline form.
11. Assert JSON output has no envelope and every field result equals RFC 6901
   traversal over the whole-record JSON.
12. Assert type output is exactly the current selected declared type name and
    contains no value, echoed Pointer address, field listing, definition, or
    union narrowing.
13. Add a record field and an unregistered structured value in a test schema;
    assert Human, JSON, and type display work without changing inspect dispatch.
14. Reject combined display modes and every removed inspect option; verify that
    inspection performs no runnable preparation, provider request construction,
    Run creation, or store mutation.
15. Reject an old store unchanged in read-only and writable modes; perform no
    migration or deletion.
16. Verify inspect help and run the complete default offline verification.

## Risks

- Reject old stores before opening them to prevent silent Pointer
  reinterpretation.
- Source serialization, traversal, pointer-field metadata, and declared type
  names from one record-schema registry to prevent record-shape drift; inspect
  must not carry its own record field or type-name tables.
- Adapt all current Control kinds and Agent State fields when replacing the two
  control record classes; do not restore the architecture that preceded schema
  31.
- Apply existing `TypedPointer` validation after resolution to preserve runtime
  type safety.
- Keep summaries deterministic and visibly truncated so they cannot be mistaken
  for complete values.
- Keep specialized human renderers optional and reuse the Chat UI renderer for
  `Part`/`Part[]`; every unregistered type must remain usable through the
  generic fallback.
- Validate the reserved-colon rule recursively when values enter durable state
  so every canonical JSON field remains addressable.
- Rebase onto current `origin/main` before implementation verification because
  execution records and controls are active development areas.

## Decisions

- Record, field, and Pointer are the only generic inspection terms.
- One Pointer identifies either a historical record or one of its fields.
- `TypedPointer` is `POINTER:TYPE`; the old `TYPE@POINTER` form is removed.
- `:` is reserved for `TypedPointer`; field names cannot contain it.
- `.` expresses Step hierarchy, `@` selects a Control index, and `/` enters a
  field using RFC 6901 escaping.
- Record kind is inferred from Pointer syntax; type prefixes are forbidden.
- Roots never imply output, local, or error fields.
- `ControlRecord` is the single durable control record model; meaningful scoped
  payload unions may remain.
- Inspection is historical and read-only; future model-call preview and sending
  are deferred to a separate command.
- There is no focus stage and no independent record-schema command.
- `--human`, `--json`, and `--type` are mutually exclusive; human is default.
- Human display is a progressive one-level browser with complete copyable
  Pointer tokens and object summaries, but no type names. It resolves declared
  Pointer values, marks them with a separate `→`, and uses optional type-owned
  renderers with a generic fallback.
- `--type` emits only the selected declaration's current code-level type name;
  it does not expose definitions or child fields and is not a stable schema
  contract.
- Inspect obtains record shape and type metadata from the shared schema and has
  no record-specific dispatch, so ordinary record/type evolution does not
  require inspect changes.
- Old semantics are removed with no migration or compatibility layer; every old
  RunStore schema is rejected unchanged.
- The definition has no open questions and requires human approval before
  implementation.
