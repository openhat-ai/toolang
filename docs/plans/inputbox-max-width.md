# Independent Input Area Width

## Status and Goal

Feature implementation, approved in the user request and scope clarification.
Allow Chat and Talk's lower input area to use a different maximum width from
message output, without changing the default layout or native scrollback.

## Contract

- `TOOLANG_INPUTBOX_MAX_WIDTH` accepts a positive integer, in terminal cells.
  Surrounding whitespace is accepted; empty, noninteger, and nonpositive values
  produce a configuration error naming the variable before interactive startup.
- When absent, use the resolved `TOOLANG_PROGRESS_MAX_WIDTH` (default 120).
- The limit covers Chat's Input, Queue, and both status bars, and Talk's input
  and footer. Message output and submitted controls keep the progress limit.
- Each area is left-aligned and capped by the current terminal width. The input
  limit may be either smaller or larger than the output limit.
- Resolve environment values at interactive command entry points and pass
  concrete limits to the TUI. Scripted Chat and noninteractive Talk ignore the
  input-only setting. No new flags or live environment reload are introduced.

## Touchpoints and Verification

- Shared input configuration: `src/toolang/cli/common/input.py`.
- Chat entry point and separate live-output/input layout: `chat/main.py`,
  `chat/tui.py`; Talk entry point and composer/footer: `talk/__init__.py`,
  `talk/tui.py`, under `src/toolang/cli/toolang/commands/`.
- Test default fallback, independent override, invalid values, CLI propagation,
  narrower/wider input areas, and terminal shrink/expand with a preserved draft.
  Retain the native-scrollback resize regression checks.
- Update Chat and messaging documentation and the generated changelog entry.

The principal risk is measuring wrapped input at the output width; layout,
height calculation, and cursor recovery must use the input area's actual width.
There are no open scope questions.
