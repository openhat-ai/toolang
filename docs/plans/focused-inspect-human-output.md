# Focused Inspect Human Output

## Status

Approved by the human on 2026-08-31. The human clarified on the same date that
call request and result payloads must remain complete in Human output.

## Goal

Make every Human `inspect` projection present only the facts needed for its
view. Keep collection tables compact, make field browsing show durable raw
values, and reduce structural trees to an immediately scannable hierarchy.

## Success Criteria

- Run collections omit occurrence facts and repeated subject scope.
- Step collections combine status and activity and expose independent direct
  child Run and child Step counts.
- Field tables render raw selected values without resolving stored pointers.
- Structural trees contain exactly `NODE`, `ACTIVITY`, and `OCCUR` columns.
- Tree and Step activity uses the same status marker vocabulary.
- Child occurrence rows show indexes while their parent Step shows totals.
- Model and tool call Human output is complete and ordered by call lifecycle.
- Canonical JSON, subject grammar, durable ordering, and focused value
  resolution remain compatible.

## Projection Responsibilities

- `records` is a compact collection overview.
- `fields` is a direct raw-field index. It does not unwrap `Local` or follow a
  `Pointer` or `TypedPointer`.
- `value` is the focused value view and retains existing pointer resolution,
  validation, and specialized rendering.
- `tree` is a complete durable ownership hierarchy, not a metrics or timeline
  view.
- `call` is a historical request/result review. Human output preserves request
  text, tool signatures, structured request facts, and result payloads without
  truncation; `--json` remains the exact normalized request value, and results
  remain independently addressable through Step output pointers.

## Status Markers

Step tables and structural trees prefix activity with one marker:

| Status | Marker | Color |
| --- | --- | --- |
| `pending` | `•` | dim |
| `running` | `•` | cyan |
| `succeeded` | `✔` | green |
| `failed` | `✖` | red |
| `canceled` | `✖` | yellow |

Color distinguishes statuses that share a marker. Non-color output preserves
the broader active, successful, and unsuccessful groups; exact status remains
available from record fields and JSON.

## Run Collections

Run tables do not render `OCCUR`. Columns are ordered identity, operation,
status, size, ownership, and time:

| Subject | Human columns |
| --- | --- |
| root `runs` | `RUN`, `RUNNABLE`, `STATUS`, `STEPS`, `THREAD`, `PARENT STEP`, `CREATED` |
| `THREAD runs` | `RUN`, `RUNNABLE`, `STATUS`, `STEPS`, `PARENT STEP`, `CREATED` |
| `STEP runs` | `RUN`, `RUNNABLE`, `STATUS`, `STEPS`, `CREATED` |

The selected Step is already present in the command, so `STEP runs` does not
repeat it in every row. Existing collection order and exact pointers remain
unchanged.

## Step Collections

Step tables combine status and operation in `ACTIVITY` and put `OCCUR` last:

| Subject | Human columns |
| --- | --- |
| `RUN steps` | `STEP`, `ACTIVITY`, `CHILD RUNS`, `CHILD STEPS`, `PARENT STEP`, `CREATED`, `OCCUR` |
| `LOOP_STEP steps` | `STEP`, `ACTIVITY`, `CHILD RUNS`, `CHILD STEPS`, `CREATED`, `OCCUR` |

Activity starts with the status marker, followed by the existing aligned Step
kind and durable operation, for example `✔ [par] map search_web par 4`.
`CHILD RUNS` and `CHILD STEPS` count only direct visible children. Counts are
read in the same focused SQLite snapshot as the Step records and do not build a
structural tree.

## Raw Field Values

Field tables retain `FIELD`, `TYPE`, and `VALUE`. Each row uses the child
selection's declared type and raw canonical value. Rendering does not call the
Human pointer resolver, unwrap `Local`, follow pointers, add a resolved-type
marker, or fail because a target pointer is missing, mismatched, or cyclic.

Selecting a field directly remains a `value` projection when applicable and
retains current pointer resolution and validation. JSON remains the exact
selected canonical value in both cases.

## Structural Tree

Run `tree` and container-Step `call` render exactly three columns:

```text
NODE                  ACTIVITY                              OCCUR
run_root              ✔ <flow>  research
├─ run_root.1         ✔ [par]   map search_web par 4       6 items · 4 lanes
│  └─ run_search_1    ✔ <agic>  search_web                 item 1 · lane 1
└─ run_root.2         • [model] openai/gpt-5
```

