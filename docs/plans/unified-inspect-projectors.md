# Unified Inspect Projectors

## Status

Approved by the human on 2026-08-30. Implementation may proceed in a separate
change.

## Goal

Give every completed `inspect` subject one projector model and let tabular
output identify both its source and projected row kind in the leftmost heading.
Remove explanatory footers that repeat context already present in the command
and table.

## Success Criteria

- Record collections, record fields, direct values, and `model-call` use one
  projection dispatch model.
- Scoped collection tables use compound headings such as `THREAD RUN` and
  `RUN STEP`; root Agent collections keep the concise `THREAD` and `RUN`
  headings.
- Whole-record field tables use `THREAD FIELD`, `CONTROL FIELD`, `RUN FIELD`,
  or `STEP FIELD`.
- Structured nested fields identify their selected display type in the field
  heading.
- Human inspection emits no trailing explanatory footer.
- Subject grammar, canonical JSON, values, row order, and navigation remain
  compatible.

## Current Behavior

The command resolves subjects in three separate output branches:

- collections are rendered by `_render_collection()`;
- Pointer selections are rendered by `_render_pointer()`;
- the explicit `model-call` projector is applied and rendered separately.

Only `model-call` is currently called a projector. A whole record Pointer
actually projects its direct fields, while `threads`, `runs`, `THREAD runs`,
and `RUN steps` project record collections.

Field and explicit-projector output append prose context such as:

```text
term_fbm8x2wf has type ThreadRecord; append a FIELD to inspect a child.
run_ab12.0/output/value resolves to Part[].
run_ab12.0 projected as model-call.
```

Collection tables have only the projected record kind in their first heading,
so `RUN` does not distinguish Agent Runs from Runs scoped by a Thread. Field
tables use the generic `FIELD` heading and rely on the footer for the selected
record type.

## Projection Model

A projector is the final read-only view selected after subject resolution. One
and only one projector handles every successful inspect query:

| Projector | Selection | Human result | JSON result |
| --- | --- | --- | --- |
| `records` | a collection subject | summarized record rows | canonical record array |
| `fields` | a browsable Pointer value | direct child fields | exact selected value |
| `value` | a scalar, empty container, resolved value, or specialized block | rendered value | exact selected value |
| `model-call` | a model Step plus the explicit terminal name | structured model call | canonical normalized model call |

`records`, `fields`, and `value` are implicit projector kinds, not new subject
tokens. Commands do not gain `records`, `fields`, or `value` suffixes, and those
words are not newly reserved. `model-call` remains the only explicit projector
in the grammar.

Projector selection follows existing behavior:

1. an accepted explicit projector wins;
2. a collection subject selects `records`;
3. a Pointer whose selected value is currently browsable selects `fields`;
4. every other Pointer selects `value`.

Human Pointer resolution, type validation, specialized value rendering, and
JSON's non-resolving behavior do not change.

## Pointer Type Marker

A resolved child keeps its canonical field suffix unchanged. Pointer
indirection is shown in the `TYPE` column with a Rust-like leading `*`:

```text
FIELD       TYPE
/output     *Part[]
```

This replaces the current shape:

```text
FIELD       TYPE
/output →   Part[]
```

The marker means the displayed value and type were obtained by dereferencing
a stored `Pointer` or `TypedPointer`. It prefixes the resolved display type
without whitespace, is presentation-only, and is never part of a Pointer or
field suffix. Multiple dereference hops still use one `*`; the marker records
that resolution occurred, not its depth. Nullable and union labels retain
their existing grouping before the prefix is applied, for example
`*(Text | Part[])?`.

Directly selected resolved values have no table row or `TYPE` cell and gain no
replacement inline marker after footer removal. Resolution validation and
errors remain unchanged.

## Compound Table Headings

The leftmost heading names the explicit projector source followed by the
projected row kind. Both names are uppercase and singular. Agent is the
implicit root source and is omitted, together with the separating space, from
root collection headings.

