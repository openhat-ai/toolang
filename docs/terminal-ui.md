# Terminal UI

Script, Chat, and command output share terminal geometry and Markdown styles.
This guide owns terminal mechanics. [Execution presentation](execution-presentation.md)
owns progress projection and semantic rows; [Chat](chat.md) owns session controls,
queue interaction, and status. [CLI](cli.md#terminal-titles-and-pane-marks) owns
terminal titles and navigation metadata.

## Output ownership

Script uses one event-driven Rich Live area per root Run. Chat uses prompt_toolkit
as the terminal owner, renders the same Rich Markdown values, commits stable
fragments into native scrollback, and retains only replaceable fragments live.
Resize repaints live content without reconstructing or deleting scrollback.
Live updates preserve draft, focus, selection, and queue state.

Non-TTY output is append-only and ANSI-free. It preserves semantic rows and
complete values while omitting live replacement, cursor operations, and palette
discovery. Quiet mode suppresses progress; it does not hide actionable errors.

## Geometry

Widths are measured in terminal display cells. Execution and command output use
the smaller of the attached terminal width and `TOOLANG_PROGRESS_MAX_WIDTH`
(default 120). Non-TTY Script uses that configured maximum. Input, Queue, and
status bars use `TOOLANG_INPUTBOX_MAX_WIDTH`, falling back to the progress limit.
Invalid or empty settings fail at the client boundary.

Semantic markers and wrapped continuations retain their alignment. Prose wraps;
resource tables elide flexible cells to keep rows intact. Individual surfaces
choose their degradation order when space is scarce; the shared width limit
does not merge their mutable state.

## Palette and Markdown

Ordinary text inherits terminal foreground/background. Semantic accents use
named ANSI colors so the terminal theme controls their actual color. Chat
preserves those ANSI identities through live rendering and scrollback. Code
blocks and inline code receive distinct derived backgrounds; block padding and
trailing cells form one rectangle.

Both clients resolve one palette before interactive input or run output.
`TOOLANG_COLOR_SCHEME` accepts `dark`, `light`, or three `#RRGGBB` values in
`input,queue,code` order. An explicit value bypasses discovery; an explicit code
color applies to both fenced and inline code.

Otherwise bounded OSC 10/11 queries read terminal foreground/background only
when stdin and the output stream refer to the same TTY and no input is pending.
Incomplete or unsupported replies fall back to the dark palette; `COLORFGBG` is
not consulted. Quiet Script and noninteractive/non-TTY paths do not probe.
The shared resolver derives backgrounds with bounded contrast and cannot infer
terminal opacity. Exact palette and contrast constants belong to the resolver,
not separate client copies.

## Implementation evidence

- [Surface resolver](../src/toolang/cli/common/terminal_surfaces.py), [surface tests](../tests/unit/cli/test_terminal_surfaces.py)
- [Markdown tests](../tests/unit/cli/test_markdown_rendering.py), [Chat TUI tests](../tests/unit/cli/test_chat_tui.py)
