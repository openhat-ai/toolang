# Define Terminal-Adaptive Chat Surfaces

## Work Type and Status

Feature definition. Approved for implementation. This document defines the
implementation scope and does not change product behavior.

This definition supersedes the fixed shared ANSI-slot-8 surface decisions in
`terminal-native-markdown-colors.md` and `tool-call-terminal-blocks.md` for the
interactive Chat TUI. It does not change Script presentation.

## Goal and Success Criteria

Give the interactive Chat TUI distinct Input, Queue, and Code backgrounds that
follow the active terminal theme without fixing ordinary text colors. The
feature succeeds when:

- Input, Queue, and Code are the only public surface-background names;
- Input is shared by the input box and Run, Steer, and Quick Command bars;
- Queue is visually distinct from the adjacent Input area;
- Code is only subtly distinct from the terminal background while retaining a
  visible block boundary on near-black themes;
- normal and dim text inherit the terminal foreground on every surface;
- the input cursor remains visible through reverse video;
- an explicit color scheme bypasses terminal discovery;
- an unspecified scheme uses OSC 10/11 when a safe query succeeds and a fixed
  dark palette otherwise; and
- terminal probing never consumes pending user input or leaves terminal mode
  changed.

## Scope

In scope:

- one dependency-free terminal-surface resolver owned by the CLI;
- `TOOLANG_COLOR_SCHEME` parsing and validation;
- a bounded OSC 10/11 foreground/background query before Prompt Toolkit starts;
- deterministic light, dark, and theme-derived Input, Queue, and Code colors;
- Chat input, queue, cursor, Run/Steer/Quick Command bars, tool-detail blocks,
  and fenced Markdown code backgrounds;
- removal of fixed ordinary-text foregrounds from those Chat surfaces;
- offline unit, pseudo-terminal, renderer, and Chat integration tests; and
- current execution-presentation documentation.

Out of scope:

- `COLORFGBG`, terminal-product detection, shell-theme inspection, or OS
  appearance settings;
- terminal opacity detection or configuration;
- live theme-change watching after Chat starts;
- changing branding accents, error/warning colors, or terminal ANSI syntax
  token colors;
- changing Script, non-interactive Chat, web, API, stored output, or transcript
  semantics;
- a general user theme system; and
- compatibility aliases for previous surface names or color-scheme ordering.

## Surface Vocabulary

The implementation exposes one immutable palette with exactly these background
tokens, in this order:

1. `input_background`
2. `queue_background`
3. `code_background`

Input and Queue are peer areas in the user-input panel. Run, Steer, and Quick
Command bars reuse `input_background`; they are not a fourth surface. A focused
Queue selection continues to reuse Input's background. Tool results and fenced
Markdown code reuse `code_background`.

No compatibility aliases or additional public surface vocabulary are introduced
in code or documentation.

## Resolution Policy

The interactive Chat entry point resolves the palette once, before Prompt
Toolkit or another keyboard reader owns stdin, in this order:

1. A non-empty `TOOLANG_COLOR_SCHEME` value.
2. An OSC 10/11 query when stdin and stdout are the same usable TTY.
3. The fixed dark palette.

The resolver does not read `COLORFGBG`. An empty `TOOLANG_COLOR_SCHEME` is
equivalent to it being absent. Explicit API foreground/background parameters
are internal test and integration seams, not additional user configuration.

### Explicit Schemes

`TOOLANG_COLOR_SCHEME` accepts:

- `dark` or `light`, case-insensitive after surrounding whitespace is removed;
  or
- exactly three comma-separated `#RRGGBB` colors in
  `input,queue,code` order.

Examples:

```sh
TOOLANG_COLOR_SCHEME=dark toolang chat
TOOLANG_COLOR_SCHEME=light toolang chat
TOOLANG_COLOR_SCHEME='#1f1f1f,#121212,#0b0b0b' toolang chat
```

