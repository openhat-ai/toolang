# Execution Progress Presentation State Machine

## Status

Feature definition. Implementation starts only after human approval. This plan
supersedes the larger proposal in pull request #275 and uses the execution
contracts now present on `main`.


## Goal and Success Criteria

Give script mode and the Chat TUI one terminal-independent interpretation of
ordered execution events while keeping terminal mechanics surface-specific.
The shared component is a presentation reducer, not another executor.

The feature succeeds when:

- both surfaces derive the same ownership, activity, output, aggregates,
  metrics, and errors from the same event sequence;
- completed blocks append in completion order and live output is replaced
  atomically;
- ordinary Agic work stays flat while parallel live height is bounded by lane
  count;
- each causal error has one visible owner;
- optional statement and iteration boundaries can be removed without changing
  the underlying Agic Step stream;
- rendering never queries durable storage; and
- deterministic reducer and golden-output tests cover this plan.


## Verified Foundation and Missing Dependency

Current `main` already provides:

- ordered native `RunEvent` values for Run, Step, and Part lifecycles;
- dot-separated `StepPath` values such as `run_id.0.1`;
- `StepKind` values `run`, `agent`, `human`, `model`, `tool`, `par`, `loop`,
  and `value`, with no system Step;
- typed `StepBegin.given` as `FlowStmt`, `ModelStepGiven`, or `ToolStepGiven`;
- typed model-only `StepEnd.noted`;
- typed `Occurrence` item, lane, and body/until iteration positions; and
- `ExecutionError` as either a concrete string or a pointer to a failed Run or
  Step.

Script and chat still duplicate semantic routing in
`src/toolang/cli/common/script_progress/tracer.py` and
`src/toolang/cli/toolang/commands/chat/presenter.py`, with partial shared state
in `src/toolang/cli/common/execution_progress/state.py`.

One executor behavior is missing. A failed parallel child currently makes the
par Step point to the failed child Run. The par Step must instead own a concrete
boundary error, for example
`parallel step stopped because lane 1 (#5) failed`. Enclosing Steps and the Run
then point to the par Step, while the child keeps its original concrete error.
This needs no event or storage schema change.

An inline Repeat until condition has only a generated runnable reference, not
an independent `FlowStmt` or `doc`. Generated conditions therefore use a fixed
presentation fallback; no AST change is required.


## Scope

In scope:

- a shared live presentation reducer for script and chat;
- presentation-safety validation and bounded preview state;
- root and child Agic or Flow Run ownership;
- Model, Tool, Flow, Repeat, and parallel formatting;
- AST-derived Flow statement headers;
- causal error ownership and the par boundary error above;
- TTY, non-TTY, and Chat TUI sink contracts; and
- deterministic acceptance tests.

Out of scope:

- durable-history reconstruction and inspect output;
- authored syntax or scheduling changes;
- full executor-semantic or cardinality validation in the presenter;
- root Run summary, cost, and control redesign;
- visible result pointers, child closure, binding effects, or control decisions;
- new Stage, Iteration, statement, or presentation events.


## Reducer Contract

The shared reducer accepts one ordered native event and returns:

```text
ProgressUpdate
  stable: zero or more newly finalized semantic blocks
  live: the complete current live snapshot
```

`stable` is an append-only delta in completion order. `live` is a full
replacement. A sink atomically clears the prior live area, appends `stable`,
and draws the new snapshot. There are no imperative show, update, clear, or
finalize effects and no logical-position insertion.

The reducer retains only:

- root lifecycle;
- active Runs by Run ID and active Steps by `StepPath`;
- active Parts by `(StepPath, part)`;
- nearest visible owner and current bounded output preview;
- one row per active parallel lane;
- emitted statement and iteration boundary keys;
- aggregate metrics; and
- concrete errors needed to resolve pointers observed in the current run tree.

Finalized display detail moves to `stable` and is released when no active
pointer or aggregate needs it. Stable transcript storage belongs to the sink.

The reducer validates only presentation safety: unique begin, matching end,
matching Step kind, active Part ownership, and no events after root termination.
It does not revalidate output shape, binding, lane scheduling, or iteration
limits. A malformed stream clears live state and emits one root-level error row.


## Visible Ownership

1. Root and non-parallel child Agic Runs expose their Model and Tool Steps.
2. Non-parallel child Flow Runs expose their statements and descendant Agic
   Steps directly.
3. Statement and iteration headers are optional flat boundaries, not visual
   indentation owners.
4. The nearest par Step replaces every descendant owner with one lane. Child
   Agic Runs, child Flow Runs, nested statements, and nested Repeat work update
   only that lane.
5. A Flow Step owns a row only for its own activity, aggregate, value output,
   transform error, cancellation, or par boundary error.
6. A Run with no Step-owned error may own one root-level error row.

This keeps a child Flow inside parallel work from escaping the bounded lane
area, closing the ownership gap in #275.


## Output Grammar

`·` is the only execution marker. `↳`, `→`, and `!` are removed.

