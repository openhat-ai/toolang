# Text agent header dividers

Status: approved in chat on 2026-10-08 for an implementation PR and visual review.

## Goal and scope

Make adjacent agent messages easier to distinguish without adding vertical
spacing. Add a subtle horizontal rule to each agent's existing sender-name row
in Interactive Text. Human message headers, backgrounds, and message bodies
retain their current presentation.

## Design

- Extend `─` from the agent name to the edge of the body text area, separated
  from the name by one space. Mirror the rule before the name for right-aligned
  messages. Keep the existing marker placement and horizontal padding.
- Use the terminal's default foreground with dim styling for the rule. Preserve
  the name and marker's shared, stable ANSI color and non-dim styling.
- Omit the rule when the available text width cannot fit the full name, one
  space, and at least one rule character. Retain existing name wrapping; never
  shorten a name to make room for decoration. Measure names in terminal cells.
- Reuse Rich's `Rule` in the Text renderer; do not change shared Markdown,
  transport, CLI options, or message persistence.

## Touchpoints and acceptance

- `src/toolang/cli/toolang/commands/text/rendering.py`: construct agent headers.
- Text rendering tests: verify left/right rule placement and width, dim default
  foreground, preserved name/marker colors, Unicode and narrow-width fallback,
  and unchanged body rows, spacing, and human headers.
- Update `CHANGELOG.md` through `too aide.too update_changelog`.
- Run the default lint, formatting, type, and offline test checks.

The main risks are name truncation and accidental dim/color inheritance; the
width and segment-style assertions must cover both. No open questions.
