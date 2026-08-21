# Define Terminal-Native Markdown Colors

## Work Type and Status

Feature definition. Approved for implementation on 2026-08-21 after revision
to use an ANSI code surface, then expanded by human confirmation to align the
Chat input and control-bar background with that surface. This document does
not implement product code.

## Verified Current Behavior

- Model output Markdown is rendered by the shared Rich presentation used by
  Script and Chat.
- Rich Markdown defaults fenced code to the RGB-based `monokai` syntax theme
  and inline code to `bold cyan on black`.
- Most other Rich Markdown styles use named ANSI colors, but Chat converts
  every Rich color through `Color.get_truecolor()`. This changes terminal ANSI
  colors such as `red` into fixed RGB values before prompt-toolkit sees them.
- Chat constructs its Rich rendering console as truecolor, renders stable
  scrollback with `ColorSystem.TRUECOLOR`, and fixes the prompt-toolkit
  application to 24-bit color depth.
- Script TTY presentation uses Rich's standard color system. Non-TTY Script
  output contains no ANSI color.
- Fenced code currently retains one rectangular set of padded rows, including
  authored blank lines, when a background style is present.

## Goal and Success Criteria

Make model output Markdown inherit the user's terminal palette instead of
assuming one RGB theme. The feature succeeds when:

- normal Markdown text uses the terminal's default foreground and background;
- all Markdown semantic and syntax colors use named ANSI palette entries or
  terminal defaults, never fixed RGB values;
- fenced and inline code use a consistent dark ANSI surface that remains
  independent of the terminal's default background;
- the Chat input and control bars use the same ANSI surface as code; and
- Chat live output, Chat stable scrollback, and Script TTY output preserve the
  same color identities.

## Design Decisions

### Terminal Palette Is the Theme Authority

Toolang will not identify terminal products or named themes. It will emit
terminal-default colors and ANSI palette names, leaving the terminal to map
those names to its configured theme.

The implementation must not query OSC 11, inspect `COLORFGBG`, or infer theme
brightness from terminal names. Those mechanisms are absent or unreliable in
many local, remote, multiplexed, and embedded terminals and would make input
handling part of Markdown rendering.

No new Toolang theme setting is introduced. In particular, this feature does
not add dark, light, Pygments-theme, or RGB overrides.

### Markdown Styles

The shared Rich console theme will override the Markdown styles that currently
assume fixed colors:

- paragraphs and ordinary text use the terminal defaults;
- inline code uses bold ANSI slot 15 text on ANSI slot 8 background;
- fenced code uses ANSI slot 15 as its base foreground and ANSI slot 8 as its
  background;
- existing headings, lists, block quotes, links, rules, and tables continue to
  use Rich's named ANSI colors and text attributes.

The Chat Rich-to-prompt-toolkit bridge will preserve color types:

- Rich `ColorType.DEFAULT` becomes prompt-toolkit `ansidefault`;
- Rich `ColorType.STANDARD` becomes the corresponding prompt-toolkit
  `ansi...` color name;
- indexed and RGB colors remain representable for non-Markdown Chat controls,
  but Markdown must not produce them.

### Fenced-Code Syntax Palette

Fenced code will reuse Rich's `ansi_dark` syntax-token mapping, overriding only
its base foreground and background:

- ANSI slot 8 (`bright_black` / prompt-toolkit `ansibrightblack`) is the code
  surface;
- ANSI slot 15 (`bright_white` / prompt-toolkit `ansiwhite`) is the base code
  foreground; and
- language tokens use the ANSI colors and attributes defined by `ansi_dark`.

The pair must be applied together. Retaining the terminal-default foreground
would put a light theme's dark text on the dark slot-8 surface. The selected
pair intentionally makes fenced code a dark surface in both light and dark
terminal themes while leaving the actual RGB values under terminal control.

Fenced code retains its existing full-width rectangular padding and blank-row
continuity. ANSI slot 8 is a convention rather than a semantic surface color,
so the design optimizes for common terminal palettes rather than guaranteeing
contrast for arbitrary user-defined slot values. Toolang will not replace this
heuristic with fixed RGB or terminal-color queries.

### Related Chat Surfaces

The Chat input and start, steer, and quick-command control bars use ANSI slot 8
as their background. Rich and prompt-toolkit use different names for the same
slot: `bright_black` and `ansibrightblack`, respectively. The implementation
keeps both backend-specific names and verifies that the Rich-to-prompt-toolkit
bridge maps them to the same color identity.

This alignment does not change queue, cursor, status-error, control-accent, or
foreground colors. Those remain separate presentation choices.

### Output Paths

