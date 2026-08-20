# Execution Progress Projection

## Status

Approved feature definition. Implementation is in progress in pull request #283.
This plan supersedes the proposal closed with pull request #275.

## Goal

Give script mode and Chat one interpretation of ordered execution events while
keeping terminal and UI mechanics surface-specific.

The feature is complete when:

- the same event stream produces the same Step ownership, terminal output,
  aggregates, facts, and errors on both surfaces;
- ordinary leaf Steps progressively commit complete trace to scrollback;
- parallel work remains bounded to one live row per lane;
- each causal error is displayed once at its actual owner;
- Repeat and Settle share one typed loop presentation model; and
- projection is deterministic, storage-independent, and covered by tests.

## Vocabulary and Pipeline

```text
RunEvent -> ProgressProjector -> ProgressUpdate -> surface presenter
```

- `ProgressProjector` owns event validation, Run and Step lifecycle, nearest
  visible owner, boundary claims, metric aggregation, and pointer suppression.
- Step projection functions own Model, Tool, Flow, parallel-lane, and loop
  content. Their input is typed state and their output is semantic rows.
- `ProgressUpdate.committed` contains newly stable, append-only fragments in
  event order. One active Step may emit any number of committed fragments.
- `ProgressUpdate.live` is the complete replaceable live snapshot.
- `ScriptRunPresenter` and `ChatRunPresenter` adapt these updates to their
  surfaces. Wrapping, lane truncation, color, and live-area replacement remain
  surface concerns.
- Progress output is at most 120 cells wide by default and never wider than the
  TTY. `TOOLANG_PROGRESS_MAX_WIDTH` overrides the positive maximum for both
  surfaces; non-TTY Script output uses the configured maximum.

The projector never queries durable storage. It retains active state, streamed
Model source through authoritative Part closure, the uncommitted Markdown
suffix, one state record per active lane, aggregate metrics, and concrete
errors required to resolve pointers.

## Runtime Facts

`StepEnd.noted` supports these typed payloads:

- `ModelStepNoted` for model usage, price, cost, and provider state;
- `CollectionStepNoted` for total and output item counts; and
- `LoopStepNoted` for completed `iterations`, optional `total`, and
  `termination`.

`LoopStepNoted.termination` is one of:

- `exhausted`: all configured iterations completed;
- `satisfied`: the until condition became true;
- `failed`: execution stopped on an error;
- `canceled`: execution was canceled.

The payload must agree with `StepEnd.status`. Repeat and Settle both emit it.
Settle child Runs carry both an `item` occurrence for data provenance and a
typed `iteration` occurrence for loop presentation.

## Run and Step Model

A Run has no execution header. It contains projected Steps and, for the root
Run only, a footer.

A Step and its presentation fragments have independent lifecycles:

```text
Step:      active ------------------------------------------> terminal
Display:  committed header -> committed fragments + live tail -> committed tail
```

`committed` means safe to append to terminal scrollback; it does not mean the
owning Step is terminal. A Step may own committed fragments and one replaceable
live tail at the same time. `StepEnd` closes the tail and appends only terminal
content that was not already committed.

Every started Part must emit `PartEnd` before its owning `StepEnd`, including
failed and canceled Model streams. Every child Step and Run must likewise be
terminal before its parent Step ends.

For a streamed Model Text Part, concatenated `TextDelta` content must be an
exact prefix of `PartEnd.data.text`. `PartEnd` is the authoritative Part
closure, and a successful `StepEnd.output` must contain the same completed
Parts. A mismatch is a provider/executor contract violation.
When a streaming adapter emits `ModelPartEnd`, its Text Part is likewise
authoritative and must match the final `ModelCallResult` before the executor
emits the public `PartEnd`.

A Flow Step is any Step whose typed `given` value is a `FlowStmt`. Flow Steps
define statement headers and a facts slot. Model and Tool Steps have neither.
`executing` and `executed` are formatting words, not lifecycle statuses.

The nearest parallel Step determines the presentation mode:

- no parallel ancestor: `Trace`, preserving every leaf Step;
- parallel ancestor: `Lane`, limiting current descendant work to one row for
  that lane.

Repeat and Settle use the same `Loop` behavior. Each iteration applies the same
Trace-or-Lane ownership rule to its child statement.

## Marker Grammar

`•` is the execution row marker and starts in column zero outside a lane.
There is no `↳`, `→`, or `!` marker.

- Model activity and output use `•` and normal text.
- Tool activity uses `•` and normal text. Its successful terminal marker, row,
  and following output are dim.
- Flow live and successful terminal output, including collection and loop
  aggregates, use `•` and normal text.
