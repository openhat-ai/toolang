# Implement Tool Call Terminal Blocks

## Work Type and Approval

Feature implementation. The human approved this behavior on 2026-08-22 by
requesting an implementation worktree and pull request with the decisions
below.

## Goal and Success Criteria

Make completed tool calls easier to scan by visually separating their summary
from their result or error. The feature succeeds when tool results and errors
use a compact rectangular surface, live summaries are visually quiet, and the
display obeys the shared progress width limit in both Chat and Script.

## Scope and Design

- Render standalone running tool summaries as dim progress rows, including the
  marker. Keep terminal summaries as ordinary progress rows.
- Begin every Step with one unpainted blank row, including a model Step after a
  Tool Step. A preceding statement, iteration, or condition header's trailing
  blank row satisfies that boundary; do not duplicate it. Do not repeat the gap
  for continuation fragments from the same Step.
- Render succeeded results and failed errors on the tool-detail surface.
- Keep the tool-detail surface borderless.
- Keep the normal two-cell continuation indent, matching model code-block
  alignment.
- Separate the summary from the detail surface with one unpainted blank row.
- Pad detail content by one empty row above and below and one empty column on
  the left and right.
- Fill each detail row to the available width, bounded by
  `TOOLANG_PROGRESS_MAX_WIDTH` (120 by default), and wrap long content inside
  that width.
- Share terminal-owned ANSI slot 8 with the Chat control bar and input box for
  the detail background, with ANSI foregrounds that preserve failure tone.
- Preserve the same gaps, padding, and configured width in non-TTY output while
  omitting ANSI sequences and live replacement.
- Use `NAME ARG executing ...` as the default running summary and `NAME ARG` as
  the default succeeded or failed summary. Keep the canceled default unchanged.
- Keep compact parallel-lane summaries unchanged; they remain one-line lane
  content rather than expanding into cards.
- Do not change result serialization or web/API presentation.

## Touchpoints

- `src/toolang/cli/common/execution_progress/types.py`
- `src/toolang/cli/common/execution_progress/step_projection.py`
- `src/toolang/cli/common/execution_progress/rich_rendering.py`
- `src/toolang/cli/toolang/commands/chat/rendering.py`
- `src/toolang/execution/executor/steps/tool.py`
- `tests/unit/cli/test_execution_progress_projector.py`
- `tests/unit/cli/test_chat_tui.py`
- `tests/unit/cli/test_script_run_presenter.py`
- `tests/unit/execution/test_tool_step_summary.py`
- `docs/execution-presentation.md`
- `docs/tools.md`

## Acceptance Tests

1. A standalone running tool summary is entirely dim, including its marker;
   canceled summaries retain plain styling. Every Step begins after exactly one
   unpainted blank row unless the preceding header already owns that row.
2. Succeeded and failed summaries remain plain and have no background.
3. Default lifecycle summaries use the approved running, succeeded, failed, and
   canceled forms without displaying the tool family.
4. Results and errors use the detail surface.
5. The detail surface has an ANSI background and aligns after the bullet.
6. Detail surfaces emit no border glyphs or separate border color.
7. Surface rows wrap and fill no farther than the configured progress width.
8. One unpainted blank row separates the summary and detail, and detail content
   has exactly one internal padding row above and below and one padding column
   on each side.
9. Script non-TTY output remains uncolored, retains the same block geometry as
   TTY output, and has no decorative borders.
10. The default repository verification passes.

## Risks and Open Questions

ANSI slots are user-configurable and may have weak contrast in unusual terminal
palettes. The implementation deliberately follows the existing terminal-native
color policy. There are no open questions.
