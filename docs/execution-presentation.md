# Execution Presentation

This document defines the target presentation language for Toolang execution.
It is intended for review before the script renderer is finalized and before
inspection and chat adopt the same vocabulary.

The concise normative block grammar is defined in
[execution-transcript.md](./execution-transcript.md). Where examples in this
broader design record differ, the transcript grammar takes precedence.

It defines presentation only. It does not add execution concepts, records, or
events. Inspection reads durable schemas. Live script and chat renderers
consume ordered native `RunEvent` values.


## Surfaces

The three surfaces share words and formatting, but not one renderer:

- **script** renders live progress to stderr and the final value to stdout;
- **inspection** renders durable state without reconstructing deltas;
- **chat TUI** keeps mutable activity in a bounded live area and progressively
  moves stable content into scrollback.

The current implementation work applies this design to script mode first.
Inspection and chat keep their existing implementation until changed
explicitly.


## Visibility

Examples use these review annotations. They are not printed:

| Annotation | Visible at |
| --- | --- |
| `[0+]` | default, `-v`, and `-vv` |
| `[1+]` | `-v` and `-vv` |
| `[2]` | `-vv` only |
| `[live]` | mutable terminal content, replaced rather than appended |
| `[always]` | every non-quiet level, especially failures |

Script channel behavior is:

| Level | stderr | stdout |
| --- | --- | --- |
| `-q` | nothing | nothing |
| default | headers, stable run-step output, meaningful results, failures, and root summary | final value |
| `-v` | descriptions and stable batched-work summaries | final value |
| `-vv` | input, IDs, step facts, predictable results, and repeat sections | final value |

At `-q`, only the process exit status communicates the outcome. Verbosity
never exposes secrets or unbounded request data.


## Vocabulary

### Run

A run executes one named agic or flow. A caller starts a root run. A flow
statement or agic loop may start child runs.

```text
run_abc123
```

### Step

A step is one durable execution operation. Its full `StepPath` is:

```text
run_id/step_index[/step_index...]
```

Examples:

```text
run_abc123/2
run_abc123/2/0
```

Whenever output shows a StepPath, it shows the complete path so it can be
copied into an inspection command.

### Control

A run control is identified by its run ID and durable zero-based control
index. Presentation uses `@` so it cannot be confused with a StepPath:

```text
run_abc123@0    # start control
run_abc123@1    # first later steer or stop control
run_abc123/0    # first step, not a control
```

The compact forms are:

```text
RUN_ID@CONTROL_INDEX
RUN_ID/STEP_INDEX[/CHILD_STEP_INDEX...]
```

### Flow Statement

A flow statement is an authored operation such as `run`, `scatter`, `map`, or
`rank`. It is not a separate durable record.

A statement may contain three phases:

1. run child work or perform a local operation;
2. validate, reshape, filter, rank, or otherwise transform the result;
3. save or discard the successful result.

The renderer derives this structure from existing step events and statement
facts. It does not invent statement events.


## Shared Formatting

### Status

Execution statuses are presented without translation.

| Internal | Display |
| --- | --- |
| `running` | `running` |
| `succeeded` | `succeeded` |
| `failed` | `failed` |
| `canceled` | `canceled` |

`marker` is the canonical term for a leading structural glyph. Markers express
ownership; color expresses status:

| Marker | Meaning |
| --- | --- |
| `·` | successful or active run-internal output, or batch work summary |
| `!` | step, transformation, or control diagnostic |
| `↳` | compact child-run summary, statement result, or control decision |
| none | continuation facts, headers, and the root run summary frame |

Active agic work and successful output use `·`:

```text
· thinking…
· executing web_search.search…
```

Failure uses `!`. The error occupies the normal output position, followed by
the same facts layout as successful output:

```text
! web_search.search: provider returned status 429
  run_abc123/2 · 820ms · exit 429
```

Failed diagnostics and failed `↳` summaries are red. Canceled summaries are
yellow. Successful completion is dim, with an optional green status word.
Facts are dim. Root frames have no marker.

Root results use an unmarked frame containing the root run ID:

```text
--- run_abc123 succeeded ---
1 item returned
8.2s · 4.6k/63 tokens · 1 model call
----------------------------
```

### Shape

Shape is distinct from the value it contains:

```text
1 item
0-item list
1-item list
8-item list
```

`1 item` never describes a one-element flow list.

### Facts

Facts are separated by centered dots:

```text
8.2s · 18 runs · 64.2k/720 tokens · 18 model calls
```