Chat may retain `ColorDepth.DEPTH_24_BIT` and its truecolor internal Rich
console for the existing non-Markdown controls. Prompt-toolkit emits explicit
`ansi...` foreground and background names as their palette-slot SGR codes at
every color depth, so preserving those identities is sufficient for Markdown
to remain terminal-native.

Stable Chat scrollback may continue to render Rich styles through its current
ANSI writer. Named ANSI and default colors must remain SGR palette/default
codes rather than `38;2` or `48;2` sequences. Script continues to use the
standard Rich color system for TTY output and no color for non-TTY output.

## Scope and Touchpoints

In scope:

- `src/toolang/cli/common/execution_progress/rich_rendering.py`: own the
  slot-8/slot-15 fenced-code renderer derived from Rich's `ansi_dark` mapping
  and instantiate shared Markdown with it.
- `src/toolang/cli/common/script_progress/console.py`: install the shared Rich
  Markdown style overrides without changing non-TTY behavior.
- `src/toolang/cli/toolang/commands/chat/rendering.py`: install the same Rich
  overrides, preserve default and ANSI color identities in prompt-toolkit
  styles, and align the input and control-bar background with ANSI slot 8.
- `tests/unit/cli/test_chat_tui.py` and
  `tests/unit/cli/test_script_run_presenter.py`: cover color identity,
  code-block shape, live/stable parity, and non-TTY output.
- `docs/execution-presentation.md`: document terminal-native Markdown colors
  and the dark ANSI code-surface contract.

Out of scope:

- the fixed Chat queue, cursor, status-error, control-accent, and foreground
  palette;
- automatic terminal-background detection;
- user-selectable Toolang or Pygments themes;
- full-TUI `NO_COLOR` behavior;
- Markdown parsing, streaming partitioning, wrapping, widths, or syntax
  recognition; and
- web, API, stored output, or transcript presentation.

## Acceptance Tests

1. A shared Markdown fixture containing headings, emphasis, a list, a quote, a
   link, inline code, and fenced code renders without RGB foreground or
   background colors.
2. Rich default colors map to `ansidefault` in prompt-toolkit rather than
   black, and all 16 Rich standard colors map to their corresponding prompt-
   toolkit ANSI names.
3. Chat live fragments for Markdown contain `ansi...` and `ansidefault` color
   references rather than hexadecimal colors.
4. Stable Chat Markdown emits ANSI/default SGR codes and contains no truecolor
   `38;2` or `48;2` sequences.
5. Fenced code keeps equal-width top padding, content rows, authored blank
   rows, and bottom padding; every cell uses ANSI slot 8 as its background.
6. Fenced code uses ANSI slot 15 as its base foreground and Rich `ansi_dark`
   token colors without emitting RGB.
7. Inline code uses bold ANSI slot 15 text on ANSI slot 8 background.
8. Script TTY output uses the same Markdown color identities, while Script
   non-TTY output remains color-free and does not gain padded trailing spaces.
9. Chat input and control bars use ANSI slot 8, and Rich control bars map to the
   same prompt-toolkit `ansibrightblack` background as the input and code.
10. Existing Markdown text, spacing, wrapping, live/stable equivalence, and PTY
   behavior remain unchanged apart from color styling.
11. The default repository verification passes.

## Risks

- A user can configure ANSI slots 8 and 15 with poor mutual contrast. Toolang
  cannot correct arbitrary palette relationships without overriding the user's
  theme; this feature relies on their conventional dark/light roles.
- A dark code surface inside a light terminal is deliberately more prominent
  than a theme-derived subtle surface. Avoiding that contrast would require
  reliable terminal-color discovery or an explicit light/dark setting.
- User-defined slot 8 values may reduce contrast with the fixed Chat control
  accents or foregrounds; this feature deliberately leaves those colors
  unchanged.
- Chat has two output routes. Updating only the live prompt-toolkit bridge or
  only stable ANSI scrollback would make colors change when content commits.
- Mapping Rich `default` through its truecolor fallback would turn it into
  black, which is unreadable on dark terminals; this requires an explicit
  regression test.

## External Basis

- Rich documents `ansi_dark`, `ansi_light`, and the terminal-default
  background as terminal-configured alternatives to fixed Pygments colors:
  <https://rich.readthedocs.io/en/stable/syntax.html>.
- prompt-toolkit documents that `ansi...` names map directly to the terminal's
  16-color palette and that color depth is selectable by the output or
  `PROMPT_TOOLKIT_COLOR_DEPTH`:
  <https://python-prompt-toolkit.readthedocs.io/en/3.0.52/pages/asking_for_input.html>.

## Open Questions

None.