- Ordinary Agic and Flow primary rows start with `·` in column 0.
- Errors use the same glyph with error styling.
- Output, error detail, Step path, duration, tokens, cost, and other annotations
  are unmarked continuations aligned two spaces after the marker.
- Parallel lanes alone embed an Agic `·` after their lane columns.
- Wrapping may create visual continuations but no additional semantic marker.
- Result pointers, child closure, binding decisions, and control decisions are
  not displayed.

Typed `noted` facts are formatted directly without a visible `noted` label:

```text
· executed Use a shared presentation reducer.
  run_research.1 · 2.6s · deepseek-chat · 4.8k/120 tokens
```


## Statement Headers

A header is derived from its typed `FlowStmt`:

1. Use non-empty `doc`, collapsing whitespace to one logical line without
   rewriting its words or punctuation.
2. Otherwise use the deterministic fallback table below.
3. Preserve authored runnable names exactly, including underscores, case, and
   qualification: `search_web` remains `search_web`.
4. Hide generated `<agic:...>` names and use the inline fallback.
5. For value statements other than `LetStmt`, omit binding `_`; append
   `and save as NAME` for a named binding or `without saving the result` for an
   explicit `None` binding. `LetStmt` already names its binding in `Set NAME`.
6. Render an explicit lane limit as `, up to N at once`.
7. Put exactly one blank line after every statement or control header.

| Flow AST | No-doc fallback |
| --- | --- |
| `LetStmt` | `Set NAME` |
| named/inline `RunStmt` | `Run RUNNABLE` / `Run the inline task` |
| named/inline `SeekStmt` | `Ask AGENT to run RUNNABLE` / `Ask AGENT for help` |
| `AskStmt` | `Ask for human input`, or `Ask NAME for input` |
| named/inline `ScatterStmt` | `Expand into N items with RUNNABLE` / `Expand into N items` |
| named/inline `StormStmt` | `Run RUNNABLE N times` / `Generate N items` |
| named/inline `GatherStmt` | `Combine the items with RUNNABLE` / `Combine the items` |
| named/inline `SettleStmt` | `Reduce the items with RUNNABLE` / `Reduce the items` |
| named/inline `MapStmt` | `Run RUNNABLE for each item` / `Process each item` |
| positional `KeepStmt` | `Keep the first/last N items` |
| predicate `KeepStmt` | `Keep items selected by RUNNABLE` |
| positional `DropStmt` | `Drop the first/last N items` |
| predicate `DropStmt` | `Drop items selected by RUNNABLE` |
| `RankStmt` | `Rank items with RUNNABLE`, plus `and keep the top/bottom N` |
| count-only `RepeatStmt` | `Repeat N times` |
| bounded/unbounded Repeat with until | `Repeat up to N times` / `Repeat until complete` |

Use normal singular/plural forms. Keep this presentation helper separate from
the source-like `toolang.lang.format_statement_head()`.

```text
[2] Search the web for each query

· running · 4 active
```

Without `doc`:

```text
[2] Run search_web for each item, up to 4 at once

· running · 4 active
```

Emit a header only if its statement has visible Agic work, Flow output, or an
aggregate. Never emit an orphan header.


## Agic and Flow Steps

Model activity is live; successful Model output replaces it on the same line:

```text
· thinking… Comparing the available approaches…

· executed Use a shared presentation reducer.
  run_research.0 · 1.8s · deepseek-chat · 3.4k/86 tokens
```

A Model error replaces inline output:

```text
· failed provider returned status 429
```

Tool output occupies the next unmarked line outside parallel work:

```text
· executed web_search.search
  5 results
  run_research.1 · 820ms · exit 0
```

A Tool error occupies the same output position:

```text
· failed web_search.search
  provider returned status 429
```

A Flow `run`, child Run, or Repeat until Run is only a boundary. It exposes its
real Agic Steps and never synthesizes `· executed RUNNABLE`. A Flow Step uses a
primary row only for output it owns:

```text
[0] Set topic

· executed "agent runtimes"
  run_research.0 · 3ms
```

A Flow-owned error uses its normal output slot:

```text
[2] Expand into 6 items with expand_queries

· failed
  scatter requires a list result
```

All previews use bounded one-line or shape summaries. Model output remains on
its primary logical line; Tool and Flow output use one unmarked logical line.


## Repeat

The Repeat header appears once. Every visible iteration uses a one-based flat
boundary and body headers use zero-based authored positions:

```text
[3] Repeat up to 3 times

--- iteration 1 of 3 ---

[0] Run inspect_sources

· executed inspect_sources
  12 sources

[1] Run summarize

· thinking… Comparing the source claims…
```

Body ordinals reset each iteration. The reducer assigns them from ordered body
Step begins grouped by `(repeat StepPath, iteration index)` because durable
nested Step indices continue increasing across iterations.

If body statement headers are enabled, the iteration boundary is also enabled.
Compact mode removes both and leaves only the flat Agic stream.

The until phase is another optional boundary:

```text
<?> completion_check

· thinking… Checking whether the result is complete…
```