Token usage is `INPUT/OUTPUT tokens`:

```text
4.6k/63 tokens
34k/1.5m tokens
```

Durations use compact units:

```text
12ms
1.8s
1m 08s
```

### Progress Style

Progress is dim by default so final stdout remains visually primary. This is a
progress style, not a blanket stderr style:

- active mutable work uses normal brightness;
- failure uses normal-brightness red;
- cancellation uses normal-brightness yellow;
- completed progress, metadata, statistics, and frames are dim.

When stderr is not a TTY, the renderer emits no color, cursor movement, or
partial delta lines. Stable boundaries remain newline-delimited.


## Alignment

Root output has no left margin:

- agic activity uses `·` in the root marker column;
- flow statements use a bracketed zero-based ordinal in the root marker
  column;
- content starts after its marker;
- wrapped lines stay aligned with that content;
- the root summary reserves the marker column without displaying a marker.

```text
· Alpha beta gamma delta epsilon zeta
  eta theta iota kappa.
  run_abc123/0 · 1.8s · deepseek/deepseek-chat · 3.4k/86 tokens

--- run_abc123 succeeded ---
1 item returned
1.8s · 3.4k/86 tokens · 1 model call
----------------------------
```

Nested content adds one two-space level:

```text
[1] map search_web par 4
  · A long result starts here and continues after wrapping
    at the same content boundary.
```

Parallel lane rows are truncated instead of wrapped so their columns remain
stable.


## Root Run Block

Every root run follows this structure:

```text
Run RUNTYPE NAME                                  [0+]
DESCRIPTION                                       [1+]

> INPUT                                           [2]
  ARG=VALUE ...                                   [2]
  RUN_ID@CONTROL_INDEX                            [2]

ACTIVITY

--- RUN_ID STATUS ---                             [0+]
RESULT SHAPE OR ERROR                             [0+]
AGGREGATE FACTS                                   [0+]
---------------                                   [0+]
```

Rules:

- runnable name and description form one left-aligned paragraph;
- a blank line separates that paragraph from the input paragraph;
- the input block has a trailing blank line when visible;
- activity has a trailing blank line before the root summary;
- omitted optional blocks do not leave extra blank lines;
- the input paragraph identifies its accepted start control;
- the frame title contains the root run ID and status;
- child run IDs come from descendant StepPaths or failure boundaries rather
  than being repeated on work lines;
- opening and closing frame lines have equal width.


## Agic Runs

An agic is a model-tool loop. Activity starts with what is happening rather
than the implementation kind.

### Model

Before output:

```text
· thinking…                                                   [live]
```

When deltas arrive, they replace the same live line:

```text
· Sunlight is scattered by molecules in the atmosphere…      [live]
```

Successful final text remains in scrollback at every non-quiet level. The
canonical, untruncated root value is still written to stdout, while progress
uses a bounded preview.

At `-vv`, the completed model step adds one metadata line:

```text
· Sunlight is scattered by molecules in the atmosphere.
  run_abc123/0 · 1.8s · deepseek/deepseek-chat · 3.4k/86 tokens
```

The order is:

```text
STEP_PATH · DURATION · MODEL · TOKENS
```

There is no separate `model succeeded` line.

### Tool

```text
· executing web_search.search…                               [live]
· web_search.search: 5 results                               [0+]
  run_abc123/1 · 820ms · exit 0                              [2]
```

The same two-line structure presents failure. The error replaces successful
output in the primary content position, and facts remain below it:

```text
! web_search.search: provider returned status 429
  run_abc123/1 · 820ms · exit 429
```

Tool failure is visible at every non-quiet level. There is no second tool
summary repeating the same tool name, error, or duration.

### Complete Agic

```text
Run agic expand_queries
Expand one topic into several research queries.               [1+]

> agent framework                                             [2]
  count=6                                                     [2]
  run_queries123@0                                            [2]

· ["agent framework architecture", "multi-agent SDK", ...]    [0+]
  run_queries123/0 · 2.0s · deepseek/deepseek-chat · 4.6k/44 tokens

--- run_queries123 succeeded ---
1 item returned
2.0s · 4.6k/44 tokens · 1 model call
--------------------------------
```

The model metadata line is visible only at `-vv`.


## Flow Runs

A flow presents statements in source order. A statement header starts with its
zero-based ordinal in the currently visible statement list:

```text
[1] map search_web par 4                                     [0+]
  Search the web for each query.                             [1+]
```

