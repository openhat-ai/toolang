# Tool-owned progress descriptions

## Goal and scope

Make tool activity readable in Chat and Script progress without changing model
protocols, events, records, tool execution, or historical replay. This definition
is approved in the implementation discussion.

- Running, successful, and canceled tools occupy one line; failed tools occupy
  two, with an indented error on the second line. Truncate long lines.
- Remove all tool-result blocks. Keep results in records and model messages.
- Keep Step markers unstyled: `•` for models, `›` for ordinary tools, and `✧`
  for runtime helpers. Use dim text for all tool summaries and red error details;
  preserve model Markdown and styling.
- Preserve compact timing, hidden intercepted workspace calls, child/flow
  hierarchy, and root footers.
- Group token usage and cost with a space, not a fact separator.
- Show `kind:name` without the module in Chat run-control and status bars;
  retain complete runnable references in requests, events, and stored snapshots.

## Design

Leaf tools may override `summary(arguments, result=None) -> str | None` as defined
in [the toolset protocol](toolset-protocol.md). No result means running; result
errors distinguish failure from success. Cancellation uses executor wording.
The hook returns plain text
from call/result data only: no I/O, markers, colors, elapsed time, or wrapping.
It is separate from the model-facing `ToolDefinition.description`.

The function-tool factory and `LoadedTool` forward this optional hook. Executor
supplies isolated data with existing sensitive-argument masking, uses its generic
fallback for absent/empty/failed descriptions, and persists the resulting text
in existing given/noted summaries. Recorded summaries remain usable without
recomputation. Plugins migrate to the Tool protocol; formatting cannot affect
execution outcomes.

Fs, shell, and runtime helpers use this same path. Honor has no extra `files`
display input: running text uses available call data; completed text identifies
rules files from result controls. Remove executor-specific runtime wording and
the presentation-only discovery callback.

Fs displays `workspace:/relative/path` (root: `workspace:/`); listing
`workspace://` displays “Listing/Listed workspaces”. Both existing URI and
separate workspace/path arguments work unchanged. Shell shows its command, not
stdout. Descriptions contain no result dumps or statistics. A nonzero shell exit
does not change Step status under this presentation-only change.

Honor uses “Loading rules...” and “Loaded rules: repo:/AGENTS.md”; completed
summaries list only files supplied by the result. Pick uses “Loading/Loaded
guidance: skill/name” or “service/name”, using the call's kind and resource ref.
Reload retains “Reloading/Reloaded agent state”. These are display labels, not
new tool input syntax. Local refs omit their storage scope in display labels;
remote refs retain their full identity. Previously persisted summaries are not
rewritten.

Footer facts separate duration, execution counts, and usage with ` · `; usage
includes both tokens and any visible cost, for example `↑12.2k ↓528 ≈$0.0003`.
Existing unknown/zero cost handling and cost visibility remain unchanged.
Chat bars omit the `module$` prefix before fitting labels to the terminal width.
Status compares these display labels when omitting a matching default label;
model names, reasoning, and runnable selection stay unchanged.

## Touchpoints and acceptance

- `base/types/tool.py`, `base/utils/function_tools.py`, toolset loading: optional
  hook, factory/wrapper forwarding, unchanged model definitions.
- Fs/shell/runtime toolsets and executor tool summaries/rules: lifecycle wording,
  fallback and masking, cancellation at begin/during/end, no I/O for descriptions.
- Shared progress projection/rendering: one/two physical lines at narrow widths,
  distinct normal markers, dim tool summaries, red error line, no result surfaces,
  compact timer cleanup, and unchanged model/flow output.
- `Metrics.facts` and Chat block/widget rendering: grouped usage/cost; full
  runnable refs render without modules in idle, active, and queued snapshots.
  Cover absent cost/usage, hidden cost, matching status labels, and narrow widths.
- Tests cover default hooks, formatter exceptions/None and mutation isolation,
  both workspace spellings/root paths, honor results and retries, cancellation,
  persisted replay, Chat/Script and parallel lanes. Verify that namespace labels
  never replace model-facing workspace URIs or resource refs.

Run the default lint, formatting, type, and offline test suite. Review for stale
runtime-specific wording branches and unused result-block code. No open product
decisions; plugin formatting is optional and presentation failure falls back.
