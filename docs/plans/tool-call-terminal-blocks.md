# Implement Tool Call Terminal Blocks

## Work Type and Approval

Feature implementation. The human approved this behavior on 2026-08-22 by
requesting an implementation worktree and pull request with the decisions
below.

## Goal and Success Criteria

Make completed tool calls easier to scan by visually separating their summary
from their result or error. The feature succeeds when terminal tool calls use
two compact rectangular surfaces, retain the existing live and cancellation
presentation, and obey the shared progress width limit in both Chat and Script.

## Scope and Design

- Keep running and canceled tool summaries as ordinary progress rows.
- Render succeeded and failed summaries on the tool-summary surface.
- Render succeeded results and failed errors on the tool-detail surface.
- Frame the tool-detail surface with straight left and right borders and a
  square-cornered bottom border. The summary surface acts as the visual top, so
  the detail does not add a separate top border.
- Keep the bullet outside the surface and use the normal two-cell continuation
  indent, matching model code-block alignment.
- Fill each colored row to the available width, bounded by
  `TOOLANG_PROGRESS_MAX_WIDTH` (120 by default), and wrap long content inside
  that width.
- Use terminal-owned ANSI backgrounds: slot 8 for summaries and slot 0 for
  details, with ANSI foregrounds that preserve failure tone.
- Preserve plain, unpadded, color-free non-TTY output.
- Keep compact parallel-lane summaries unchanged; they remain one-line lane
  content rather than expanding into cards.
- Do not change execution events, durable records, tool summaries, result
  serialization, or web/API presentation.

## Touchpoints

- `src/toolang/cli/common/execution_progress/types.py`
- `src/toolang/cli/common/execution_progress/step_projection.py`
- `src/toolang/cli/common/execution_progress/rich_rendering.py`
- `tests/unit/cli/test_execution_progress_projector.py`
- `tests/unit/cli/test_chat_tui.py`
- `tests/unit/cli/test_script_run_presenter.py`
- `docs/execution-presentation.md`

## Acceptance Tests

1. Running and canceled tool summaries retain their existing plain rows.
2. Succeeded and failed summaries use the summary surface.
3. Results and errors use the detail surface.
4. Both surfaces have distinct ANSI backgrounds and align after the bullet.
5. Detail borders use `│`, `└`, `─`, and `┘`, and count toward the configured
   progress width.
6. Surface rows wrap and fill no farther than the configured progress width.
7. Script non-TTY output remains uncolored and has no padded trailing cells or
   decorative borders.
8. The default repository verification passes.

## Risks and Open Questions

ANSI slots are user-configurable and may have weak contrast in unusual terminal
palettes. The implementation deliberately follows the existing terminal-native
color policy. There are no open questions.
