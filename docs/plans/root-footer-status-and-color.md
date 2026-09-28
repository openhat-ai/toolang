# Root Footer Status Marker and Color

## Goal and success criteria

Use a consistent `▪︎` marker in chat and script root-run footers, and distinguish outcomes through the caption color: success stays dim, failure is red, and cancellation is yellow.

## Scope

- Use `▪︎` for succeeded, failed, and canceled chat/script root-run footers.
- Keep the marker dim for every status.
- Keep the success caption dim; color failed captions red and canceled captions yellow.
- Preserve footer facts, spacing, wrapping, and width behavior.
- Leave non-root progress markers and unrelated status presentation unchanged.

## Design touchpoints

- `src/toolang/cli/common/execution_progress/rich_rendering.py`: use `▪︎` for root footers, dim the marker, and apply terminal status styling to the caption.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: consume the shared root-footer renderer.
- `tests/unit/cli/test_chat_tui.py`, `tests/unit/cli/test_script_run_presenter.py`, and `tests/unit/cli/test_agic_run_progress.py`: verify glyph, marker style, caption colors, and layout.
- `tests/system/cli/test_chat_tui_e2e.py`, `tests/integration/cli/test_script_remote_execution.py`, `tests/integration/cli/test_script_local.py`, `tests/integration/cli/test_local_core_commands.py`, and `tests/integration/cli/test_compact_command.py`: update root-footer output assertions.

## Acceptance criteria

- Chat and script root-run footers use `▪︎` for succeeded, failed, and canceled outcomes.
- The marker is always dim and has no outcome-specific color.
- The caption is dim for success, red for failure, and yellow for cancellation.
- Footer width and wrapping remain correct; non-root progress markers are unchanged.

## Risks

- The marker's variation-selector sequence must retain a consistent single-cell width in supported terminals.
- Root-footer styling is shared by chat and script; tests cover both entry points and their output consumers.

## Open questions

None; surfaces, symbol, colors, and unchanged layout are specified.