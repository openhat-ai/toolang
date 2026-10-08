# Text agent header dividers

Status: approved in chat on 2026-10-08 for an implementation PR and visual review,
including follow-up requests for lighter rules below names and full-width left messages.

## Goal and scope

Make adjacent agent messages easier to distinguish in Interactive Text. Place a
subtle horizontal rule between each agent's sender-name row and message body.
Let left-aligned messages use the full configured content width.

## Design

- Put the light dashed glyph `┄` on a separate row immediately after the complete sender header,
  including wrapped names. Start at the body's left inset and extend to the
  message area's right edge, without trailing padding.
- Use dim ANSI bright-black (gray) for a faint rule in light and dark themes. Preserve
  the name and marker's placement, shared stable ANSI color, and non-dim styling.
- Render the rule even at narrow widths; never shorten a name for decoration.
  Add exactly one rule row, preserving body paragraphs and the final message gap.
- Remove the opposing gutter for all left-aligned messages, including human
  bubbles. Fill the width bounded by the terminal and configured content limit.
  Retain the existing gutter and bubble sizing for right-aligned own messages.
- Human headers have no rule. Preserve body padding, backgrounds, and Markdown
  styling. Reuse Rich's `Rule` in the Text renderer; do not change shared Markdown,
  transport, CLI options, or message persistence.

## Touchpoints and acceptance

- `src/toolang/cli/toolang/commands/text/rendering.py`: construct message blocks.
- Text rendering tests: verify rules below names, flush right edges, full-width
  left bodies and human backgrounds, and unchanged right human bubbles.
- Verify dim gray rules, preserved name/marker colors, full Unicode
  and wrapped names at narrow widths, body spacing, and configured width limits.
- Update `CHANGELOG.md` through `too aide.too update_changelog`.
- Run the default lint, formatting, type, and offline test checks.

The main risks are name truncation, overflow, and accidental dim/color inheritance;
width and segment-style assertions cover these. No open questions.
