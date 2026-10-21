# Hub snapshot colors and alignment

Status: Proposed on 2026-10-21; awaiting explicit human confirmation before
implementation. This updates only the presentation decisions in
[Hub instance snapshot](hub-status-tree.md).

## Goal and scope

Make names the primary visual element of `too hub`, with aligned status and
supplementary fields for both Hub and agents. `too` is an alias for `toolang`.
Success means consistent columns, distinct instance suffixes, and field-level
positive/negative styling in terminal output.

Change only snapshot rendering and its acceptance coverage. Keep discovery,
readiness, version lookup, instance labels, lifecycle commands, exit codes,
summary counts, and diagnostics unchanged. This definition ships no product
code. Live refresh, new flags, and changes to other commands are outside scope.

## Presentation decisions

All rows use the same three columns, with two spaces between columns:

```text
hub                               running      v0.4.0a11
├── gus                           running        enabled
├── lex@rabcd1234                 running       disabled
└── lex@vabcd1234                 not running          -

3 agents, 2 running, 1 talk enabled
```

An offline Hub row contains `hub` in column one, `not running` in column two,
and an empty third column. Empty rosters still use the shared Hub layout and
retain the existing `no agents` line without a count summary.

- Render `hub` and the agent-name portion in Rich `cyan`, without bold or dim.
  Render the complete `@r…` suffix in `yellow` and the complete `@v…` suffix in
  `magenta`, including any existing `#…` discriminator. Derive styled segments
  from the existing name and label; do not shorten, reformat, or reclassify IDs.
  Tree branches retain their normal style.
- Column one has a minimum width of 32 terminal cells, including the four-cell
  agent tree prefix, instead of the current 24. Grow it for complete labels
  when space permits. Keep the Hub name at the root indentation. This moves
  the supplementary fields eight cells farther right for short names.
- Left-align column two. Retain right alignment in column three, with the same
  right edge for Hub version and agent Talk values. Compute widths once from
  Hub and agent content: column two has an 11-cell minimum for `not running`,
  and column three retains its 8-cell minimum but grows for the Hub version.
  Size to content rather than stretching to the terminal's full width.
- A ready Hub displays `running` and its supplied version in columns two and
  three. Missing or `unknown` version displays `unknown` in column three.
  Every non-ready Hub displays `not running` with no version, preserving the
  existing readiness classification and diagnostics.
- `running`, `enabled`, and known versions use normal intensity. `not running`,
  `disabled`, `-`, and `unknown` use `dim` only on that field. Names, summary,
  `no agents`, diagnostics, and tree branches are never dimmed by row state.
- When the natural layout exceeds terminal width, shrink and wrap all three
  columns without losing text. Share widths and continuation alignment between
  Hub and agent rows, retain instance colors through wrapping, and preserve
  existing Unicode-cell measurement, ASCII fallback, and plain-output behavior.

## Implementation touchpoints

- [Snapshot renderer](../../src/toolang/cli/toolang/commands/hub_status.py):
  include the Hub in shared sizing, render its status/version as separate
  fields, and preserve styled name segments through wrapping.
- [Renderer tests](../../tests/unit/cli/test_hub_status.py) and
  [CLI tests](../../tests/unit/cli/test_hub_commands.py): revise output
  expectations and cover the acceptance scenarios below.
- [Existing instance vocabulary](../../src/toolang/up/types.py) remains the
  source of names and labels; no runtime or persistence changes are needed.
- During implementation, generate the user-visible Unreleased entry through
  `too aid.too update_changelog`; a maintainer verifies it before merge.

## Acceptance scenarios

| Scenario | Pass condition |
| --- | --- |
| Online and offline Hub | Hub status starts at the same cell as agent status; known version shares the Talk column's right edge. Offline Hub has an empty third field. Empty rosters retain the same layout rules. |
| Colors and intensity | Resident names and `hub` are cyan; same-name `@r…` and `@v…` instances have yellow and magenta suffixes. Positive fields are normal; negative and unknown fields are dim without affecting names. |
| Wide layout | At 80, 120, and 160 cells, short-name status starts at cell offset 34; long names/versions expand their columns when they fit, and rows remain sized to content. |
| Narrow layout and encoding | At 20, 32, and 40 cells, long versions, IDs, wide Unicode names, and ASCII-escaped names retain all text and field styles within the existing effective minimum width of 20. |
| Plain terminals | Pipes, `NO_COLOR`, and `TERM=dumb` preserve readable values and alignment without ANSI controls; existing tree fallback remains intact. |
| Existing behavior | Ordering, counts, partial diagnostics, readiness and request behavior, and lifecycle routing retain their current semantics. |

Use offline fixtures and fake transports. Run the focused renderer/CLI tests
and the repository's default verification before implementation commits.

## Risks and open questions

Long versions can force wrapping sooner because the Hub now participates in
column sizing. Terminal palettes determine the exact cyan/yellow/magenta shades;
instance markers remain readable without color. Human-readable spacing and Hub
wording change, so consumers parsing the old output need to adjust.

Open questions: none; colors, widths, and unknown-version handling above are
concrete proposed defaults for human confirmation.