- Errors use `•` with error styling.
- Parallel lanes embed `•` after their lane columns.
- Facts retain `·` as their inline separator; it is not a row marker.
- Flow facts and continuation lines are unmarked and align with the text after
  the marker.
- Headers and facts are dim. Errors and cancellation retain their status styles.

Terminal markers use the same color and intensity as their following content.
Successful Model and Flow outputs use normal white; successful Tool terminal
output uses dim white. Failure uses red and cancellation uses yellow. Green is
not a terminal status color.

Result pointers, child closure, binding effects, and control decisions are not
displayed. If such information is added later, it belongs below output as an
unmarked continuation.

## Flow Headers

Statement headers are Step boundaries followed by one blank line. Once a typed
Flow statement determines a header, the projector commits it immediately at
Step begin. Iteration and until headers are committed as soon as their typed
Occurrence is observed. Headers never enter live state and are not delayed
until child output or `StepEnd`.

Header generation is deterministic:

1. Use a non-empty `FlowStmt.doc`, collapsed to one logical line.
2. Otherwise generate a concise sentence from the typed AST.
3. Preserve runnable names exactly; do not split underscores or change case.
4. Hide generated `<agic:...>` names behind an inline-task fallback.
5. Append an explicit lane limit as `, up to N at once`.
6. Add binding wording only when it conveys authored behavior.

| Flow statement | No-doc header |
| --- | --- |
| `LetStmt` | `Set NAME` |
| `RunStmt` | `Run RUNNABLE` / `Run the inline task` |
| `SeekStmt` | `Ask AGENT to run RUNNABLE` / `Ask AGENT for help` |
| `AskStmt` | `Ask for human input` / `Ask NAME for input` |
| `ScatterStmt` | `Expand into N items with RUNNABLE` / `Expand into N items` |
| `StormStmt` | `Run RUNNABLE N times` / `Generate N items` |
| `GatherStmt` | `Combine the items with RUNNABLE` / `Combine the items` |
| `SettleStmt` | `Reduce the items with RUNNABLE` / `Reduce the items` |
| `MapStmt` | `Run RUNNABLE for each item` / `Process each item` |
| positional `KeepStmt` / `DropStmt` | `Keep/Drop the first/last N items` |
| predicate `KeepStmt` / `DropStmt` | `Keep/Drop items selected by RUNNABLE` |
| `RankStmt` | `Rank items with RUNNABLE`, optionally keeping top/bottom N |
| count-only `RepeatStmt` | `Repeat N times` |
| bounded/unbounded Repeat with until | `Repeat up to N times` / `Repeat until complete` |

Repeat iteration and condition boundaries are:

```text
--- iteration 1 of 3 ---

<?> completion_check

• thinking
• true
```

The condition is a Run, not a synthetic `executed completion_check` Step.

## Trace Output

In Trace mode, Model Text delta is append-only Markdown source. The projector
commits Markdown blocks once later input makes them stable and retains only the
current incomplete block as the replaceable live tail. Logical newlines and
Markdown structure are preserved; surface width controls rendering only. At
Part closure the remaining tail is committed, and `StepEnd` does not repeat the
same output:

```text
• thinking
• # Deep Research Brief
  ## Executive summary
  The evidence supports explicit ownership
```

The first rendered Model fragment owns `•`; every later fragment uses the same
two-space continuation alignment, so moving unchanged content from live state
to scrollback produces no visible text change. Active labels and streamed
content do not append an ellipsis. Committed and live fragments are independent
Markdown render units; syntax cannot reopen a committed fragment, and a later
reference definition affects only its own fragment.

When authored Markdown separates top-level blocks with a blank line, that gap
belongs to the following fragment as `gap_before`. The same leading gap is
rendered while the fragment is live and after it is committed, preserving both
Markdown readability and a visually unchanged transition. Multiple separator
lines collapse to one rendered blank line. Flow headers retain their existing
trailing boundary gap; Step facts remain directly below Step output.
Markdown horizontal rules use a dim Unicode `─` divider instead of Rich's
full-width ASCII hyphens.

Script renders the live Markdown tail through one event-driven
`rich.live.Live` per root Run. Stable fragments are printed through the same
Rich Console while Live redraws the remaining tail. Auto-refresh and stdio
redirection are disabled. Non-TTY Script output prints committed fragments only
and never emits cursor control.

Chat does not instantiate Rich Live because prompt_toolkit owns its terminal.
It reuses the same Rich Markdown renderable and committed/live fragments:
committed fragments enter Chat scrollback, while only the current tail remains
in the prompt_toolkit live container.

Inside a parallel lane, the same Model delta is flattened and truncated to the
lane's single physical row.

