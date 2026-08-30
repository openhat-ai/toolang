# Inspect Runs Step Count

## Status

Approved by the human on 2026-08-30.

## Goal

Add a `STEPS` column to the human-readable `inspect runs` tables so an
operator can compare Run size without inspecting each Run separately.

## Success Criteria

- `too AGENT inspect runs` displays a numeric `STEPS` column for every Run.
- `too AGENT inspect THREAD runs` displays the same column.
- The value equals the number of ordinarily visible Steps owned by that Run.
- Empty and running Runs display `0` and their current visible count,
  respectively.
- Canonical JSON inspection and other Run-listing interfaces remain unchanged.

## Behavior

The human headings become:

```text
RUN  THREAD  TITLE  STEPS  STATUS  CREATED
RUN  TITLE  STEPS  STATUS  CREATED
```

`STEPS` is the count of the same records returned by `inspect RUN steps`:

- count every ordinarily visible Step whose owning Run is the row's Run;
- include nested StepPaths because they are still owned by that Run;
- exclude ejected Steps through the store's existing default visibility;
- do not include Steps owned by child Runs;
- render the current count for a running Run and `0` when none exist.

This is a human-presentation change only. `inspect runs --json` remains a bare
array of canonical Run records and gains no synthetic count field. The legacy
`too AGENT runs` command, HTTP schemas, Run detail output, ordering, titles,
statuses, timestamps, and collection bounds do not change.

## Design And Touchpoints

- `src/toolang/execution/history.py`: allow `RunHistory.describe_runs()` to
  consume an optional caller-supplied mapping of already loaded visible Steps.
- `src/toolang/cli/toolang/commands/inspect.py`: load visible Steps once for the
  selected Runs, use that mapping for both summaries and counts, and render
  `STEPS` between `TITLE` and `STATUS` in both Run table shapes.
- `tests/integration/cli/test_local_core_commands.py`: cover top-level and
  Thread-scoped tables, zero and nested counts, child-Run isolation, unchanged
  JSON, empty headings, and the single Step batch read.

No persistence migration, new query grammar, public schema field, or API change
is required. The separately approved unified-projector definition remains a
separate implementation concern; its future scoped heading changes compose
with this column without changing the count semantics.

## Acceptance Tests

1. A Run with no Steps renders `0`.
2. A Run with flat and nested visible Steps renders their combined total.
3. A child Run's Steps affect only the child Run row.
4. Both `inspect runs` and `inspect THREAD runs` place `STEPS` between `TITLE`
   and `STATUS` and show the correct value.
5. `inspect runs --json` remains canonical and contains no presentation-only
   step count.
6. Empty Run collections still print the `STEPS` heading.
7. The complete default verification passes.

## Risks And Open Questions

The count is a read-time snapshot, so it can increase while a Run is active;
this matches the existing non-transactional inspection semantics. There are no
open product questions in this definition.