The complete node set and exact reusable pointers remain visible. Human output
removes the independent status, duration, and metrics columns; JSON retains all
existing timestamps and metrics.

For item and lane occurrence, a child node displays only one-based indexes,
such as `item 2 · lane 1`. The owning parent Step displays known totals, such as
`6 items · 4 lanes`. Totals come from durable Step given/noted facts, with
consistent direct child occurrence counts as a fallback; missing dimensions are
omitted and no total is invented. Iteration index and phase keep their current
semantics.

A failed or canceled node retains its bounded error continuation row, placed in
the activity area rather than masquerading as another node pointer.

## Call Projections

Model call Human sections follow request/result order:

1. call summary;
2. instructions;
3. messages;
4. tool signatures;
5. output contract when present;
6. continuation when present;
7. result.

Empty sections are omitted. Request text, output contracts, continuation data,
and results are rendered without truncation. Section content retains the
established review presentation: authored text keeps line breaks, messages use
descending review numbers, tool parts use fenced Human blocks, structured data
uses indented key and index lines, output contracts use formatted JSON, and
all tool signatures and descriptions are shown. Parameter schemas remain
summarized by the signatures. Section headers contain only their titles, without
message counts, tool counts, or result Pointers. Result remains last.

Tool call Human output shows one summary, the normalized invocation, and the
stored result payload without truncation. Results retain the same fenced,
structured Human block used inside model messages. It displays both call
identifiers only when they differ. Bare canonical call JSON remains unchanged.

## Scope

Included:

- Human collection, field, tree, and call rendering;
- direct visible child Step counting and occurrence-total inspection facts;
- tests and directly affected CLI documentation.

Excluded:

- changes to canonical JSON or persisted record schemas;
- new projectors, flags, filters, pagination, folding, or sorting;
- runtime execution, visibility, ownership, and control behavior;
- exact event timelines or new metrics projections.

## Design Touchpoints

- `src/toolang/execution/inspection.py`: focused direct-child count and
  occurrence-total vocabulary.
- `src/toolang/execution/store.py`: transactionally consistent focused counts
  and occurrence facts.
- `src/toolang/cli/toolang/commands/inspect.py`: projector-specific Human
  rendering, status markers, occurrence labels, columns, and call summaries.
- `tests/unit/execution/test_inspection.py`: focused descriptor counts and
  occurrence facts.
- `tests/unit/cli/test_inspect_rendering.py` and
  `test_inspect_subject_navigation.py`: raw fields, status markers, tree columns,
  occurrence labels, and call summaries.
- `tests/integration/cli/test_local_core_commands.py`: end-to-end columns,
  pointers, errors, and unchanged JSON.
- `docs/api.md`: focused Human projection contracts.

## Acceptance Tests

1. All Run collection variants omit `OCCUR`, preserve order, and use the exact
   columns defined above.
2. Step tables prefix activity with the correct marker and report exact direct
   visible child Run and child Step counts in separate columns.
3. Field tables show raw Local and pointer data, perform no target read, and
   succeed for missing and cyclic pointers; selecting the pointer value directly
   retains existing resolution failures.
4. Run tree and container call Human output contain exactly `NODE`, `ACTIVITY`,
   and `OCCUR`, render every node once, and preserve reusable pointers.
5. Parent Steps show known item/lane totals while child nodes show one-based
   indexes without counts; incomplete occurrence data is not invented.
6. Pending/running/succeeded/failed/canceled markers and colors match the
   approved mapping; failed and canceled errors remain diagnosable.
7. Model and tool call Human output omits empty sections, follows lifecycle
   order, preserves request text and result payloads without truncation, and
   uses clean title-only section headers.
8. Every `--json` projection is structurally identical to current output.
9. Ruff, formatting, ty, and the complete default offline pytest suite pass.

## Risks And Open Questions

- Field Human output becomes deliberately more storage-oriented. The focused
  value view remains the path to resolved content.
- Failed and canceled share a glyph in non-color output. Exact status remains
  inspectable from fields and JSON.
- Child counts and occurrence totals must stay snapshot-consistent and must not
  turn primitive Step inspection into structural-tree construction.
- Call output can be large by design because its purpose is exact request/result
  review; callers choose this explicit projector knowingly.
- There are no open product questions.
