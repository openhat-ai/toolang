# Tool-owned progress descriptions

## Goal and scope

Make tool activity readable in Chat and Script progress without changing model
protocols, events, records, tool execution, or historical replay. This definition
is approved in the implementation discussion.

- Running, successful, and canceled tools occupy one line; failed tools occupy
  two, with an indented error on the second line. Truncate long lines.
- Remove all tool-result blocks. Keep results in records and model messages.
- Keep Step markers unstyled. Preserve model Markdown and ordinary tool styles;
  use cyan for runtime helper descriptions and red for errors.
- Preserve compact timing, hidden intercepted workspace calls, child/flow
  hierarchy, and root footers.

## Design

Leaf tools may implement `describe(arguments, status, output=None) -> str | None`.
Status is running, succeeded, failed, or canceled. The hook returns plain text
from call/result data only: no I/O, markers, colors, elapsed time, or wrapping.
It is separate from the model-facing `ToolDefinition.description`.

The function-tool factory and `LoadedTool` forward this optional hook. Executor
supplies isolated data with existing sensitive-argument masking, uses its generic
fallback for absent/empty/failed descriptions, and persists the resulting text
in existing given/noted summaries. Old plugins and recorded summaries remain
usable without recomputation. Formatting cannot affect execution outcomes.

Fs, shell, and runtime helpers use this same path. Honor has no extra `files`
display input: running text uses available call data; completed text identifies
rules files from result controls. Remove executor-specific runtime wording and
the presentation-only discovery callback.

Fs displays `[workspace] relative/path` (root: `[workspace] /`); listing
`workspace://` displays “Listing/Listed workspaces”. Both existing URI and
separate workspace/path arguments work unchanged. Shell shows its command, not
stdout. Descriptions contain no result dumps or statistics. A nonzero shell exit
does not change Step status under this presentation-only change.

## Touchpoints and acceptance

- `base/types/tool.py`, `base/utils/function_tools.py`, toolset loading: optional
  hook, factory/wrapper forwarding, unchanged model definitions.
- Fs/shell/runtime toolsets and executor tool summaries/rules: lifecycle wording,
  fallback and masking, cancellation at begin/during/end, no I/O for descriptions.
- Shared progress projection/rendering: one/two physical lines at narrow widths,
  normal markers, cyan helpers, red error line, no result surfaces, compact timer
  cleanup, and unchanged model/flow output.
- Tests cover old plugins, formatter exceptions/None and mutation isolation,
  both workspace spellings/root paths, honor results and retries, cancellation,
  persisted replay, Chat/Script and parallel lanes.

Run the default lint, formatting, type, and offline test suite. Review for stale
runtime-specific wording branches and unused result-block code. No open product
decisions; plugin formatting is optional and presentation failure falls back.