Preserve an authored `RepeatStmt.runnable` after `<?>`; for a generated
condition use `<?> Check whether to stop`. Show real inner Tool and Model Steps,
including `· executed true` or `· executed false`, but no until decision or
synthetic wrapper row.

Repeat completion may own one aggregate:

```text
· completed · 2 iterations
  6 runs succeeded · 8.4s · 7 model calls
```

An iteration error stays at its actual Agic or Flow owner. Repeat and Run error
pointers normally remain silent.


## Parallel Work

Parallel projection covers par Steps such as storm, map, predicate keep/drop,
and rank. Each active lane is exactly one logical line:

```text
· running · 4 succeeded · 3 active
  0 | #4 | · thinking… Comparing source claims…
  1 | #5 | · executing web_search.search…
  2 | #6 | · executed Source summary prepared
```

- lane and `#item` indices are zero-based and dynamically column-aligned;
- lane reuse replaces the row and retains no completed lane history;
- Model output, Tool output, error, and nested Flow activity stay inline;
- Tool output uses `· executed web_search.search · 5 results`;
- lane rows omit annotations and truncate to available width; and
- a child Flow contributes only its deepest useful activity or bounded final
  summary, never nested headers or rows.

On success, atomically clear lanes and stabilize the aggregate, output, and
facts:

```text
· 7 succeeded
  7-item list
  run_research.2 · 6.4s · 7 model calls · 28.2k/940 tokens
```

A local failure first appears transiently in its lane:

```text
· running · 4 succeeded · 1 failed · 2 canceling
  0 | #4 | · executed web_search.search · 5 results
  1 | #5 | · failed fetch_page · provider returned status 429
  2 | #6 | · canceling…
```

The terminal update clears all lanes and stabilizes only the par aggregate and
its independent boundary error:

```text
· 4 succeeded · 1 failed · 2 canceled
  parallel step stopped because lane 1 (#5) failed
```

The par error identifies the first local failure that caused termination.
Other failures or cancellations contribute to counts but do not replace that
causal location. Lane error and par error never coexist in one snapshot.


## Error Ownership

Deduplicate by identity and pointers, never by message text:

1. Render a concrete Step error at that Step's output position.
2. Treat a parent pointer as propagation only; update status without another
   error row.
3. Treat the par boundary string as a new par-owned error; enclosing pointers
   remain silent.
4. Render a concrete Run error with no Step owner as one final error-styled
   row containing the message directly:

   ```text
   · progress stream ended before run completion
   ```

5. Render independent concrete errors separately even when their text matches.

Resolve pointers only from errors observed in the current ordered run tree. An
unresolved or cyclic pointer at terminal shutdown becomes one root-level
presentation diagnostic and clears live state.


## Sink Contracts

- Script TTY clears the old live area, writes stable blocks to stderr, redraws
  live rows, and leaves stdout to the command's final-value policy.
- Script non-TTY ignores live snapshots and appends stable blocks without
  cursor controls. Every terminal error and aggregate must therefore stabilize.
- Chat maps stable blocks to scrollback and replaces its execution live blocks,
  while submission and steer blocks remain chat-owned.

All sinks calculate wrapping and styling, but none reinterpret events.


## Implementation Touchpoints

After approval:

1. Add reducer, semantic blocks, ownership, metrics, and header formatting under
   `src/toolang/cli/common/execution_progress/`.
2. Change parallel child failure handling under
   `src/toolang/execution/executor/` to create the par-owned boundary error.
3. Adapt `src/toolang/cli/common/script_progress/tracer.py` as a script sink.
4. Adapt `src/toolang/cli/toolang/commands/chat/presenter.py` as a chat sink.
5. Remove superseded shared and surface state after both sinks use the reducer.

Land the reducer and tests first, then script, chat, and duplication removal.


## Acceptance Tests

Reducer and golden tests must cover:

- Run, Step, and Part lifecycle safety and state release;
- Model and Tool live replacement, output, error, and typed annotations;
- full dot-separated Step paths;
- `doc` precedence and every no-doc AST fallback, including exact runnable-name
  preservation, binding modifiers, generated-name hiding, and lane wording;
- non-parallel child Agic and Flow Runs and Flow-owned value/transform output;
- Repeat boundaries, body ordinal reset, compact mode, until variants, and no
  wrapper completion rows;
- lane reuse, width alignment, one-line truncation, nested Flow compaction, and
  bounded live height;
- atomic parallel success, cancellation, and lane-error-to-par-error failure;
- pointer propagation without duplicate rows and ownerless Run errors;
- out-of-order completion with completion-order stable blocks; and
- equivalent script and chat semantic rows before surface styling.

Sink tests cover TTY redraw, non-TTY stable-only output, narrow widths, and
shutdown with active live content. Default verification is required:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```


## Risks

- Par-owned wrapper errors are an intentional exception; other parents must
  continue pointer-only propagation.
- Header copy must remain separate from `.too` source formatting.
- Parallel child Flow compaction hides detail from progress but preserves its
  durable Runs for inspection.
- Non-TTY mode omits activity, so every terminal outcome must become stable.


## Open Questions

None.
