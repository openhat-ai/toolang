# Chat TUI Banner

Status: Approved for implementation.

## Goal and success criteria

Replace the Chat TUI startup banner with the compact Toolang logo used by the
`info` command. Keep stable startup identity in the banner and leave the active
model in the status bar, where it can change during a chat.

The change succeeds when:

- the banner shows the color-neutral three-line logo beside `Toolang` and its
  exact version value, separated by one ordinary space;
- `home` appears above `executor`, and the current process-local chat path shows
  `executor  local`;
- the displayed home uses platform-native user-home and temporary-directory
  abbreviations without changing the resolved agent layout;
- no model value appears in the banner;
- wide terminals use a side-by-side layout and narrow terminals stack metadata
  below the logo without clipping;
- the current `info` logo and Chat TUI behavior remain otherwise unchanged.

## Scope and presentation

The rounded panel keeps a dim border, two columns of horizontal padding, and one
empty row above and below its content. The logo has no fixed color. Its solid
cells use reverse-styled spaces so adjacent rows join without glyph line gaps;
plain terminals fall back to block characters.
`Toolang` is bold bright cyan. Its exact `toolang_version()` value follows after
one space in the normal foreground rather than participating in key/value
alignment. The `home` and `executor` keys are dim, and their values use the
normal foreground. The metadata order is Toolang/version, home, then executor.

At 69 columns or wider, the logo and metadata render side by side when the
longest metadata value fits without wrapping:

```text
╭────────────────────────────────────────────────────────╮
│                                                        │
│  ████           ██    Toolang 0.3.0+cd50c7f*           │
│   ██   ⬤   ⬤    ██    home      ~/.toolang/agents/eve  │
│   ██          ████    executor  local                  │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

Below 69 columns, or whenever the wide layout would wrap, metadata stacks below
the logo. Before rendering, paths under the user home use `~`; platform
temporary-directory roots use `/tmp`, a matching environment variable such as
`$TMPDIR` or `%TEMP%`, or the native temporary root while retaining the relative
path below that root. Other paths remain absolute. Long paths fold within the
value column. One blank line remains after the panel before the transcript.

HTTP ChatClient implementation and endpoint presentation are out of scope.
When that client is added, it can replace the `local` value with its sanitized
endpoint without changing this banner layout.

## Design touchpoints and acceptance tests

- `src/toolang/cli/common/output.py`: expose the compact art through a neutral
  shared logo helper while preserving the info view.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: own responsive header
  composition and styling.
- `src/toolang/cli/toolang/commands/chat/tui.py`: remove model resolution from
  header construction while retaining it for the status bar.
- `src/toolang/cli/toolang/commands/chat/main.py`: shorten the selected home for
  display without changing the resolved layout path.
- `tests/unit/cli/test_output.py`, `tests/unit/cli/test_chat_command.py`, and
  `tests/unit/cli/test_chat_tui.py`: cover path abbreviations, shared logo
  identity, both layouts, content order, wrapping, and styles.
- `tests/system/cli/test_chat_tui_e2e.py`: retain local TUI startup coverage.

Acceptance requires focused banner tests and the default repository verification
to pass. Risks are accidental changes to the info logo, removal of status-bar
model resolution, and terminal-width errors around the responsive threshold.

## Open questions

None.