A Tool keeps its terminal action and complete output on following lines:

```text
• executing web_search.search
• executed web_search.search
  {"results":[{"url":"https://example.com"}]}
```

Structured Tool result output uses compact single-line JSON. Textual `stdout`
and `stderr` retain their original lines. Wrapping is a surface concern and does
not alter the result content.

A model Tool request is visible without creating a synthetic Tool result:

```text
• requested web_search.search
```

Model and Tool Steps do not display StepPath, duration, model name, exit code,
tokens, cost, or other Step facts.

A direct Flow-owned value is displayed directly. Positional collection
transforms instead describe their semantic result, such as `Kept the first 2
items out of 6` or `Dropped the last item out of 6, leaving 5`. A Flow `run`
with one child Run emits no synthetic success row; the absence of an error
means success. Flow output shape such as `1 item` or `6-item list` is never
displayed.

## Flow Facts

A Flow Step may append one unmarked facts row when it owns child execution:

```text
[2] Search the web for each query

• Mapped all 6 items in parallel
  31.0s · 6 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

StepPath is omitted. Undefined facts are omitted rather than synthesized.
Token usage and optional cost form one `↑INPUT ↓OUTPUT $COST` fact. Cost is
rounded to cents. Model, Tool, and value Steps currently define no facts.

Run facts appear only in the root Run footer and aggregate the whole Run tree.

## Parallel Output

Parallel live output is one aggregate row plus exactly one physical row per
lane. Surfaces truncate the lane row to available width; the projector retains
semantic content.

```text
• running · 4/18 succeeded · 3 active
  0 | #4 | • thinking
  1 | #5 | • executing web_search.search
  2 | #6 | • Source summary prepared
```

On success, live lanes are cleared and one natural-language result sentence
remains. It names both parallel execution and the statement transform:

- `Mapped all 7 items in parallel`
- `Brainstormed 7 items in parallel`
- `Evaluated 7 items in parallel, kept 5`
- `Evaluated 7 items in parallel, dropped 2, leaving 5`
- `Scored 10 items in parallel, kept the top 8`

The normal unmarked Flow facts row follows that sentence.

On failure, clear successful, active, and canceled lanes. Preserve each failed
lane and its complete causal output, then display an independent parallel-Step
boundary error and the Flow facts:

```text
• Parallel execution stopped: 4/18 succeeded, 1 failed, and 2 were canceled
  1 | #5 | • failed fetch_page
             provider returned status 429

