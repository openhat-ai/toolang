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
- ordinary leaf Steps remain as complete finalized trace;
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
- `ProgressUpdate.finalized` is an append-only delta in completion order.
- `ProgressUpdate.live` is the complete replaceable live snapshot.
- `ScriptRunPresenter` and `ChatRunPresenter` adapt these updates to their
  surfaces. Wrapping, lane truncation, color, and live-area replacement remain
  surface concerns.

The projector never queries durable storage and retains only active state,
bounded Model preview text, one state record per active lane, aggregate metrics,
boundary claims, and concrete errors required to resolve pointers.

## Runtime Facts

`StepEnd.noted` supports these typed payloads:

- `ModelStepNoted` for model usage, price, cost, and provider state;
- `LoopStepNoted` for `iterations` and `termination`.

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

A Step follows one lifecycle:

```text
optional header -> replaceable live content -> terminal output -> optional facts
```

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

`·` is the only execution marker and starts in column zero outside a lane.
There is no `↳`, `→`, or `!` marker.

- Model activity and output use `·`.
- Tool activity and the Tool terminal row use `·`; Tool output continues below
  without a marker.
- Flow terminal output and parallel aggregate output use `·`.
- Errors use `·` with error styling.
- Parallel lanes embed `·` after their lane columns.
- Flow facts and continuation lines are unmarked and align with the text after
  the marker.

Result pointers, child closure, binding effects, and control decisions are not
displayed. If such information is added later, it belongs below output as an
unmarked continuation.

## Flow Headers

Statement headers are optional Step boundaries. They are emitted only when the
Step has visible trace, output, or aggregate content and are followed by one
blank line.

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

· thinking…
· true
```

The condition is a Run, not a synthetic `executed completion_check` Step.

## Trace Output

Model output replaces Model activity and is preserved completely:

```text
· thinking…
· Source summary prepared
```

A Tool keeps its terminal action and complete output on following lines:

```text
· executing web_search.search…
· executed web_search.search
  5 results
```

A model Tool request is visible without creating a synthetic Tool result:

```text
· requested web_search.search
```

Model and Tool Steps do not display StepPath, duration, model name, exit code,
tokens, cost, or other Step facts.

A direct Flow-owned value is displayed directly. A Flow `run` with one child
Run emits no synthetic success row; the absence of an error means success.
Flow output shape such as `1 item` or `6-item list` is never displayed.

## Flow Facts

A Flow Step may append one unmarked facts row when it owns child execution:

```text
[2] Search the web for each query

· 6 succeeded
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
· running · 4 succeeded · 3 active
  0 | #4 | · thinking…
  1 | #5 | · executing web_search.search…
  2 | #6 | · Source summary prepared
```

On success, live lanes are cleared and only the finalized aggregate remains:

```text
· 7 succeeded
  31.0s · 7 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

On failure, clear successful, active, and canceled lanes. Preserve each failed
lane and its complete causal output, then display an independent parallel-Step
boundary error and the Flow facts:

```text
· 4 succeeded · 1 failed · 2 canceled
  1 | #5 | · failed fetch_page
             provider returned status 429

· parallel step stopped because lane 1 (#5) failed
  31.0s · 7 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

The lane owns the original error. The parallel Step owns a distinct explanation
of why the aggregate Step terminated. Enclosing pointer failures remain silent.

## Loop Output

Repeat and Settle share iteration boundaries and terminal output. A successful
loop emits one of:

```text
· completed 3 iterations
· condition met after 2 iterations
```

Failure and cancellation emit:

```text
· interrupted after 2 iterations
· canceled after 2 iterations
```

The causal child error is displayed at the child Step or lane. The loop row
describes termination and does not repeat that error.

## Error Ownership

Errors are visible once, as close as possible to where they occur:

- a concrete Step error occupies that Step's normal output position;
- a failed lane preserves its concrete error;
- a parallel Step adds only its distinct local-failure boundary error;
- parent Steps and Runs whose errors are pointers emit nothing;
- a concrete Run error with no Step owner is emitted as a final error-like Step
  row: `· MESSAGE`.

There is no special diagnostic marker. A malformed presentation stream also
clears live state and emits one root-owned `· MESSAGE` row.

## Root Run Footer

Script mode displays no Run header and no output shape. After projected Steps it
renders only the root footer:

```text
--- run_nrqpt0mf succeeded ---
1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
------------------------------
```

The footer owns total duration and Run facts. Cost uses full precision while
aggregating and is rounded to cents only for display.

Chat uses the same projected content and aggregate facts in its existing Run
status/footer surface; submission UI is not an execution Run header.

## Implementation Touchpoints

- `src/toolang/execution/types.py`, `events.py`, `records.py`, and `schemas.py`:
  typed loop facts and validation.
- `src/toolang/execution/executor/steps/loop.py` and statement executors:
  iteration occurrences and terminal cause.
- `src/toolang/cli/common/execution_progress/`: projector, typed active state,
  Step projection, headers, formatting, and semantic update types.
- `src/toolang/cli/common/script_progress/`: `ScriptRunPresenter`, console sink,
  and root footer.
- `src/toolang/cli/toolang/commands/chat/`: `ChatRunPresenter` integration.

## Acceptance Tests

Deterministic tests cover:

- complete multiline Model and Tool output without per-leaf facts;
- single-Run Flow trace and Flow facts;
- no-doc AST headers with unchanged runnable names;
- one-line parallel live lanes and successful finalized aggregation;
- retained failed lanes, aligned error details, boundary error, and pointer
  suppression;
- Repeat and Settle boundaries plus all four typed termination causes;
- ownerless Run errors;
- no Run header, no output shape, and root footer facts/cost; and
- equivalent Script and Chat semantic projection.

The default offline verification suite must pass.

## Risks

- Event ordering assumptions are guarded by presentation-safety validation.
- Nested Run metric double-counting is guarded by aggregate regression tests.
- Output volume grows because finalized Trace output is intentionally complete;
  only live previews and parallel lane rows are bounded.
- Future Step kinds must explicitly choose Trace, Lane, Flow, and facts behavior.

## Open Questions

None.
