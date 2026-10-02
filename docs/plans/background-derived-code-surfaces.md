# Background-derived code surfaces

Status: approved through the inline contrast 1.15 visual review and the request
to implement it in a pull request, extended by the explicit request to make
Script and Chat consistent. This supersedes the code-surface decisions in
[terminal-adaptive-chat-surfaces.md](terminal-adaptive-chat-surfaces.md).

## Goal and scope

Make inline code distinguishable without adding spaces or changing wrapping.
Chat and Script code backgrounds depend only on the terminal background, independently of
its default foreground. Input/queue colors, text attributes, and ANSI syntax
highlighting retain their existing behavior.

## Design

- Mix the original terminal background toward the black or white endpoint with
  greater contrast headroom, in linear RGB.
- Block contrast is 1.05, or 1.07 when background luminance is at most 0.005.
  Inline contrast is 1.15. Do not clamp either using default text contrast.
- Add a concrete `inline_code_background` to `TerminalSurfaces`. Fixed dark/light
  schemes and the dark fallback use #151515/#efefef for inline backgrounds.
- Preserve the three-color environment configuration: its explicit code color
  controls both block and inline backgrounds, with no probe or derivation.
- Carry both backgrounds through Chat live, committed, and durable `/output`
  rendering. Rich retains ownership of Markdown text styling.
- Shared render functions accept an optional inline override, falling back to
  the block background for existing callers.
- Script local/remote execution and retry/rerun resolve the same palette once
  at command orchestration, before starting run output, using loaded runtime
  environment and stderr as the output stream. Quiet execution does not probe.
- Script's presenter and console receive concrete surfaces without performing
  terminal I/O. All live and committed Markdown paths pass both backgrounds
  and inherit the terminal foreground, matching Chat. TTY output uses truecolor;
  non-TTY output remains uncolored. The shared resolver only probes when stdin
  and stderr are the same TTY with no pending input.
- Retain the existing input/queue derivation, including its historical weak
  surface reference used for compression and quantization.

## Touchpoints

`terminal_surfaces.py` owns palette derivation and configuration. Script and
thread command entry points resolve colors for the Script presenter/console. Chat's TUI,
presenter, and blocks pass colors into `human_values.py` and shared execution
progress rendering. Focused terminal-surface, Markdown, and Chat tests cover the
policy and propagation. Update `docs/execution-presentation.md` to describe
the code background policy. The local comparison script remains an experiment and
is not part of the production change.

## Acceptance checks

- Equal backgrounds produce equal code colors with different foregrounds,
  including low-contrast black and white themes.
- Quantized colors approximate the contrast targets; inline is stronger than
  block. Black produces #0b0b0b/#151515; white produces #f9f9f9/#efefef.
- Input/queue outputs remain unchanged, including low-contrast/tinted themes.
- Paragraph, list, quote, and table inline spans receive only the background
  override. Fences use their separate background, wrapping/text are unchanged,
  and the console theme is restored after rendering.
- Live, committed, and durable slash outputs propagate the selected colors.
- Script and Chat render identical Markdown colors with dark, light, custom,
  and detected palettes, in both live and committed paths.
- Script CLI wiring honors loaded environment, probes its stderr terminal once,
  and avoids probing quiet/non-TTY output. Invalid configuration fails before
  starting a run. Explicit three-color values still control both code colors.
- Run all default repository checks before commit and PR handoff.

## Risks and open questions

Neutral mixing may reduce saturation. Code text contrast is not guaranteed by
this background-only policy; terminal-owned ANSI colors still determine text
readability. Code surfaces need not remain weaker than foreground-capped queue
surfaces. HTML previews approximate the terminal ANSI palette. No open questions.
