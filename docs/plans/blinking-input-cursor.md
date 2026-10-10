# Blinking Input Cursor

## Approved Scope

The user selected a blinking block after reviewing the existing Chat and Talk
beam cursor and available terminal cursor shapes, then requested a pull request.
Use this shape in both interactive applications to make the cursor more visible.

## Design

Use `CursorShape.BLINKING_BLOCK` at the existing application constructors in
`src/toolang/cli/toolang/commands/chat/tui.py` and
`src/toolang/cli/toolang/commands/talk/tui.py`. Keep terminal rendering and cursor
restoration with prompt_toolkit. No new configuration or input behavior.
Update the existing cursor lifecycle checks in
`tests/system/cli/test_scrollback_resize.py` and the cursor descriptions in
`docs/chat.md` and `docs/messaging.md` to match.

## Acceptance and Risks

- Both application configurations request a blinking block cursor.
- Cursor lifecycle checks confirm the requested shape after startup, clear, and
  resize, and terminal-default restoration on exit for both applications.
- Existing input, focus, layout, and terminal tests and default checks pass.
- A supported terminal displays a blinking block while composing input;
  terminal preferences can override the requested appearance or blinking.

There are no open design questions.
