# Chat Control-Bar Accent Glyph

## Goal and success criteria

Reduce the visual weight of run, steer, and quick-command control bars by replacing their accent-colored background cells with a foreground `▮` marker. The marker uses each bar's existing accent color; its background matches the surrounding control bar.

## Scope

- Apply the marker treatment to run, steer, and quick-command control bars only.
- Preserve bright cyan for run, bright magenta for steer, and yellow for quick commands.
- Preserve marker positions, bar dimensions, text, wrapping, and surrounding input background.
- Leave chat input, queue, and other UI accents unchanged.

## Design touchpoints

- `src/toolang/cli/toolang/commands/chat/blocks.py`: render control-bar accents as foreground glyphs on the input background.
- `src/toolang/cli/toolang/commands/chat/rendering.py`: define a control-bar marker without changing the shared blank `ACCENT_CELL` used by other surfaces.
- `tests/unit/cli/test_chat_tui.py`: verify foreground colors, surrounding background, marker positions, and unchanged layout for all three bar types.

## Acceptance criteria

- Run, steer, and quick-command bars show `▮` in their respective existing accent colors.
- The marker background matches the surrounding control-bar background; no accent-colored background cell remains.
- Existing bar widths, wrapping, and message rendering remain unchanged.
- Other chat surfaces retain their existing appearance.

## Risks

- `▮` must occupy one terminal cell so existing bar geometry remains stable; verify display width in tests.

## Open questions

None; marker, colors, affected bars, and surrounding background are specified.