For top-level statements, the bracketed ordinal currently equals the final
`StepPath` segment. Inside `repeat`, the body restarts at `[0]` for every
iteration, so its ordinal is scoped presentation rather than durable identity.
Failures and step facts use complete paths when they must identify a persisted
operation.

Event placement indexes are displayed without conversion:

- `item`, `lane`, and `loop` are zero-based positions;
- `items`, `lanes`, `completed`, and `active` are cardinal counts.

For example, four lanes are identified as `0` through `3`, while the work line
still says `4 lanes`.

Generated and internal agics do not appear as root commands, but a statement
may identify the runnable it invokes.


## Statement Work And Child Runs

Work uses concise imperative sentences:

```text
Run agic expand_queries
Run flow review
Run agic search_web in parallel (18 items, 4 lanes)
Run agic generator in parallel (6 times, 4 lanes)
Run agic reducer sequentially (6 items)
```

The sentence distinguishes one invocation, parallel independent invocations,
and sequential invocations that carry state forward. Parentheses contain only
execution facts: `N items` describes per-item work, while `N times` describes
repeated generation from the same input.

If a parallel statement receives an empty list, no child `RunBegin` exists.
The statement block still emits its work sentence when `StepEnd` supplies the
zero-item result:

```text
[3] rank top 8
  Run agic <agic:L51> in parallel (0 items)
```

An inline agic uses its source reference instead of an unstable generated
name:

```text
Run agic <agic:L35>
```

A successful child run does not get a frame. A visible nested run may close
with one compact summary; batched success does not accumulate one line per
item. Descendant StepPaths identify the work, and a child failure shows its ID
and one diagnostic.


## Statement Headers And Binding

A statement header is a compact source rendering: it retains `.too` operand
order and binding syntax, drops the body and colon, and hides generated inline
agic names. Binding therefore needs no presentation-only suffix:

```text
[0] map search_web par 4
[1] let findings = map search_web par 4
[2] let map search_web par 4
```

The three heads save to `_`, save to `findings`, and discard, respectively. A
failed statement never applies its binding, so no additional `unchanged` line
is needed. `repeat` is control flow and has no result binding.


## Statement Result Phrases

Each phrase begins with the final shape and destination, then uses `·` to
separate the transformation. The destination is `saved to _`, `saved to NAME`,
or `discarded`. For a list, the first number is the final length. In the table,
`K` is the result length, `M` is the input length, and `N` is an authored count.

| Statement | Transformation after shape and destination | Visibility |
| --- | --- | --- |
| direct `run` | represented by its compact child-run summary | `[2]` |
| `scatter N` | `scattered from 1 item` | `[0+]` |
| `storm N` | `produced by N runs` | `[2]` |
| `gather` | `gathered from M items` | `[2]` |
| `settle` | `reduced from M items` | `[2]` |
| `map` | `mapped from M items` | `[2]` |
| `keep first N` | `K/M items kept from the start` | `[0+]` |
| `keep last N` | `K/M items kept from the end` | `[0+]` |
| predicate `keep` | `K/M items kept` | `[0+]` |
| `drop first N` | `K/M items retained after dropping from the start` | `[0+]` |
| `drop last N` | `K/M items retained after dropping from the end` | `[0+]` |
| predicate `drop` | `K/M items retained` | `[0+]` |
| `rank` | `ranked` | `[2]` |
| `rank top N` | `top K/M items selected` | `[0+]` |
| `rank bottom N` | `bottom K/M items selected` | `[0+]` |
| authored `let` | `perceived from authored content` | `[2]` |

Meaningful results depend on runtime selection or cardinality and remain
visible by default. Predictable results merely confirm the statement contract
and appear only at `-vv`. Failed statements have no result. `repeat` has no
statement result; its `until` clause may emit a control decision instead.


## Direct `run`

A `run` statement retains both boundaries: its source-like statement header
shows authored intent and binding, while its work header shows the resolved
runnable kind:

```text
[3] let report = run review
  Review and improve the report.                              [1+]

  Run flow review                                             [0+]
  · reviewing weak sections…                                [live]
    run_review1/0 · 4.4s · deepseek/deepseek-chat            [2]
  ↳ run_review1 succeeded · 4.4s                             [2]
```

The compact child-run summary closes the work. A separate statement result
would repeat the same value, so direct `run` does not add one.


## Scatter

`scatter` runs one child, then validates and reshapes its result:

