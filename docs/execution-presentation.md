# Execution Presentation

This document defines the execution progress language shared by script mode and
the Chat TUI. It is a presentation contract only: it does not add execution
events, records, identities, or lifecycle states.

Inspection reads durable state and may reuse the same vocabulary, but it does
not reconstruct live progress from stored records.

## Projection Model

Ordered native events pass through one terminal-independent pipeline:

```text
RunEvent -> ProgressProjector -> ProgressUpdate -> surface presenter
```

`ProgressUpdate.committed` contains stable, append-only fragments in event
order. `ProgressUpdate.live` is the complete replaceable snapshot. A Step can
progressively commit a header and output while retaining only its unfinished
tail in live state.

Script and Chat use the same projected ownership, content, aggregates, facts,
and errors. Their presenters own terminal mechanics such as wrapping, lane
truncation, scrollback, and live-area replacement.

A Run has no progress header. A root Run contains projected Steps followed by
one footer. Child Runs are represented through the Steps they execute rather
than separate Run headers or closure rows.

## Width and Alignment

Progress is at most 120 terminal cells wide by default and never wider than an
attached TTY. Set `TOOLANG_PROGRESS_MAX_WIDTH` to a positive integer to change
the maximum for both surfaces. Non-TTY script output uses that configured
maximum.

Execution markers start in column zero. Wrapped content and unmarked
continuations align with the text after the marker:

```text
• Alpha beta gamma delta epsilon
  zeta eta theta
```

Flow headers also start in column zero and are followed by one blank line.
Iteration and condition headers create the same kind of stable boundary.

## Markers and Style

`•` is the only execution-row marker. There is no separate marker for errors,
child closure, control decisions, or parallel work. The centered dot `·` is
only an inline facts separator.

- Model activity and output use `•` and normal text.
- Tool activity uses `•` and normal text. Its successful terminal marker,
  action, and output are dim.
- Flow activity and terminal output use `•` and normal text.
- Errors use `•` with error styling; cancellation uses warning styling.
- Parallel lanes place `•` after the lane columns.
- Headers and facts are dim.

Terminal markers use the same color and intensity as their following content.
Successful Model and Flow outputs use normal white; successful Tool terminal
output uses dim white. Failure uses red and cancellation uses yellow. Green is
not a terminal status color.

Step paths, child-Run closure, binding effects, result pointers, and control
decisions are not displayed. Model and Tool Steps also omit duration, model
name, exit code, usage, cost, and other per-Step facts.

## Flow Headers

A Flow Step uses its non-empty authored doc comment as the header. Without a
doc comment, the presenter generates a short sentence from the typed AST. It
preserves runnable names exactly, hides generated inline runnable names, and
includes authored concurrency or binding behavior only when useful.

Examples include:

| Statement | Generated header |
| --- | --- |
| `let` | `Set NAME` |
| `run` | `Run RUNNABLE` |
| `scatter` | `Expand into N items with RUNNABLE` |
| `storm` | `Run RUNNABLE N times` |
| `gather` | `Combine the items with RUNNABLE` |
| `settle` | `Reduce the items with RUNNABLE` |
| `map` | `Run RUNNABLE for each item` |
| positional `keep` or `drop` | `Keep/Drop the first/last N items` |
| predicate `keep` or `drop` | `Keep/Drop items selected by RUNNABLE` |
| `rank` | `Rank items with RUNNABLE` |
| fixed `repeat` | `Repeat N times` |
| conditional `repeat` | `Repeat up to N times` or `Repeat until complete` |

A direct single-Run Flow Step preserves that Run's leaf trace and emits no
synthetic success row. Absence of an error means success. Direct values are
displayed as values; output shapes such as `1 item` or `6-item list` are not
displayed.

## Model and Tool Trace

Outside parallel work, every leaf Step leaves a complete trace. Model text is
incrementally projected as Markdown. The initial live row:

```text
• thinking
```

is replaced once text arrives, while stable Markdown progressively enters
scrollback:

```text
• # Deep Research Brief
  ## Executive summary

  The evidence supports explicit ownership
```

Complete Markdown blocks move to scrollback as soon as later input makes them
stable. Only the unfinished block remains live. The transition does not add an
ellipsis or visibly change already-rendered text. At Part closure the remaining
tail is committed, and Step closure does not repeat the final output.

Tool activity and terminal output use this form:

```text
• executing web_search.search
• executed web_search.search
  {"results":[{"url":"https://example.com"}]}
```

Structured Tool results use compact single-line JSON. Textual `stdout` and
`stderr` preserve their original lines. A model Tool request without a Tool
result is displayed as `• requested TOOL`.

For a streamed Model Text Part, concatenated deltas must be an exact prefix of
the authoritative Part closure. A successful Step result must contain that
same completed Part. Started Parts, child Steps, and child Runs must close
before their owning Step closes. Violations are reported as execution contract
errors rather than repaired by the presenter.

## Flow Facts

A Flow Step that owns child execution may append one unmarked facts row:

