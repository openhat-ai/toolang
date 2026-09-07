# Show a Flow Outline in Script Help

Status: Approved for implementation with authored-doc-first rows and truncation.

## Goal and Success Criteria

Let users inspect a flow's stages before supplying input. Both explicit flow
`--help` and help displayed for incomplete input show the same static outline,
without preparing an agent or starting execution.

## Scope and Design

- Append a `Flow outline` section after the existing Arguments and Options
  sections of a selected script flow's help. Preserve usage, flow-level docs,
  options, input handling, and exit codes (`--help`: 0; incomplete input: 2).
  A flow's own doc comment remains its overall description below Usage, shown
  once before its arguments and options.
- Enumerate `FlowDecl.stmts` in source order, including content assignments and
  positional selection. Use zero-based `[N]` ordinals matching progress.
- When a non-empty authored `##` exists, put its normalized single-line text
  on the numbered first line, then the automatic statement description on a
  second line aligned after `[N] `. Without a doc, show only the numbered
  automatic description. Render generated descriptions dim and authored docs
  at normal intensity, so complete docs customize the outline's main narrative.
  Preserve generated runnable names. Extract the existing automatic wording
  into `statement_description()` and retain `statement_header()` as the
  unchanged doc-first wrapper used by progress.
- Expand `RepeatStmt.stmts` once beneath its header, indent two spaces per
  level, and restart ordinals within each block. Do not unroll iterations.
  The existing generated repeat description includes its count and condition;
  no separate runtime `until` boundary is shown.
- Keep runnable calls as single entries. Do not traverse referenced flows,
  perform additional callee resolution, include prompt bodies, or evaluate
  templates or predicates after normal source parsing and validation.
- For an empty typed `FlowDecl`, show `No statements.`. The source grammar
  still requires a non-empty flow body; this fallback does not change it.
- Render descriptions and docs as literal text. Keep each on one physical
  line and truncate overflow at the available terminal width with an ellipsis.
  Preserve indentation; brackets in docs and names must not become Rich markup.
  Show every stage even when its individual lines are truncated.
- Agic help, the script-level Runnables list, successful execution, progress
  rendering, and statement description wording remain unchanged.

For `too examples/deep_search.too research --help`, the section contains:

```text
Flow outline
  [0] Set value to topic
  [1] Expand the research question into diverse search queries
      Scatter into 6 items with expand_queries
  [2] Search the web for each query
      Map each item with search_web, up to 4 at once
  [3] Keep evidence bundles that answer the research task
      Keep items where is_relevant is true
  [4] Prioritize the strongest evidence
      Sort items by relevance in descending order
  [5] Keep the first 8 items
  [6] Extract source-backed findings from each evidence bundle
      Map each item with extract_findings, up to 4 at once
  [7] Synthesize the final research brief
      Gather all items into one with synthesize_report
```

## Implementation Touchpoints

- `src/toolang/cli/toolang/commands/script.py`: pass the selected flow to the
  existing `_RunnableCommand`; extend its help formatter to append the outline
  after normal Typer help. Keep AST traversal and outline rendering in small
  private helpers here. Do not reconstruct option help or alter dispatch.
- `tests/unit/cli/test_script_command.py`: cover the new help section through
  `dispatch()` and retain the existing invocation and help contract tests.
- `src/toolang/cli/common/execution_progress/headers.py`: extract the automatic
  description function without changing existing progress headers or wording.
- `tests/unit/cli/test_execution_progress_projector.py`: verify automatic
  descriptions remain independent of docs and progress still prefers docs.

## Acceptance Tests

1. Explicit flow help and incomplete-input help contain each stage exactly
   once, in source order, after Options; they retain their current exit codes.
   A flow-level doc appears once as its overall description before Arguments.
2. Docs, missing/blank docs, named/inline/implicit runs, bindings, lane limits,
   and positional selection use the existing automatic wording. Authored docs
   appear first with the ordinal; generated descriptions appear underneath,
   aligned after the ordinal. Missing or blank docs produce one numbered row.
3. Nested repeats show each body once with block-local ordinals and preserved
   nesting; fixed, conditional, and bounded conditional repeats are covered.
4. An empty typed flow has the stated placeholder. A call to another flow is one
   item even if that target contains nested or recursive calls.
5. Narrow, TTY, and non-TTY output truncate each long description/doc to one
   physical line with an ellipsis, including wide Unicode characters. All
   stages remain visible, indentation is preserved, and literal brackets and
   generated names are not interpreted as markup.
6. Agic help and script-level help have no outline. Explicit `--help` does not
   read stdin; all help paths avoid `_run` and runtime preparation. A valid
   invocation still executes normally without printing the outline.
7. The default offline verification passes: Ruff lint and format checks,
   `uv run ty check`, and `uv run pytest`.

## Risks and Open Questions

Typer's Rich epilogue collapses single newlines and strips paragraph indentation;
do not encode the outline as an epilogue string. Extend the existing command's
help formatting without patching Typer internals. Long or nested outlines may
increase help length; per-line truncation limits width. No unresolved design
questions remain.