`light` and `dark` select the fixed palettes below without querying the
terminal. A three-color value is used exactly, without foreground/background
inference, contrast adjustment, or OSC. Any other non-empty value fails before
the TUI starts with an actionable configuration error that states the accepted
keywords and `input,queue,code` order.

There is no support for alternate ordering, foreground and background pairs,
named ANSI colors, or partial overrides.

### OSC Query

With no explicit scheme, the resolver requests the terminal's default
foreground and background using OSC 10 and OSC 11. The probe:

- requires POSIX terminal controls, TTY stdin and stdout referring to the same
  terminal, and no input already pending;
- sends both queries together, accepts BEL- or ST-terminated replies, and
  requires valid replies for both colors;
- accepts the standard `rgb:R/G/B` form with one to four hexadecimal digits per
  channel and scales it to 8-bit RGB;
- uses a bounded 350-millisecond timeout;
- temporarily enters cbreak mode and restores the original terminal attributes
  on success, timeout, invalid data, operating-system error, and interruption;
- emits no diagnostics or other output; and
- returns the fixed dark palette for unsupported, incomplete, invalid, or
  failed queries.

The resolver never sends a query after Prompt Toolkit starts. It does not try
to consume or repair late terminal replies after a timeout; callers that cannot
reserve the startup input stream use the dark fallback directly.

## Palette Derivation

Detected terminal RGB values derive each surface by mixing the terminal
background toward its foreground in linear RGB. Target contrast ratios against
the terminal background are:

| Surface | Target |
| --- | ---: |
| Code | 1.05, raised to 1.07 on a near-black background |
| Queue | 1.12 |
| Input | 1.28 |

The intended ordering is Code < Queue < Input in visual strength.
Mixing is capped when necessary so inherited terminal foreground text retains
at least 4.5:1 contrast, or the terminal's original foreground/background
contrast when that is already lower. When Input reaches that cap, the three
strengths are compressed together instead of being clamped independently, so
their ordering remains distinct where RGB quantization permits. Code's
near-black floor prevents RGB rounding from erasing its boundary on black
Terminal Pro and iTerm2 themes.

The canonical fallbacks are:

| Scheme | Input | Queue | Code |
| --- | --- | --- | --- |
| Dark | `#1f1f1f` | `#121212` | `#0b0b0b` |
| Light | `#e3e3e3` | `#f2f2f2` | `#f9f9f9` |

These values are the deterministic results of deriving from white-on-black and
black-on-white defaults. The fixed dark palette is the only automatic fallback
when OSC is unavailable. A light terminal without working OSC must opt into
`TOOLANG_COLOR_SCHEME=light` or supply the three final colors.

OSC reports configured RGB colors but not terminal opacity. Derived and
explicit surface fills are RGB cells and may appear more opaque than the
terminal's default background; no transparency claim is made.

## Rendering Behavior

- Input background covers the complete input box and the non-accent cells of
  Run, Steer, and Quick Command bars.
- Queue background covers its complete width in expanded and collapsed states.
  A focused selected entry uses Input background as today.
- Code background covers the existing padded rectangles for tool details and
  fenced Markdown code without changing their shape, width, wrapping, or gaps.
- Ordinary Input, Queue, control-bar, tool-result, and base code text assign no
  foreground color. Dim labels and placeholders use only the terminal dim
  attribute.
- The input cursor uses reverse video instead of fixed black/white colors.
- Run bright-cyan, Quick Command yellow, and Steer purple accents remain fixed
  branding colors. Existing red/yellow error and warning meanings remain.
- Fenced-code syntax tokens and other established semantic styles remain named
  terminal ANSI colors; no new fixed text RGB values are introduced.
- Rich and Prompt Toolkit receive the same concrete background RGB values so a
  live block and its stable scrollback form do not change color.

## Design Touchpoints

- Add `src/toolang/cli/common/terminal_surfaces.py` for the immutable palette,
  explicit-scheme parser, RGB math, OSC reply parser, bounded probe, and
  resolution diagnostics. Importing it performs no I/O.