```text
[2] Search the web for each query

• Mapped all 6 items in parallel
  31.0s · 6 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

The row has no StepPath. Undefined facts are omitted. Usage and optional cost
form one `↑INPUT ↓OUTPUT $COST` fact, with cost rounded to cents. Model,
Tool, and direct-value Steps define no facts. Run facts appear only in the root
footer and aggregate the complete Run tree.

## Parallel Work

Parallel work keeps one aggregate live row and one physical row per active
lane. Lane rows are truncated rather than wrapped:

```text
• running · 4/18 succeeded · 3 active
  0 | #4 | • thinking
  1 | #5 | • executing web_search.search
  2 | #6 | • Source summary prepared
```

On success, the live lanes are cleared and one natural-language result remains:

```text
• Mapped all 7 items in parallel
• Brainstormed 7 items in parallel
• Evaluated 7 items in parallel, kept 5
• Evaluated 7 items in parallel, dropped 2, leaving 5
• Scored 10 items in parallel, kept the top 8
```

On failure, successful, active, and canceled lanes are cleared. Each failed
lane retains its causal error, followed by the parallel Step's distinct
boundary error:

```text
• Parallel execution stopped: 4/18 succeeded, 1 failed, and 2 were canceled
  1 | #5 | • failed fetch_page
             provider returned status 429

• parallel step stopped because lane 1 (#5) failed
  31.0s · 7 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

## Repeat and Settle

Repeat and Settle use the same loop presentation. Each iteration follows the
normal trace-or-lane rule for its child statement:

```text
--- iteration 1 of 3 ---

<?> completion_check

• thinking
• true
```

The condition is a child Run, not a synthetic `executed completion_check`
Step. Terminal loop output identifies the actual cause:

```text
• Completed all 3 iterations
• Condition met after 2 of 3 iterations
• Completed all 3 iterations without meeting the condition
• Interrupted after completing 2 of 3 iterations
• Canceled after completing 2 of 3 iterations
• Settled all 6 items in 6 iterations
```

The causal child error remains at the child Step or lane. The loop row describes
termination without repeating it.

## Error Ownership

Each causal error is displayed once, as close as possible to its owner:

- a Step error occupies that Step's normal output position;
- a failed lane preserves its concrete error;
- a parallel Step adds only its distinct local-failure boundary error;
- parent Step and Run pointer errors remain silent; and
- a concrete Run error without a Step owner becomes a final `• MESSAGE` row.

A malformed presentation stream also clears live state and emits one
root-owned error row. There is no separate diagnostic marker.

## Root Run Footer

Script and Chat end a root Run with the same footer:

```text
• run_nrqpt0mf succeeded ─────────────────
  1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
```

The divider is 42 cells wide: the Step-compatible `•` marker and its following
space
occupy two cells, and the caption plus trailing rule fill the remaining 40.
Narrow terminals shorten the divider without wrapping it. The marker, caption,
and rule share the terminal status style: dim for success, red for failure, and
yellow for cancellation. Facts remain dim and use the available terminal width
independently, so they may extend beyond the divider.

The footer owns total duration and whole-tree Run facts. Script does not append
a separate `Run: RUN_ID` line. Errors before `RunBegin` are reported outside
execution progress; later terminal errors belong to progress and its footer.

## Reopened Chat Result Divider

The Chat TUI `:show` command introduces a durable result with a quiet divider:

```text
• run_ma8hccd9 result ────────────────────

• Result body rendered as Markdown.
```

The `•` marker, caption, and rule are dim, while the result body retains
normal intensity. The divider follows the same fixed 42-cell width as the root
Run footer and shortens only when the available width requires caption
truncation. Exactly one blank line separates the divider from the result body.

## Surface Behavior

Script writes progress to stderr. It does not copy the durable root result to
stdout by default. `--save -` writes the result to stdout and `--save PATH`
atomically writes it to a file. Failed and canceled Runs do not write the
selected destination. Non-TTY output contains stable newline-delimited content
without color, cursor movement, or partial delta lines.

TTY script output uses one event-driven Rich `Live` area per root Run. Chat
does not create a Rich `Live` because prompt_toolkit owns its terminal; it uses
the same Rich Markdown renderables while moving committed fragments into
scrollback and retaining only replaceable fragments in its live container.

Chat submission and steer controls contain only their authored message. Their
left background-filled accent cells distinguish start from steer without
displaying Run IDs or execution state. Quick-command bars use the same
background-cell treatment with their own accent, and the prompt uses the start
accent. Control bars and the input box share one surface background color. An
empty prompt shows the muted placeholder `Ask anything`; the
placeholder disappears as soon as the buffer contains text and is never part of
the submitted message. The status bar does not paint a base background and
therefore inherits the terminal background. Its left side begins with a marker,
one space,
and the current default runnable as `agic:name` or `flow:name`. The current
default model is right-aligned against the terminal edge; hotkey hints are
omitted. Runnable and model text inherit the terminal's default foreground
without dim styling. The marker and spinner use the input background color as
their foreground without painting a status background. The elapsed time uses
the terminal's dim attribute. The default `squares` style uses `▪︎`
while idle and rotates through `◧`, `◩`, `◨`, and `◪` every 300 milliseconds
during a Run. The retained `triangles`, `quadrants`, `hatch`, and `dots` styles
remain available through an internal named style switch. A whole-second elapsed
time follows the runnable.
Short Runs retain the running state long enough to avoid flashing. This
transient UI state is never committed to execution scrollback.
