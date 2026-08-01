# Execution Transcript

This document defines Toolang's compact linear execution transcript. It is a
presentation grammar only and adds no events, records, steps, or identities.
It refines the shared styles in
[execution-presentation.md](./execution-presentation.md).

Script mode implements it first. Chat may reuse its live and finalized blocks.
Inspection reuses its vocabulary and identities from durable state, but never
reconstructs a historical event stream.


## Principles

- Preserve ordered events as one timeline, not a deeply nested tree.
- Keep dynamic parent context in flat section headers.
- Display only real run IDs, control indexes, and `StepPath` values.
- Bound parallel output by lane count, not item count.
- Keep `default ⊂ -v ⊂ -vv`; failures remain visible at every non-quiet
  level.


## Blocks

```text
RunBlock
├── RunHeader
├── RunInput
├── Body
│   ├── AtomicStep
│   └── StatementBlock
│       ├── StatementHeader
│       ├── Work
│       │   ├── ChildRun
│       │   ├── Batch
│       │   ├── Repeated
│       │   │   └── TraceSection
│       │   └── Local
│       └── Outcome
│           ├── StatementResult
│           └── ControlDecision
└── RunSummary
```

A root run uses the complete block. A child run uses the same header, input,
and body with a compact one-line summary. `TraceSection` is display context,
not an execution entity, and never creates an ID or indentation level.


## Visibility

Annotations in examples are documentation only:

| Annotation | Visibility |
| --- | --- |
| `[0+]` | default, `-v`, `-vv` |
| `[1+]` | `-v`, `-vv` |
| `[2]` | `-vv` |
| `[live]` | replaced while active |
| `[always]` | every non-quiet level |

- `-q` prints neither progress nor the final value.
- Default shows run, statement, and work headers, stable run-step output,
  meaningful statement or control results, and the root summary.
- `-v` adds descriptions and stable batched-work aggregates.
- `-vv` adds inputs, controls, step facts, predictable statement results, and
  retained repeat sections.

When visible, completed batch work collapses into one aggregate line. Repeat
may retain its authored nested statements at `-vv`; per-item batch output
remains bounded even there.


## Identity And Layout

All positions are zero-based:

```text
run_abc@0       run control
run_abc/2       step 2
run_abc/2/0     nested step 0
```

`@` distinguishes controls from step paths. Items, iterations, and lanes keep
their native positions; plural values remain counts. Brackets identify a
statement's zero-based ordinal in its visible statement list; they are not a
durable identity. Top-level ordinals currently coincide with the final
`StepPath` segment. A repeat body restarts at `[0]` in every iteration, while
complete durable paths remain available in step facts and diagnostics.

Root content starts at the left margin. Statement content uses one hanging
indent, and wrapped lines align with the content after their marker. A section
and its contents stay in that same content column:

```text
=== iteration 0 ===
=== iteration 1 · iteration 0 ===
```

Nested context becomes one breadcrumb rather than nested frames. The next
section, statement, or run summary ends the current section.

A blank line separates root paragraphs, authored descriptions from work, and
adjacent statement blocks. Within one statement, work output, its compact child
run summary, and the statement result stay adjacent. Hiding a verbosity-gated
line must not leave an empty paragraph behind.


## Markers And Color

`marker` is the canonical term for a leading structural glyph. `mark` is not
used as a presentation concept.

| Marker | Meaning |
| --- | --- |
| `·` | successful or active run-internal output, or batch work summary |
| `!` | step, transformation, or control diagnostic |
| `↳` | compact child-run summary, statement result, or control decision |
| none | continuation facts, headers, and the root run summary frame |

Markers describe structure; color describes status. Failed diagnostics and
failed `↳` summaries are red, canceled summaries are yellow, and successful
completed progress is dim with an optional green status word. Facts remain
dim. Root run summaries keep their frame and never add a marker.

The text after `↳` identifies which boundary it closes:

```text
↳ RUN_ID STATUS · DURATION       compact child-run summary
↳ DATA_EFFECT                    statement result
↳ CONTROL_DECISION               repeat or until decision
```


## Run And Atomic Steps

```text
Run agic expand_queries                                  [0+]
Expand one topic into several research queries.          [1+]

> agent framework                                        [2]
  count=6                                                [2]
  run_queries@0                                           [2]

· ["agent framework architecture", "multi-agent SDK"]  [0+]
  run_queries/0 · 2.0s · deepseek/deepseek-chat · 4.6k/44 tokens [2]

--- run_queries succeeded ---                            [0+]
1 item returned                                          [0+]
2.0s · 4.6k/44 tokens · 1 model call                  [0+]
-----------------------------                            [0+]
```

The header and description are one paragraph; input is another. `>` denotes
the primary input `_`, followed by arguments and the accepted control.

Root summaries use a frame. A nested run closes without repeating output or
resource totals:

```text
↳ run_reduce0 succeeded · 2.6s
```

Model and tool steps use one output line and one facts line. Failure replaces
the successful output in the same position:

```text
· web_search.search returned 5 results
  run_abc/4 · 820ms · exit 0

! web_search.search: provider returned status 429
  run_abc/4 · 820ms · exit 429
```


## Statements And Binding

The statement header is a compact rendering of the authored `.too` head. It
keeps source order and source binding syntax, omits the body and trailing
colon, and hides generated inline-agic names:

```text
[2] let findings = map search_web par 4
  Search the web for each query.
```

The three binding forms therefore remain recognizable without a presentation-
only suffix:

```text
map search_web                    save to `_`
let findings = map search_web     save to `findings`
let map search_web                discard
```

Failure performs no binding and needs no `unchanged` line.

Work describes execution:

```text
Run agic expand_queries
Run flow review
Run agic search_web in parallel (18 items, 4 lanes)
Run agic generator in parallel (6 times, 4 lanes)
Run agic reducer sequentially (6 items)
```

The statement and work boundaries remain separate even for direct `run`:

```text
[5] run synthesize_report
  Run agic synthesize_report

[6] let reviewed = run review
  Run flow review
```

Statement results combine the final shape, destination, and transformation.
Putting the destination after the shape keeps the important cardinality at the
front while avoiding an isolated binding block:

```text
↳ 6-item list saved to _ · scattered from 1 item              [0+]
↳ 18-item list saved to findings · mapped from 18 items     [2]
↳ 8-item list discarded · 8/18 items kept                   [0+]
↳ 8-item list saved to _ · top 8/18 items selected          [0+]
↳ 1 item saved to report · reduced from 6 items             [2]
```

A meaningful result depends on runtime data and remains visible by default. A
predictable result merely confirms the statement contract and appears only at
`-vv`. Failed statements produce no `↳` result.

Local statements have no child-run work header. Their operand already names
their destination:

```text
[0] let topic
  ↳ 1 item saved to topic · perceived from authored content   [2]
```


## Parallel Work

Parallel work owns one lane-bounded live area:

```text
Run agic relevance in parallel (18 items, 4 lanes)
· 5 runs succeeded · 4 active · 1 failed · 3.1s
0 │ item 5 | thinking…
1 │ item 6 | executing knowledge.search…
2 │ item 7 | score 0.82
3 │ item 8 | composing score…
```

Batch run states always use this order:

```text
· N runs succeeded · N active · N failed · DURATION
```

Zero groups are omitted. The leading marker and wording remain stable as the
live line becomes the final aggregate; completion merely removes `active` and
adds final resource facts.

Each lane replaces its previous row. Completion leaves an aggregate at `-v`
and above:

```text
· 17 runs succeeded · 1 failed · 9.4s · 64.2k/720 tokens
```

Even `-vv` does not expand every successful item. A useful failing item may
remain; full item history belongs to inspection. Empty input starts no child
run and renders `· 0 runs · empty input list`.

A lane is a reusable concurrency slot, not `item % lane_count`. A waiting item
acquires whichever lane has completed its previous run, and a `RunEnd` clears
the row only while that run still owns the lane.


## Repeat

`repeat` is a control statement with no result or binding. Its body statements
update the current flow locals through their own bindings. Each section
restarts the body's local statement ordinals:

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

  === iteration 1 ===

  [0] let review = run review
    Run agic review
  [1] run revise
    Run agic revise

  [?] until
    Run agic <agic:L42>

    · true
      run_until1/0 · 580ms · deepseek/deepseek-chat
    ↳ run_until1 succeeded · 580ms

    ↳ stop repeating
```

The section scopes `[0]` and `[1]` to one iteration. These are readable body
positions, not aliases for nested `StepPath` values. `[?]` places `until` at the
same visual level without consuming an ordinal. Its evaluator is a normal
child `RunBlock`; the first `↳` closes that run and the second records the
control decision. A blank line separates the two outcomes.

Below `-vv`, only the current iteration is live. A fixed-count repeat needs no
completion result because success already proves the count. When `until` stops
the loop early, `↳ stopped after N iterations` is a meaningful default
control result.

If the until child run fails, repeat and its enclosing flow fail without a
`continue` or `stop repeating` decision. The diagnostic uses `!`, followed by
a red `↳ RUN_ID failed` summary. If the child succeeds but its value cannot
coerce to `Boolean`, the child summary remains successful and a subsequent
`! until requires Boolean` diagnostic owns the repeat failure.


## Settle

`settle` is a value-producing left fold. Like `map`, `keep`, and `rank`, it
uses one bounded batch work block and applies its outer binding only after all
child work succeeds. Unlike them, it runs sequentially: `_` is the internal
accumulator, `item` is the current source value, and item `0` receives empty
`_`.

```text
[4] let report = settle relevance
  Reduce the findings into one report.                       [1+]

  Run agic relevance sequentially (18 items)                 [0+]
  · 5 runs succeeded · 1 active · 3.1s                     [live]
  │ item 5 | thinking…                                      [live]
```

Sequential work has at most one active item. Once it fails, no item remains
active:

```text
  Run agic relevance sequentially (18 items)
  · 5 runs succeeded · 1 failed · 4.8s
  │ item 5 | output is not valid Report
```

`-vv` uses this same compact active row. It does not add an item section or
expand the child run, just as `map`, `keep`, and `rank` do not expand their
successful items. Child output becoming the next internal accumulator is
settle's dataflow, not another binding, and receives no `save` annotation.

Completion leaves only aggregate work and the semantic result:

```text
  · 18 runs succeeded · 31.4s · 42.8k/3.1k tokens       [1+]
  ↳ 1 item saved to report · reduced from 18 items           [2]
```

The source-like statement head declares the outer binding. The result repeats
the destination only when that result is visible, so it remains meaningful
when copied without the header. Child run identity remains available in native
events, failures, and inspection; routine transcript output needs only the
zero-based item position.


## Failure And Renderer Boundary

One diagnostic appears at the most useful visible boundary:

```text
! run_research/2 failed: item 5: output is not valid Number
  · 5 runs succeeded · 1 failed · 3.1s
```

Later transformation results are omitted and the declared binding is not
applied. Full exception chains belong in the run log.

A renderer may derive ownership, placement, metrics, and bounded previews from
ordered native events. It must not create identities or events, query SQLite
on the event path, reconstruct missing locals, print secrets or unbounded
values, or change execution contracts solely for layout.