```text
[0] scatter 6 expand_queries
  Expand the topic into six research queries.                 [1+]

  Run agic expand_queries                                    [0+]
  · ["agent architecture", "agent tools", ...]               [0+]
    run_queries1/0 · 2.0s · deepseek/deepseek-chat · 4.6k/63 tokens
  ↳ run_queries1 succeeded · 2.0s                            [2]
  ↳ 6-item list saved to _ · scattered from 1 item             [0+]
```

The child model metadata line is visible only at `-vv`.

The child may fail to return an array:

```text
[0] scatter 6 expand_queries
  Run agic expand_queries
  ! run_research1/0 failed: scatter requires a list result
    · 1.7s
```

No binding follows this failure.


## Parallel Statements

The lane count is bounded while the item count may be large. Parallel work
owns one mutable block rather than one scrollback line per item:

```text
[2] let findings = rank relevance top 8 par 4
  Rank findings by relevance.                                [1+]

  Run agic relevance in parallel (18 items, 4 lanes)          [0+]
  · 5 runs succeeded · 4 active · 1 failed · 3.1s           [live]
  0 │ item 5 | thinking…                                     [live]
  1 │ item 6 | executing knowledge.search…                   [live]
  2 │ item 7 | score 0.82                                    [live]
  3 │ item 8 | composing score…                              [live]
```

Lane syntax is:

```text
LANE │ item POSITION | ACTIVITY
```

The progress line does not repeat the total already present in the statement
and work lines. Batch states consistently use `N runs succeeded`, `N active`,
and `N failed` in that order, omit zero groups, and retain the leading `·` as
live progress becomes the final aggregate. Each lane replaces its previous
item. Successful child runs update the aggregate and never create individual
scrollback entries.

A lane is a reusable concurrency slot, not an item-derived label. The next
waiting item acquires a lane only after its previous run has ended; cleanup is
guarded by run identity so an older completion cannot erase the new owner.

When work completes, the live lane area disappears and a stable work summary
remains at `-v` and above:

```text
  · 18 runs succeeded · 9.4s · 64.2k/720 tokens · 18 model calls
```

The meaningful semantic result remains at every non-quiet level and is also
self-contained when copied without its statement header:

```text
  ↳ 8-item list saved to findings · top 8/18 items selected
```

A failure retains one useful failing item:

```text
[2] rank relevance top 8 par 4
  Run agic relevance in parallel (18 items, 4 lanes)
  ! run_research1/2 failed: item 5: output is not valid Number
    · 5 runs succeeded · 1 failed · 3.1s
```


## Repeat

`repeat` is control flow. It produces no result, accepts no binding, and lets
its body statements update the current locals through their own bindings. It
keeps only its most recent iteration in the live area. Retained `-vv` sections
flatten the loop context into a three-equals heading; the heading is display
context and creates no durable identity:

The retained `-vv` form is:

```text
[2] repeat 3
  Carry each revised draft into the next review cycle.

  === iteration 0 ===

  [0] let review = run review
    Run agic review
    · Review identified unclear ownership...
      run_review0/0 · 2.1s · deepseek/deepseek-chat
    ↳ run_review0 succeeded · 2.1s

  [1] run revise
    Run agic revise
    · Revised proposal...
      run_revise0/0 · 4.3s · deepseek/deepseek-chat
    ↳ run_revise0 succeeded · 4.3s

  [?] until
    Run agic <agic:L42>

    · false
      run_until0/0 · 620ms · deepseek/deepseek-chat
    ↳ run_until0 succeeded · 620ms

    ↳ continue
```

The section scopes `[0]` and `[1]` to one iteration; the ordinals restart in
the next section and are not aliases for durable StepPaths. `[?]` is a sibling
clause marker that does not consume an ordinal. Its evaluator is shown as a
normal nested run. One `↳` closes that run, then a blank line separates the
control decision `↳ continue` or `↳ stop repeating`.

Below `-vv`, the next iteration replaces the preceding live section. A
fixed-count repeat needs no completion result because successful completion
already proves the count. Early exit is meaningful and remains visible:

```text
  ↳ stopped after 2 iterations                               [0+]
```

If an iteration body fails, later body statements, `until`, and later
iterations do not occur. If the `until` evaluator run fails, repeat and the
enclosing flow fail; the transcript shows the diagnostic and a red compact run
summary, but no control decision:

```text
  [?] until
    Run agic <agic:L42>
    ! model request failed: provider returned status 429
      run_until1/0 · 820ms
    ↳ run_until1 failed · 820ms
```

