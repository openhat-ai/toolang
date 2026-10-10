# Talk message layout

Approved presentation for interactive Talk.

## Design

- Agent message bodies use Chat's Markdown renderer and terminal palette. Human
  message bodies are literal text on the input background.
- Sender markers share the name row. Names align with the message body's text.
  Agent names and `•` markers share a stable ANSI color derived from the name,
  with bold names and no dimming. Human headers use the cyan `▮` marker.
- A faint `┄` rule in dim ANSI bright-black ends each agent message block,
  immediately after its last visible body row. Trim trailing empty Markdown
  rows, including bottom code padding, while preserving interior spacing and
  styles. The rule starts at the body's left inset and reaches the message's
  right edge. Block spacing follows the rule; human messages have no rule.
- Left-aligned messages fill the configured content width. Own messages align
  right with a gutter. Human bodies have two-cell horizontal padding and one
  blank row above and below; all messages retain their final spacing.
- Preserve complete Unicode names and wrap them when necessary. Narrow terminals
  reduce decoration to preserve content. The content width is capped by the
  terminal and `TOOLANG_PROGRESS_MAX_WIDTH` (default 120).

## Ownership and acceptance

`src/toolang/cli/toolang/commands/talk/rendering.py` constructs message blocks,
using the shared Markdown renderer and Rich's `Rule`.

Verify header/body alignment, rule placement and right edge, stable name/marker
colors across processes, human backgrounds, Unicode wrapping, Markdown layout,
terminal control removal, and configured width limits. Use segment-style and
terminal-cell assertions in Talk's rendering tests, then run the default checks.
