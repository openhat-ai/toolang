# Define the Chat Show Result Boundary

## Work Type

Feature definition. This document defines the presentation of a durable result
reopened by the Chat TUI `:show` command. It does not implement product code.

## Verified Current Behavior

- `:show [run_id]` resolves a durable output through `ChatClient.get_result()`.
- `ChatResult` carries the `run_id` and `output` needed by the result view.
- The TUI renders the result heading through the generic slash-command body as
  `: Result RUN_ID`, followed by one blank line and the Markdown result.
- Root run footers use `• RUN_ID STATUS ───` and status color. The same `•`
  marker also identifies execution output, so reusing it would make reopened
  history look like new execution output.
- No current result-view marker identifies the reopened output as a section
  whose body follows below.

## Goal

Give a reopened durable result a quiet, recognizable boundary that identifies
the source run and the result section without competing with the result body.

## Success Criteria

- `:show` renders `▿ RUN_ID result ───` instead of `: Result RUN_ID`.
- The marker, caption, and rule all use Rich `dim` styling.
- The result body remains the visually primary normal-intensity Markdown block.
- Existing one-blank-line spacing between the boundary and result body is
  preserved.
- The rule uses the same minimum length as the root run footer.

## Scope

### In Scope

- Add a dedicated Chat TUI result boundary and use it for `:show`.
- Share the root run footer's minimum rule length with the result boundary.
- Update Chat TUI unit and PTY expectations and current presentation docs.

### Out of Scope

- Changes to live execution progress or root run footer behavior.
- Changes to result lookup eligibility, ordering, or error behavior.
- New result facts such as duration, token usage, or cost.
- Changes to the `:show` command syntax or Markdown body rendering.
- Script presentation changes.

## Presentation Design

The stable output is:

```text
▿ run_ma8hccd9 result ────────────────

• Result body rendered as Markdown.
```

The command control bar remains immediately above this excerpt and is
unchanged.

### Marker

Use `▿`.

- `▿` points from the section boundary toward the result body below.
- Its hollow shape has less visual weight than the live execution marker `•`
  and suits the all-dim historical context.
- Do not use `•`, because it already denotes execution rows and root run
  footers.
- Do not use `◆`, because its solid shape suggests a terminal run event rather
  than a historical result view.
- Do not use `◇`, because it identifies an object but does not communicate the
  boundary-to-body relationship as directly as `▿`.
- Do not use `↳`, because a reopened result is not a child or causal closure of
  the `:show` command.

The marker is presentational, not an interactive disclosure control. The Chat
TUI does not add collapse or expand behavior in this scope.

### Caption

The caption is exactly `RUN_ID result`. The stable lowercase `result` label
identifies the boundary type instead of repeating the run's terminal status.
No new field is added to `ChatResult`, and no status is inferred from the
presence of output.

### Styling

Apply one `dim` style to the marker, caption, separating space, and rule. Do not
apply green, red, or yellow status color: the boundary identifies historical
context, while the result body should remain visually primary.

### Width and Spacing

- Start the marker at column zero.
- Use `─` for the trailing rule.
- Reuse the root footer's `RUN_FOOTER_MIN_RULE_WIDTH`; do not duplicate its
  numeric value in the Chat result renderer.
- Follow the Chat execution maximum-width and display-cell truncation rules.
- Use the shared minimum rule length when width permits; truncate the caption
  and shorten the rule at narrow widths rather than wrapping.
- Render exactly one blank line between the boundary and the first result row.
- Preserve the existing single blank line after the result block.

## Design Touchpoints

- `src/toolang/cli/common/execution_progress/rich_rendering.py`: expose the
  existing root footer minimum rule length without changing footer behavior.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: own the dedicated dim `▿`
  result boundary and compose it with the existing Markdown result body.
- `src/toolang/cli/toolang/commands/chat/tui.py`: pass the command message, run
  id, and output to the dedicated result block instead of constructing the
  generic `Result RUN_ID` slash body.
- `tests/unit/cli/test_chat_tui.py`: verify text, styles, shared minimum width,
  narrow width, and spacing.
- `tests/system/cli/test_chat_tui_e2e.py`: verify the durable result boundary in
  a pseudo-terminal.
- `docs/execution-presentation.md`: document `▿` as a historical result-view
  boundary distinct from execution rows.

## Acceptance Tests

1. Explicit and latest result lookup behavior is unchanged.
2. `:show` renders a line beginning `▿ run_saved result `, followed only by `─`
   rule cells.
3. The rule contains exactly `RUN_FOOTER_MIN_RULE_WIDTH` cells when the
   configured width permits.
4. Every segment in the boundary uses `dim`; the Markdown result body does not
   inherit `dim`.
5. Exactly one empty row separates the boundary from the first result row, and
   exactly one empty row closes the result block.
6. Narrow-terminal rendering does not wrap or exceed the configured width.
7. Help, model-list, queue, and other slash-command rendering is unchanged.
8. The PTY flow scenario reopens a durable result and observes both
   `▿ RUN_ID result` and the saved output without a traceback.
9. The default offline verification suite passes.

## Risks

- Generic slash-command spacing must not be changed while introducing the
  dedicated result block, or unrelated command layouts may regress.
- `▿` can conventionally imply an interactive disclosure. Tests and
  documentation must present it only as a static section marker unless a future
  feature explicitly adds collapse behavior.
- Full-width Unicode rendering must use display-cell width rather than Python
  string length to avoid overflow in narrow or CJK terminals.

## Open Questions

None. This definition was approved before implementation.