- Update `src/toolang/cli/toolang/commands/chat/main.py` to resolve environment
  and terminal ambiguity at the interactive call site before TUI startup, then
  pass one concrete palette into Chat.
- Update `src/toolang/cli/toolang/commands/chat/tui.py` and `presenter.py` to own
  and pass the palette to widgets and renderable blocks without global mutable
  theme state.
- Update `src/toolang/cli/toolang/commands/chat/widgets.py`, `rendering.py`, and
  `blocks.py` to consume Input and Queue backgrounds, inherit ordinary text
  foregrounds, use a reverse cursor, and retain branding accents.
- Update `src/toolang/cli/common/execution_progress/rich_rendering.py` and
  `src/toolang/cli/common/human_values.py` to accept a concrete Code background
  from Chat. Existing Script callers retain their current presentation without
  probing the terminal.
- Add focused resolver tests under `tests/unit/cli/` and update
  `tests/unit/cli/test_chat_tui.py` plus shared progress renderer tests.
- Update `docs/execution-presentation.md` to describe adaptive Chat surfaces,
  the environment format, OSC/dark fallback, normal/dim text, and reverse
  cursor using only Input, Queue, and Code vocabulary.

## Acceptance Tests

1. `dark` and `light` bypass OSC and produce the exact fixed palettes above.
2. `#INPUT,#QUEUE,#CODE` parses in Input, Queue, Code order, preserves the exact
   normalized colors, and bypasses both derivation and OSC.
3. Empty configuration permits OSC; malformed keywords, counts, delimiters, or
   RGB values fail with the accepted format and ordering in the error.
4. Complete OSC 10/11 fixtures for black, near-black, light, and tinted themes
   produce distinct Code, Queue, Input backgrounds with the expected target
   contrast and readable inherited foreground.
5. Missing, partial, malformed, timed-out, non-TTY, non-POSIX, mismatched-TTY,
   pending-input, and operating-system-error probes return the fixed dark
   palette without consulting `COLORFGBG`.
6. Pseudo-terminal tests prove success, timeout, partial response, pending
   input, and interruption restore terminal attributes and do not consume
   pre-existing input.
7. Input and all three control bars use Input background while retaining their
   existing accent colors and normal text foreground.
8. Queue uses Queue background across both edge cells and all modes; its
   focused selection uses Input background; labels and hints use normal/dim
   styling without fixed foreground RGB.
9. The input cursor is reverse-styled and no fixed cursor foreground or
   background remains.
10. Tool details and fenced code use Code background in live and stable Chat
    output while retaining their existing rectangular geometry, padding,
    wrapping, and semantic error/warning or syntax styles.
11. Chat surface rendering introduces no fixed ordinary-text foreground, and
    live Prompt Toolkit fragments and stable Rich output use identical surface
    RGB values.
12. Script and non-interactive output behavior remains unchanged.
13. Manual smoke checks in macOS Terminal Basic, macOS Terminal Pro, and iTerm2
    confirm OSC-derived colors; no-OSC comparisons confirm dark fallback and
    explicit light/custom overrides.
14. The default repository verification passes.

## Risks

- OSC replies share stdin with keyboard input. Querying once before Prompt
  Toolkit, rejecting pending input, bounding the wait, and restoring terminal
  state reduce but cannot eliminate the possibility of an unsupported
  intermediary delivering a late reply.
- macOS Terminal Basic becomes unreadable if OSC is blocked and the user does
  not select the light fallback. The behavior is intentional and documented
  because guessing from `COLORFGBG` proved unreliable.
- User-supplied colors can have poor contrast. Exact triples deliberately bypass
  inference; the resolver validates syntax, not accessibility.
- Rich and Prompt Toolkit have separate style systems. Passing one palette and
  asserting live/stable RGB parity prevents drift.
- Shared progress rendering also serves Script. Palette injection must remain a
  Chat concern so Script does not acquire startup queries or color changes.

## Open Questions

None.