An evaluator run may succeed while its output still fails Boolean coercion.
In that case its successful run summary remains valid, followed by the owning
control diagnostic; failure is never interpreted as `false`:

```text
    · "yes"
      run_until1/0 · 580ms · deepseek/deepseek-chat
    ↳ run_until1 succeeded · 580ms

    ! until requires Boolean; got Text
```

Zero iterations leave locals unchanged.


## Settle

`settle` is a left fold. It transforms an `N-item list` into one item through
sequential reducer runs.

The current syntax has no explicit initial-value clause. Its implicit initial
value is empty content. The reducer uses Toolang's primary input/output
convention:

```too
agic reducer(_: Part[], item: Item) -> Part[]:
  ...
```

For every reducer run:

- `_` is the accumulated value and the child run's primary input;
- `item` is the current source-list item, passed as a runnable argument;
- the child run output becomes `_` for the next call.

Conceptually:

```text
_ = reducer(_, item)
```

```text
_ = empty
_ = reducer(_, a)
_ = reducer(_, b)
_ = reducer(_, c)
```

Therefore:

- an `N-item list` starts `N` reducer runs;
- the first reducer run receives `item=0` and empty `_`;
- an empty list returns the empty implicit initial value;
- the reducer primary-input type must accept that empty value;
- item and accumulator types may differ after the first call.

Event placement preserves source and call indexes separately:

- `item` is the zero-based source-list index;
- `loop` is the zero-based reducer-call index.

The first reducer run has `item=0` and `loop=0`. With the current contract,
the two indexes remain equal.

Named and inline settle forms use the existing syntax:

```too
settle reducer

settle -> Part[]:
  Accumulated:
  {{_}}

  Current item:
  {{item}}
```

Lowering gives an inline settle agic an implicit `item: Part[]` parameter. It
does not add an initial-value node to the grammar or AST.

Settle uses the same bounded batch-work language as `map`, `keep`, and `rank`,
with one active slot rather than multiple lanes:

```text
[4] let report = settle reducer
  Run agic reducer sequentially (6 items)
  · 3 runs succeeded · 1 active · 4.2s                       [live]
  │ item 3 | thinking…                                        [live]
```

`-vv` keeps the same compact row. It does not create an item section or expand
the child run, matching `map`, `keep`, and `rank`.

Completion leaves the aggregate work and semantic result:

```text
  · 6 runs succeeded · 12.4s · 18.2k/940 tokens
  ↳ 1 item saved to report · reduced from 6 items             [2]
```

The source-like header and visible result both identify the outer binding.
Child output becoming the next accumulator is settle's internal dataflow, not
another binding.

An explicit authored initial value remains a future language feature. It will
be designed with the settle syntax and AST rather than inferred by execution.


## Failures

One diagnostic is printed at the most useful visible boundary. It identifies,
when available:

- authored statement and complete StepPath;
- failed item and child run;
- expected type or shape;
- bounded received value;
- actionable provider or tool cause.

```text
[2] rank relevance top 8 par 4
  ! run_research1/2 failed: item 5: output is not valid Number
    · 5 runs succeeded · 1 failed · 3.1s · 18.2k/210 tokens

--- run_research1 failed ---
5 runs succeeded · 1 failed · 3.1s · 18.2k/210 tokens
----------------------------
```

If no visible operation already owns the diagnostic, the root frame displays
it once. Full exception chains belong in the per-run log.

Canonical partial progress is:

```text
storm       5 of 6 runs completed before failure
map         5 of 18 items mapped before failure
keep/drop   5 of 18 predicates evaluated before failure
rank        5 of 18 items scored before failure
settle      5 of 6 items settled before failure
repeat      2 iterations completed before failure
```

Failed value statements never apply their declared binding.


## Cancellation

A canceled run has no stdout value:

```text
--- run_abc123 canceled ---
interrupted by user
4.1s · 2 completed · 1 active
---------------------------
```

The first interrupt requests cancellation and keeps the owner event loop alive
for cleanup and terminal events. A later interrupt may force termination. A
mutable parallel block must never swallow interrupts.


## Root Summary

The root summary is the only routine summary visible at every non-quiet level:

```text
--- run_abc123 succeeded ---
1 item returned
51s · 80.8k/1.2k tokens · 5 model calls · 12 tool calls
----------------------------
```

Agic step metadata and repeat sections are optional `-vv` details.
Stable parallel work summaries remain visible at `-v`. The root frame:

