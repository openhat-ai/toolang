# Chore list presentation

Status: Approved in conversation on 2026-10-22; implementation requested.

## Goal and scope

Make human `too AGENT chore list` readable at ordinary terminal widths while
retaining scheduling, execution, source, and failure information by default.
Change CLI presentation only; preserve selection, ordering, inspection semantics,
authoring, scheduler checkpoints, and execution stores.

## Decisions

- Keep the display order `CHORE`, `TITLE`, `STATUS`, `LAST RUN`, `NEXT RUN`,
  `SCHEDULE`, `ERROR`, `LOCATION`, independently of omission priority.
- Use the full terminal width, an 80-cell minimum, and a 120-cell fallback.
  Below 80 cells, render the same layout as at 80.
- Keep one logical row per chore. Keep identity, scheduler status, next-run
  timestamp, schedule, and any displayed latest-run status complete.
  `LAST RUN` remains a status;
  `NEXT RUN` retains the current local ISO timestamp and earliest-due selection.
  Preserve `once` and `unsynced` with their current meanings.
- Shrink `TITLE` first, then `ERROR`, then `LOCATION`, down to their header
  widths. Clip prose with `…`; clip locations in the middle to retain both ends.
  Keep existing home-relative locations and complete inline source line suffixes;
  reserve enough location width for that suffix and an ellipsis.
- After shrinking flexible fields, omit `TITLE`, then `LAST RUN` if needed.
  Refit remaining flexible fields after each omission; retain display order and
  use the same columns for all rows. Always retain `CHORE`, `STATUS`, `NEXT RUN`,
  `SCHEDULE`, `ERROR`, and `LOCATION`.
- If the remaining columns still exceed the budget at their minimum widths,
  allow a wider table, following the model-list overflow contract. Keep recurrence
  rules complete and add no continuation rows.
- Preserve column-zero headers, one header separator, two-space column gaps, and
  the plain bottom summary. Sanitize terminal controls, normalize embedded
  whitespace, and preserve literal markup. Add no flags or JSON mode.

## Touchpoints and acceptance

- CLI owner: `src/toolang/cli/toolang/commands/chore.py`; reuse existing output
  and display-cell helpers where practical. Keep width resolution at the CLI.
- Add focused rendering tests and extend
  `tests/integration/cli/test_local_core_commands.py`. Cover 80/120/240-cell and
  fallback layouts, below-minimum widths, long fixed fields, Unicode, controls,
  literal markup, empty/singleton counts, and location suffixes.
- Verify both omission boundaries independently of display order, one row per
  item, and clipping only in the three flexible columns.
  Verify source/scheduler/latest-run error precedence, local timestamps,
  manual-versus-scheduled due selection, and unsynced definitions remain intact.
- Verify listing remains read-only and does not prepare state or resolve
  runnables. Update the CLI design and generated changelog during implementation;
  run the repository's default checks before code commits.

## Risk

Long RRULEs and timestamps can exceed the terminal even after prose is clipped;
clipped errors may require opening the source or inspecting execution history.
