# Implement Tool Call Terminal Blocks

## Work Type and Approval

Feature implementation. The human approved this behavior on 2026-08-22 by
requesting an implementation worktree and pull request with the decisions
below.

## Goal and Success Criteria

Make completed tool calls easier to scan by visually separating their summary
from their result or error. The feature succeeds when tool results and errors
use a compact rectangular surface, all summaries remain plain, and the display
obeys the shared progress width limit in both Chat and Script.

## Scope and Design

- Render all tool summaries as ordinary progress rows.
- Begin every Tool Step summary with one unpainted blank row in colored
  terminal output, keeping adjacent Tool Steps separated.
- Render succeeded results and failed errors on the tool-detail surface.
- Keep the tool-detail surface borderless.
- Keep the normal two-cell continuation indent, matching model code-block
  alignment.
- Separate the summary from the detail surface with one unpainted blank row in
  colored terminal output.
- Pad detail content by one empty row above and below and one empty column on
  the left and right.
- Fill each detail row to the available width, bounded by
  `TOOLANG_PROGRESS_MAX_WIDTH` (120 by default), and wrap long content inside
  that width.
- Share terminal-owned ANSI slot 8 with the Chat control bar and input box for
  the detail background, with ANSI foregrounds that preserve failure tone.
- Preserve plain, unpadded, color-free non-TTY output.
- Keep compact parallel-lane summaries unchanged; they remain one-line lane
  content rather than expanding into cards.
- Do not change execution events, durable records, tool summaries, result
  serialization, or web/API presentation.

## Touchpoints

- `src/toolang/cli/common/execution_progress/types.py`
- `src/toolang/cli/common/execution_progress/step_projection.py`
- `src/toolang/cli/common/execution_progress/rich_rendering.py`
- `src/toolang/cli/toolang/commands/chat/rendering.py`
- `tests/unit/cli/test_execution_progress_projector.py`
- `tests/unit/cli/test_chat_tui.py`
- `tests/unit/cli/test_script_run_presenter.py`
- `docs/execution-presentation.md`

## Acceptance Tests

1. Running and canceled tool summaries retain plain styling, and every Tool
   Step summary begins after one unpainted blank row in colored terminals.
2. Succeeded and failed summaries remain plain and have no background.
3. Results and errors use the detail surface.
4. The detail surface has an ANSI background and aligns after the bullet.
5. Detail surfaces emit no border glyphs or separate border color.
6. Surface rows wrap and fill no farther than the configured progress width.
7. One unpainted blank row separates the summary and detail, and detail content
   has exactly one internal padding row above and below and one padding column
   on each side.
8. Script non-TTY output remains uncolored and has no padded trailing cells or
   decorative borders.
9. The default repository verification passes.

## Risks and Open Questions

ANSI slots are user-configurable and may have weak contrast in unusual terminal
palettes. The implementation deliberately follows the existing terminal-native
color policy. There are no open questions.