• parallel step stopped because lane 1 (#5) failed
  31.0s · 7 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

The lane owns the original error. The parallel Step owns a distinct explanation
of why the aggregate Step terminated. Enclosing pointer failures remain silent.

## Loop Output

Repeat and Settle share iteration boundaries and terminal output. A successful
loop emits one of:

```text
• Completed all 3 iterations
• Condition met after 2 of 3 iterations
• Completed all 3 iterations without meeting the condition
• Settled all 6 items in 6 iterations
```

Failure and cancellation emit:

```text
• Interrupted after completing 2 of 3 iterations
• Canceled after completing 2 of 3 iterations
• Settling was interrupted after 2 of 6 iterations
• Settling was canceled after 2 of 6 iterations
```

Zero completed iterations use `before completing an iteration`; an empty
Settle uses `Settled no items`. Stable result sentences have no trailing period.

The causal child error is displayed at the child Step or lane. The loop row
describes termination and does not repeat that error.

## Error Ownership

Errors are visible once, as close as possible to where they occur:

- a concrete Step error occupies that Step's normal output position;
- a failed lane preserves its concrete error;
- a parallel Step adds only its distinct local-failure boundary error;
- parent Steps and Runs whose errors are pointers emit nothing;
- a concrete Run error with no Step owner is emitted as a final error-like Step
  row: `• MESSAGE`.

There is no special diagnostic marker. A malformed presentation stream also
clears live state and emits one root-owned `• MESSAGE` row.

## Root Run Footer

Script and Chat display no Run header or output shape. After projected Steps
they render the same root footer:

```text
▴ run_nrqpt0mf succeeded ─────────────────
  1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
```

The divider uses a solid `▴` marker. Its line is 42 cells wide: the marker and
space occupy two cells, and the caption plus trailing rule fill the remaining
40. Narrow terminals shorten the divider without wrapping it. The marker,
caption, and rule use dim, red, and yellow for success, failure, and
cancellation, respectively. Facts text remains dim, uses the available terminal
width independently, and may extend beyond the divider.

The footer owns total duration and Run facts. Cost uses full precision while
aggregating and is rounded to cents only for display. Script does not append a
separate `Run: RUN_ID` line. Errors raised before `RunBegin` are reported outside
execution progress; terminal Run errors are owned by progress and its footer.

Script does not copy the durable root result to stdout by default. `--save -`
writes that result to stdout and `--save PATH` atomically writes it to a file;
both are independent of progress verbosity. Failed and canceled Runs do not
write a selected result destination.

Chat uses the same projected content, aggregate facts, and root footer.
Submission UI is not an execution Run header.

## Chat Control Blocks

`RunStartBlock` represents a submitted request and `RunSteerBlock` represents a
steer control attached to an active Run. Their rendered bars contain only the
authored message: Run ids, pending state, and other execution status belong to
execution progress rather than the control.

Both control bars use the same background and reserve column zero for a
left-aligned half-cell `▌` accent strip. Start and Steer use distinct accents;
the bottom `PromptBox` uses the Start accent. The strip replaces prompt glyphs
such as `>` and `+` while leaving message text aligned in column two.

The bottom `StatusBar` reserves eight cells before the model name. When idle, it
renders a Start-accent `▌` in column zero, one space, and a Start-accent `model `
label without punctuation. A local Run first changes column zero to a full-cell
`█`; columns one through six then contain a contiguous background fill that
grows from left to right and retracts, while column seven remains a separating
space. Filled cells start with the same color as the full-cell strip and weaken
toward their right edge. Their colors are six sRGB-channel linear blends from
the configured Start accent toward the configured status background, at blend
amounts `0`, `.16`, `.32`, `.48`, `.64`, and `.80`; changing either endpoint
therefore updates the entire gradient.

The breathing cycle uses monotonic elapsed time rather than uniform frame
steps: a 720-millisecond sine-eased expansion, a 180-millisecond peak, a
900-millisecond sine-eased retraction, and a 260-millisecond trough. Every
retraction narrows the full-cell `█` back to the half-cell `▌` for the trough;
the strip returns to `█` before the next expansion. The UI checks the phase
every 80 milliseconds but redraws only when the visible state changes.
Completion retracts from the current fill with the same easing and a duration
proportional to its width, then narrows the full-cell `█` directly back to the
idle half-cell `▌`. No gap appears between the strip and fill, the model name
never shifts, and animation is never committed to scrollback.

Run completion does not delay results or input, but the running appearance is
held for at least 600 milliseconds and finishes its retraction so short Runs do
not flash past abruptly.

## Implementation Touchpoints

- `src/toolang/execution/types.py`, `events.py`, `records.py`, and `schemas.py`:
  typed collection and loop facts plus validation.
- `src/toolang/execution/executor/steps/loop.py` and statement executors:
  collection cardinality, iteration occurrences, totals, and terminal cause.
- `src/toolang/execution/executor/steps/model.py`: streamed Text Part and final
  Model result consistency enforcement.
- `src/toolang/cli/common/execution_progress/`: projector, typed active state,
  Step projection, Markdown partitioning/rendering, headers, formatting, and
  semantic update types.
- `src/toolang/cli/common/script_progress/`: `ScriptRunPresenter`, console sink,
  and root footer.
- `src/toolang/cli/toolang/commands/chat/`: `ChatRunPresenter` integration.

## Acceptance Tests

Deterministic tests cover:

- complete multiline Model and Tool output without per-leaf facts;
- single-Run Flow trace and Flow facts;
- no-doc AST headers with unchanged runnable names;
- one-line parallel live lanes and successful committed aggregation;
- semantic Map, Storm, Keep, Drop, and Rank result sentences;
- retained failed lanes, aligned error details, boundary error, and pointer
  suppression;
- Repeat and Settle boundaries plus all four typed termination causes;
- ownerless Run errors;
- no Run header, no output shape, and root footer facts/cost;
- progressive header and Markdown commits without terminal duplication;
- streamed Part prefix and successful Step output consistency;
- retained partial Model output followed by one failure or cancellation row;
- Script Rich Live and non-TTY append-only behavior;
- equivalent Script and Chat Markdown rendering and semantic projection; and
- message-only Chat control blocks with distinct left accents and no prompt
  glyphs or execution metadata, plus a run-scoped status animation that
  preserves model-label alignment.

The default offline verification suite must pass.

## Risks

- Event ordering assumptions are guarded by presentation-safety validation.
- Nested Run metric double-counting is guarded by aggregate regression tests.
- Output volume grows because committed Trace output is intentionally complete.
  Parallel lane rows remain bounded. A single unfinished Markdown block can
  still exceed the preferred live height; correctness takes precedence over
  forcibly committing Markdown that later input could reinterpret.
- Future Step kinds must explicitly choose Trace, Lane, Flow, and facts behavior.

## Open Questions

None.