| Query | Source | Row kind | Leftmost heading |
| --- | --- | --- | --- |
| `inspect threads` | Agent (implicit) | Thread | `THREAD` |
| `inspect runs` | Agent (implicit) | Run | `RUN` |
| `inspect THREAD runs` | Thread | Run | `THREAD RUN` |
| `inspect RUN steps` | Run | Step | `RUN STEP` |
| `inspect THREAD` | Thread record | Field | `THREAD FIELD` |
| `inspect CONTROL` | Control record | Field | `CONTROL FIELD` |
| `inspect RUN` | Run record | Field | `RUN FIELD` |
| `inspect STEP` | Step record | Field | `STEP FIELD` |

For a browsable nested field, the source name is its existing displayed type
label converted to uppercase, followed by `FIELD`. For example, a selected
`Pointer[]` container uses `POINTER[] FIELD`. Whole records use their logical
record kind rather than schema class names such as `RunRecord`.

Only the leftmost heading changes. Existing relative field suffixes, full
record Pointers, `TYPE` and `VALUE` columns, summaries, counts, statuses,
timestamps, ordering, Rich table style, and empty-table behavior remain as
currently defined. This feature does not add the separately proposed `STEPS`
count column to Run rows.

## Footer Removal

Human projector output does not append a prose context footer. This removes:

- `has type ...` and `append a FIELD ...` after field projections;
- `has type ...` and `resolves to ...` after direct values;
- `projected as model-call` after the explicit projector.

The renderer also removes blank lines emitted solely to separate those
footers. A direct scalar or specialized block therefore prints only its value.
Its Pointer remains in the command, and its declared type remains discoverable
from the parent field table or canonical JSON. Resolved child rows use the
`TYPE`-column `*` marker defined above instead of modifying the field suffix.

## Scope And Compatibility

Included:

- unified projector selection and human rendering in `inspect`;
- compound leftmost headings for every existing record collection and
  browsable Pointer table;
- removal of all existing human inspect context footers;
- tests and directly affected CLI documentation.

Excluded:

- new subject, relation, or projector tokens;
- changes to Pointer syntax or subject transition legality;
- changes to canonical JSON or persisted records;
- new columns, filters, limits, pagination, or sorting;
- changes to the legacy `threads` and `runs` commands;
- changes to HTTP APIs, execution behavior, or model-call reconstruction.

## Design Touchpoints

- `src/toolang/cli/toolang/commands/inspect.py`: represent implicit and explicit
  projector kinds in one resolved projection path; route human and JSON output
  through it; derive compound headings from subject scope, record kind, or the
  selected display type; move resolved-Pointer indication from the field label
  to the type label; remove footer rendering.
- `tests/integration/cli/test_local_core_commands.py`: update exact pointer and
  model-call output assertions and cover every compound collection and record
  field heading.
- `tests/unit/cli/`: add focused projector-selection and heading cases if the
  extracted helpers have behavior not fully covered through the CLI.
- `docs/api.md`: describe implicit projectors, compound headings, footer-free
  human values, and unchanged JSON behavior.

No execution schema, history, store, or persistence change is required.

## Acceptance Tests

1. Root collections render `THREAD` and `RUN`; explicitly scoped collections
   render `THREAD RUN` and `RUN STEP`.
2. Whole Thread, Control, Run, and Step records render `KIND FIELD`, preserve
   relative field suffixes, and preserve the `TYPE` and `VALUE` columns.
3. A browsable nested field uses its uppercase displayed type plus `FIELD`.
4. Direct scalar, null, resolved, multiline, and specialized Part values retain
   their current content and emit no footer or footer-only blank line.
5. Human `model-call` retains all sections and emits no projection footer.
6. A resolved `Pointer` or `TypedPointer` child keeps its canonical field
   suffix and prefixes its resolved type with one `*`; the old `→` marker is
   absent, and all current resolution failures remain unchanged.
7. `--json` output for collections, records, fields, values, and `model-call`
   is structurally identical to current output.
8. `records`, `fields`, and `value` are not accepted as new terminal syntax.
9. The complete default verification passes.

## Risks And Open Questions

Removing leaf-value footers also removes their inline type annotation. This is
intentional: concise projection output takes priority, while type information
remains available one level up or in JSON. Long nested-field type names may
widen a table heading, matching the existing non-truncating type-column policy.
There are no open product questions in this definition.
