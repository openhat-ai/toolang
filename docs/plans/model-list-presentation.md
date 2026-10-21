# Model list presentation

Status: Approved in conversation on 2026-10-21; implementation requested.

## Goal and scope

Fit human `too models` output to the terminal while preserving complete model
identities and the facts needed to select a model. Change presentation only;
retain selection, ordering, queries, JSON records, and summary counts.

## Decisions

- Display `MODEL`, `CONTEXT`, `PRICE ($/1M)`, `STATUS`, `INPUT`, `FEATURES`,
  `MAX_OUTPUT`, and `OUTPUT`, in that order.
- Always retain the first four columns. When the selection does not fit, omit
  whole optional columns in order: `OUTPUT`, `MAX_OUTPUT`, `FEATURES`, `INPUT`.
  All rows in one invocation use the same columns.
- Use the full terminal width with an 80-cell minimum and a 120-cell fallback.
  Keep model identities and displayed values complete on single logical rows.
  If mandatory columns exceed the budget, allow a wider table; physical terminal
  wrapping is outside the renderer's control.
- Show availability tags in `STATUS` with readable spaces; omit provenance tags.
  Preserve complete numeric limits, comma separators, and two-decimal prices.
  Right-align context, price, and maximum output; align input/output price parts.
- Retain shared column-zero headers, separator, plain summaries, and empty output.

## Touchpoints and acceptance

- CLI model catalog command, shared table width override, and current CLI/query
  documentation. Keep provider presentation unchanged.
- Test every column-removal boundary, 80/120/240-cell layouts, below-minimum and
  unavailable terminal sizes, long and Unicode identities, status diagnostics,
  zero/missing values, price alignment, and literal/control text handling.
- Verify identical JSON and selected records across human widths and modes.
  Run the repository's default checks before code commits.

## Risk

Visible optional columns depend on both terminal width and the selected rows.
Very long mandatory values can exceed the terminal; `--json` remains unchanged.
