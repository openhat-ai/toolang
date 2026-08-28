# Chat TUI Version Caption

Status: Proposed.

## Goal and success criteria

Move the Chat TUI process identity from the metadata grid into the banner frame
caption so the three logo rows align directly with executor, sandbox, and home
metadata. This definition supersedes only the Toolang/version placement and
styling in `chat-tui-runtime-banner.md`.

The change succeeds when:

- the rounded panel's top border caption is exactly `Toolang <version>`, where
  `<version>` is the existing complete `HeaderBlock.version_label` value;
- `Toolang` and the version use the same normal-intensity terminal-default
  foreground style, without bold, dim, or the logo accent color;
- the metadata grid contains only `executor`, `sandbox`, then `home`, preserving
  its current dim keys, normal values, dim separators, endpoint hyperlink, and
  value-folding behavior;
- the wide layout top-aligns those three metadata rows with the three logo rows;
- the narrow layout keeps the logo, one blank separator row, then the three-row
  metadata grid;
- the caption remains complete and the panel does not exceed the available
  width in supported narrow renders; and
- the logo, border, internal vertical padding, surrounding blank lines, runtime
  identity rules, status bar, transcript, and chat behavior remain unchanged.

## Presentation and behavior

The embedded wide form is:

```text
╭─ Toolang v0.2.7-111-gacfe34d6 ────────────────────────╮
│                                                           │
│  ████           ██    executor  embedded                │
│   ██   ⬤   ⬤    ██    sandbox   host · macOS 27.0 arm64 │
│   ██          ████    home      ~/.toolang/agents/eve  │
│                                                           │
╰───────────────────────────────────────────────────────────╯
```

`HeaderBlock` continues to receive the version label and structured executor
metadata. It passes a single `Text("Toolang <version>")` caption to the existing
rounded `Panel`; the caption is not duplicated in the content grid. Rich's
normal panel-title placement and border gap are retained. The dim border style
must not make the caption dim: the caption explicitly uses normal intensity and
the terminal's default foreground without assigning a color.

Responsive sizing no longer counts a `Toolang` key or the process version as a
metadata row. It still chooses the wide layout only when the logo, field gap,
and longest complete metadata value fit. The complete caption must also fit the
panel width; if the terminal is too narrow for the caption and existing panel
padding, the render may use Rich's normal border-title truncation, but must not
wrap, clip panel edges, or add a second version label. Existing value folding
continues to handle long executor, sandbox, and home values.

## Scope and implementation touchpoints

- `src/toolang/cli/toolang/commands/chat/blocks.py`: move the Toolang/version
  text from the details table to the `Panel` title, apply the caption style, and
  update responsive width calculation for the three remaining metadata rows.
- `tests/unit/cli/test_chat_tui.py`: cover the exact caption, normal default
  style, metadata order and styles, wide row alignment, narrow stacking,
  padding, and the absence of a duplicate Toolang/version row.
- `tests/system/cli/test_chat_tui_e2e.py`: retain embedded and remote startup
  coverage; no behavior change is expected.

No client protocol, runtime metadata, executor-version suppression, sandbox
description, path abbreviation, logo helper, panel shape, command, or public
flag changes are in scope.

## Acceptance tests

1. Embedded, remote host, and remote Docker banners each render exactly one
   `Toolang <version>` string in the top border caption.
2. Caption segments are neither bold nor dim and have no explicit foreground
   color; logo blocks and dots retain their existing bright-cyan styles.
3. Metadata keys stay dim, values stay normal, separators stay dim, and remote
   endpoints retain their hyperlink style.
4. At wide widths, `executor`, `sandbox`, and `home` align with logo rows one,
   two, and three respectively; the panel retains one empty row above and below.
5. Narrow and long-value renders stay within the requested width, preserve all
   metadata, and do not duplicate the process version.
6. Focused Chat TUI tests and the default repository verification pass.

## Risks and open questions

Rich can inherit panel border styling into titles unless the caption carries an
explicit normal style. Width calculations can also regress if they continue to
reserve a removed metadata row or fail to account for the caption. Focused
segment-style and narrow-width tests mitigate both risks.

Open questions: none.