- contains the root run ID before the status;
- aligns with the owning run;
- has no extra status marker;
- uses equal-width opening and closing lines.


## Inspection

Inspection presents durable state and never reconstructs historical deltas. It
may expand records that script folds.

It reuses:

- status words;
- exact Run IDs and StepPaths;
- shape language;
- statement names and authored docs;
- duration and usage formatting;
- the same selected failure cause.

It does not use live markers, lane rows, or root summary frames.


## Chat TUI

The chat TUI keeps its established framework:

- non-full-screen `prompt_toolkit`;
- real terminal scrollback;
- prominent full-width start and steer control bars;
- run ID insertion into the start control after acceptance;
- aligned control padding and activity markers;
- a bounded mutable live area;
- progressive finalization;
- Rich Markdown assistant output.

Stable content moves into scrollback as soon as its ordering and value are
known:

| Content | Finalization boundary |
| --- | --- |
| start control | root `RunBegin` |
| steer control | consuming `StepBegin` or terminal `RunEnd` |
| tool activity | tool `StepEnd` |
| parallel lanes | parent `StepEnd`, as one aggregate |
| flow statement | outer `StepEnd` |
| pending response | root `RunEnd.output` confirms it |
| failure or cancellation | root `RunEnd` |

Parallel presentation uses space proportional to active lanes, not item count.
Scrollback excludes stale `thinking…` lines, token deltas, historical lane
assignments, repeated successful internal output, and duplicate errors.

Restored history is constructed from durable thread and run detail; it does
not replay historical deltas.


## Implementation Boundary

`toolang.execution` supplies execution truth. Caller packages own terminal
width, wrapping, styling, verbosity, live replacement, scrollback, and
stdout/stderr policy.

A renderer may derive:

- recursive ownership from `RunBegin.parent`;
- runnable identity and placement from run context;
- statement kind, binding, source, and operands from `StepBegin.given`;
- output shape, count, usage, and results from `StepEnd`;
- aggregates from ordered descendant events.

If an exact fact is absent, the renderer omits it. It must not query SQLite on
the event path, guess from source text, invent display events, or modify
executor/event contracts solely for presentation.


## Review Checklist

- `-q` emits no stderr or stdout.
- Default shows headers, meaningful statement or control results, failures,
  root summary, and final stdout.
- `-v` adds descriptions, useful previews, and stable work summaries.
- `-vv` adds input, IDs, step facts, predictable statement results, and
  retained repeat sections.
- Runnable descriptions align with the runnable header, with a blank line
  before input.
- `RUN_ID@INDEX` denotes a run control; `RUN_ID/INDEX` denotes a StepPath.
- Control, step, event, record, and displayed statement indexes are zero-based.
- A top-level statement's bracketed ordinal equals the final segment of its
  current StepPath. Repeat-body ordinals restart at `[0]` per iteration and are
  not durable identities.
- Event `item`, `lane`, and `loop` positions are displayed as zero-based values
  without adding one; plural totals and progress values remain counts.
- Root output has no left margin; nested content adds two spaces.
- Wrapped text aligns with its semantic content boundary.
- Root summary frames contain the root run ID before status.
- Model and tool steps do not repeat metadata in separate success lines.
- `marker` is the presentation term; `·` marks run work, `!` marks a
  diagnostic, and `↳` closes a child run, statement result, or control
  decision.
- Errors use `!`; their following facts use the same layout as successful
  output. Failed diagnostics and failed `↳` summaries are red.
- Batched child runs do not accumulate stable per-item summaries; a visible
  nested run may close with one compact summary.
- Non-default value bindings appear on statement headers; default `_` bindings
  are omitted.
- Parallel output is bounded by lane count.
- Repeat has no value or binding; its body statements update current locals.
- Repeat sections use `=== iteration N ===`; body ordinals restart at `[0]`,
  and `[?]` identifies `until` without consuming an ordinal.
- A failed `until` evaluator or failed Boolean coercion fails repeat and emits
  no `continue` or `stop repeating` decision.
- Settle uses one bounded sequential active slot and never accumulates item
  sections in scrollback.
- Settle uses `_` as its accumulated primary input/output and `item` as the
  current source-list item.
- `1 item` and `1-item list` are never interchangeable.
- Failed work never claims that a result was saved or discarded.
- One failure diagnostic is displayed.
- Inspection remains snapshot-oriented.
- The chat TUI retains its layout and progressively finalizes stable content.
