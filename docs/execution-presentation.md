# Execution Presentation

This document defines the execution progress language shared by script mode and
the Chat TUI. It is a presentation contract only: it does not add execution
events, records, identities, or lifecycle states.

Inspection reads durable state and may reuse the same vocabulary, but it does
not reconstruct live progress from stored records.

## Invocation and Waiting

Present run, exec, async run, spawn, and await as one family with action/target
primary and identity, result, timing, and errors in consistent secondary positions.
Their live/success meanings are Running/Completed, Transferring/Transferred,
Starting/Started, Spawning/Spawned, and Waiting/Completed respectively. Preserve
existing synchronous Run headers/footers and exec handoff boundaries.

An async launch is one caller Step that ends at admission. Optional await is one
separate blocking Step. Route the background Run tree through its own scope;
never reopen launch, insert its activity into later caller Steps, or replay it
under await. Aggregate its metrics once into the owning Run, even after its launch
Step ends. Flow/tool syntax determines the action; handle types are not UI labels.


## Operational Progress

Preparation, setup discovery, and AgentServer runtime work use `ProgressEvent`,
not `RunEvent`. One `CliProgress` segment owns each uninterrupted interval
before terminal control passes to Run output, foreground logs, a prompt UI, or
a command result. Cleanup after that handoff opens a new segment.

Producers supply complete verb-first sentences. Running work ends in `...` and
successful work uses simple past without terminal punctuation:

```text
Fetching skill browser...
Fetched skill browser
Installing Toolang from the package index...
Installed Toolang from the package index
```

A TTY uses one dim, transient row with no spinner. It delays the row for 150
milliseconds and adds per-activity elapsed time only after one second.
Successful closure clears the row without a summary. Non-TTY output writes
each material action, checkpoint, and outcome immediately as append-only,
ANSI-free stderr and never includes elapsed time.

Operational rows use the width and display-cell wrapping rules below. They do
not display agent identity; the stable command result owns it, for example
`Agent eve started: http://localhost:7001`. Failures leave one verb-first block
with the qualified stage and reason, plus a fix and log path when applicable.
Run projection remains a separate event and presentation contract.

## Projection Model

Ordered native events pass through one terminal-independent pipeline:

```text
RunEvent -> ProgressProjector -> ProgressUpdate -> surface presenter
```

At `StepBegin`, `operations.normalize_operation` maps Flow statements and runtime
Tool Steps to one presentation-only `StepOperation`: operation name, source,
target label, run scope, and tool marker. Progress rows carry derived
`tool_identity` (toolset/leaf) and `tool_status` independently of saved text.
Parallel activity and terminal rows retain both fields. Renderers add a textual
`[toolset]` prefix from the call identity, never from the implementation plugin
or by parsing summary text. Long toolset labels leave room for the description.
No icons or icon configuration are implemented. Active Step state retains that projection;
headers, lanes, timers, and terminal rows consume it. The same module recognizes
committed exec outcomes from the completion summary or a tool reply. Raw tool-name
matching belongs there, including result-only UI updates such as Chat's cwd label.
Persisted Step kinds and execution metrics keep their original meaning. Script and
Chat retain their existing source-specific styling.

`ProgressUpdate.committed` contains stable, append-only fragments in event
order. `ProgressUpdate.live` is the complete replaceable snapshot. A Step can
progressively commit a header and output while retaining only its unfinished
tail in live state.

Script and Chat use the same projected ownership, content, aggregates, facts,
and errors. Their presenters own terminal mechanics such as wrapping, lane
truncation, scrollback, and live-area replacement.

The shared projector emits no standalone Run header. Script adds the root
context header described below; Chat retains its existing run control bar.
A root Run contains projected Steps followed by one footer. Child Runs are represented through the Steps they
execute rather than separate Run headers or closure rows. A model-produced
dynamic Run Step owns its own opening and closing divider around that child
content.

## Script Run Context

Script prints one persistent context header to stderr when the root Run begins:

```text
‣ agic:review                  deepseek/deepseek-chat · auto
```

The entire header is dim on the terminal's default background. `‣` marks the
start, with the runnable at the left edge and model/reasoning ending two cells
inside the available progress width, aligned with Step and Run footers. At
least two spaces separate the fields. Narrow layouts put model/reasoning below
the runnable, with two-cell indentation; long fields wrap without truncation.
If fewer than three content cells remain, drop the marker and indentation;
reduce the right inset only to retain one content cell. Terminal controls are
escaped as literal text.

The runnable uses the resolved root identity, displaying an unnamed entry as
`agic:` or `flow:`. The model is the resolved initial run-level setting;
Flow steps and child Runs may select other models. Reasoning shows an explicit
effort or token budget, or `auto` when unspecified. A missing model request
shows `model unspecified` with no reasoning field.

The header appears once, before Step output, separated by one blank line. It
remains in scrollback after success, failure, or cancellation. Preparation and
submission failures before `RunBegin` produce no header. Nested Runs do not
repeat it. Non-TTY output retains the layout without ANSI; `-q` suppresses it.
Result saving, stdout, and the existing root footer are unchanged. Chat TUI
does not display this Script header.

## Width and Alignment

Shared output/input limits and display-cell measurement belong to
[Terminal UI](terminal-ui.md#geometry). The rules below specify semantic
Step/Run alignment within that available width.

## Chat Input and Queue Controls

[Chat](chat.md#queue-and-steering) owns keyboard dispatch, queued snapshots,
steer adoption, and terminal input/Queue appearance. Input, Queue, and both
status bars share the input-width limit, independently of execution output.
Rendering must preserve the draft, focus, selection, and scrollback through
live-area updates and resize.

## Markers and Style

`•` marks Model and Flow Steps; `›` marks ordinary tools; `✧` marks the `pick`,
`compact` and `honor` runtime helpers. `┌` and `└` frame a dynamic Run Step,
and `▪︎` marks the root Run footer. The centered dot `·` is only
an inline facts separator.

- Model activity and output use `•` and normal text.
- Ordinary and runtime Tool markers and summaries use normal intensity while
  running and dim intensity when finished, with a separate red error line on
  failure.
- Flow activity and terminal output use `•` and normal text.
- Model and Flow failures use red; cancellation uses yellow.
- Parallel lanes place the Step marker after the lane columns. The lane number
  and both `|` separators are dim; the item identity `#N` has normal intensity.
  These styles remain independent of the activity's dim or error styling.
- Headers and facts are dim.

Model and Flow markers remain unstyled, independently of their following content.
Successful Model and Flow outputs use the terminal's default foreground.
Green is not a terminal status color.

Step paths appear only at the right edge of facts-bearing Flow Step footers. A
dynamic Run Step footer instead identifies its direct child Run. Binding
effects, result pointers, and control decisions are not displayed. Model and
Tool Steps also omit model name, exit code, usage, cost, and other per-Step
facts. Compact alone shows elapsed time while running and its duration when
done.

## Agic Dynamic Run Steps

A Run Step owned by an Agic Run uses a flat divider scope. Ownership is
identified by the enclosing `RunBegin.runnable`, while the compact flow operation
provides the target label. Its header begins in column zero and uses the
canonical resolved runnable ref:

For synchronous `runtime/run`, the child names the still-running Tool Step as
its parent, using the same event nesting as a flow Run Step. Open the divider at
the child's `RunBegin`, accumulate child metrics at `RunEnd`, and close it at the
enclosing `StepEnd`. The footer reflects the enclosing operation's status.
Runtime tool headers and footers use their saved summaries when present, with
the separate toolset prefix; native Flow retains its own captions.

```text
┌ Run agic:summarize ───────────────────────────────────────────────────

• Summary text from the child.

└ 2s · 1 run 1 model ────────────────────────────── succeeded run_abc123
```

Run boundaries pair `┌` and `└`, each followed by one space. Captions, facts,
elastic `─` rules, child Run IDs, and successful statuses are dim. Failed and
canceled statuses use normal-intensity red and yellow without affecting the
surrounding fields.

The header contains no Run ID. It displays the resolved `agic:NAME` or
`flow:NAME`; a failure before resolution displays bounded terminal-safe request
text, or `request` when no text is available. The footer's right field is
`STATUS CHILD_RUN_ID`, using the complete direct child identity rather than the
owning StepPath. Its facts aggregate that child's complete Run tree in the
normal order. A failure before child acceptance has no facts or invented
identity:

```text
• Runnable not found: missing

└ ────────────────────────────────────────────────────────────── failed
```

Every structural marker remains in column zero; nested dynamic calls do not
introduce indentation. An agic child retains normal Model and Tool traces. A
flow child retains its numbered Flow headers and StepPath footers, so the
caller-owned dynamic boundary and callee-owned grammar stay distinct. Dynamic
calls inside compact parallel lanes remain one physical lane row.

At narrow widths the renderer shortens the leader first. Facts then wrap at
fact boundaries under a two-cell hanging indent, followed by a final leader
and complete status-plus-ID field. Long captions and identities fold by display
cells without truncation. Exactly one blank row follows the header, precedes the
footer, and follows the footer; adjacent child-owned gaps coalesce.

A confirmed tool `exec` displays its saved terminal summary and toolset in one
dim boundary before the target's first Step (or at Run end if no Step starts):

```text
- [runtime] Switched to runnable: agic:delegate ─────────────────────────
```

Native Flow exec and historical records without a summary retain the
`exec → target` caption. Exec boundaries use a short `-` followed by one space.
Run and exec captions start in the third column, use sentence case, and extend with thin
rules to the same right edge. Long target names wrap under a two-cell indent
without truncation. Script and Chat share the projector and renderer.

## Flow Headers

A Flow Step uses its non-empty authored doc comment as the header. Without a
doc comment, the presenter generates a short sentence from compact flow metadata. Named
and inline runnables use the same templates and preserve their names exactly,
including the inline adhoc identity `agic:<adhoc:32>` (the inline
declaration's source line). For example, an inline map displays
`Map each item with agic:<adhoc:32>`.

| Statement | Automatic description |
| --- | --- |
| content `let NAME = BODY` | `Set value to NAME` |
| `run` | `Run R` |
| `seek` | `Ask agent AGENT to run R` |
| `ask` | `Ask for human input` |
| `generate` | `Generate into N items with R independently` |
| `reduce` | `Reduce all items into one with R sequentially` |
| `map` | `Map each item with R` |
| positional `keep` or `drop` | `Keep/Drop the first/last N items` |
| predicate `keep` or `drop` | `Keep/Drop items where P is true` |
| `sort` | `Sort items by R in ascending/descending order` |
| fixed `repeat` | `Repeat N times` |
| bounded conditional `repeat` | `Repeat up to N times, until P is true` |
| condition-only `repeat` | `Repeat until P is true` |
| unbounded `repeat` without a condition | `Repeat indefinitely` |

Explicit lane limits append `, one at a time` for one lane or `, up to N at once`
for larger limits. A named statement binding (`let NAME = STMT`) then appends
`, save result to NAME`; an unbound `let STMT` appends `, discard result`.
These describe Flow locals, not persistence. Plain statements have no binding
suffix, and content `let` does not repeat its assignment as a suffix.

Counts of one use singular `item` or `time`; positional selection omits the
number for one item. Completion summaries report actual results.

A direct single-Run Flow Step preserves that Run's leaf trace and emits no
synthetic success row. Absence of an error means success. Direct values are
displayed as values; output shapes such as `1 item` or `6-item list` are not
displayed.

Headers describe intent, live rows report current activity, and final rows
report actual results. Authored headers can explain the domain-specific purpose
without changing the operation's result vocabulary:

| Statement | Live content | Final content |
| --- | --- | --- |
| content `let` | No synthetic activity | Actual value |
| `run` | Child Model or Tool activity | Child output, without a duplicate wrapper summary |
| `seek`, `ask` | No synthetic activity | Currently fail with a missing execution/input bridge error |
| `map` | Item counts and lane activity | `Mapped 6 items` |
| `generate` | Item counts and lane activity | `Generated 6 items` |
| predicate `keep` | Item counts and lane activity | `Kept all 6 items` or `Kept 4 of 6 items` |
| predicate `drop` | Item counts and lane activity | `Dropped all 6 items` or `Dropped 2 of 6 items; 4 remaining` |
| positional `keep`, `drop` | No synthetic activity | Actual selection, for example `Kept the first 4 items out of 6` |
| `sort` | Item counts and lane activity | `Sorted 6 items descending` |
| `repeat` | Iteration/condition boundaries and child activity | Completed iterations and the termination cause |
| `reduce` | Iteration boundaries and child activity | `Reduced all 6 items in 6 iterations` |

For example, `Search the web for each query` ends with `Mapped 6 items`,
`Keep evidence bundles that answer the research task` with `Kept all 6 items`,
and `Prioritize the strongest evidence` with `Sorted 6 items descending`.
`Keep the first 8 items` may also end with `Kept all 6 items` when only six
items exist. Result summaries do not claim parallel execution: the same
operations can run with the header suffix `one at a time`.

## Model and Tool Trace

Outside parallel work, every leaf Step leaves a complete trace. Model text is
incrementally projected as Markdown. The initial live row:

```text
• Thinking
```

is replaced once text arrives, while stable Markdown progressively enters
scrollback:

```text
• # Deep Research Brief
  ## Executive summary

  The evidence supports explicit ownership
```

Complete Markdown blocks move to scrollback as soon as later input makes them
stable. Only the unfinished block remains live. The transition does not visibly
change already-rendered text. At Part closure the remaining tail is committed,
and Step closure does not repeat the final output.

Markdown blocks reserve two cells at the right of the lesser of the available
width and `TOOLANG_PROGRESS_MAX_WIDTH`, aligned with execution footers. After
the row prefix, tables stretch to the remaining content width and fold cell
overflow, list markers start at the row prefix, and quoted content keeps the
two cells its `▌ ` bar does not use. Fenced code fills the content width as
one rectangular Code surface.

Tool activity uses the persisted running description, replaced at completion.
Built-in tool summaries and the default fallback do not append progress dots;
argument punctuation and width-driven truncation ellipses remain intact:

```text
› [web] Searching: “Toolang plugin protocol”
› [web] Searched: “Toolang plugin protocol”
```

Active tool markers and their full summaries use normal intensity, matching
`• Thinking`. Completed tool markers and summaries are dim; error diagnostics
retain their error styling. Completed traces contain the terminal description
only. Tool-owned descriptions can provide clearer wording and logical workspace
labels:

```text
› [fs] Listed files: repo://
✧ [runtime] Loaded rules for repo://src/file.py
✧ [runtime] Loaded guidance: skill/python-testing
› [fs] Wrote file: repo://src/example.py
› [history] Read steps: run_abc
› [history] Read more steps
› [fs] Failed to read file: repo://missing.txt
  File not found
```

History summaries name the requested threads, runs, steps, or output and any
explicit thread/run reference. Cursor-only requests say `more threads`, `more
runs`, or `more steps`; summaries neither decode nor display the cursor.

Every Step begins after one unpainted blank line, including model output that
follows a Tool Step. A preceding statement, iteration, or condition header can
own that same separator through its trailing blank row; the following Step
does not add a second one. Continuation rows from the same Step do not add
another separator. Running, successful, and canceled tools occupy one physical
line. Failed tools occupy two: the summary and a two-cell-indented error, without
an intervening blank row or background surface. Long lines are truncated to the
available width, bounded by `TOOLANG_PROGRESS_MAX_WIDTH`. Script and Chat use
the same layout; non-TTY output omits ANSI sequences and live replacement.

The executor records a human-readable `summary` when the Tool Step begins and
another when it ends. A leaf tool may supply
`summary(arguments, result=None) -> str | None`, including through
`@tool(summary=...)`. No result means running; `ToolResult.error` distinguishes
failure from success, while cancellation uses executor wording.
The hook receives isolated call/result data with sensitive
arguments masked. It performs no I/O and supplies no markers, styling, or timing.
The executor uses generic wording when the hook is absent, blank, nontext, or raises.
Progress reads the saved summaries; it never invokes plugins during replay.

The default summary uses the leaf and first supplied argument in schema order,
or input order when no schema key matches. Its forms are `Executing NAME: ARG`,
`Executed NAME: ARG`, `Failed NAME: ARG`, and `Canceled NAME: ARG`. Supplied empty,
null, false, and zero values remain visible. Previews are masked, single-line,
and at most 80 characters. No arguments means no colon or invented defaults.
Only failure adds a diagnostic continuation. Custom summaries cancel as
`Canceled: <running summary>`, except shell uses `Canceled: command`.
Honor retains the input paths in all states; its result keeps the loaded rule files.

Historical Steps without summaries retain the compatibility forms `executing
TOOL`, `executed TOOL`, `failed TOOL`, and `canceled TOOL`.

Tool results, including JSON, stdout, and stderr, are not displayed as progress
blocks. They remain in records and model messages. Dynamic run and handoff
calls retain their hierarchy. Model `ToolCallPart` values are not displayed;
the following Tool Step owns the visible call activity. Mixed Model
output retains its text and other displayable Parts. A successful Model Step
containing only tool-call Parts commits no terminal row and leaves its live
position to the following Tool Step. This presentation suppression does not
change execution status, records, metrics, or footer facts.

For a streamed Model Text Part, concatenated deltas must be an exact prefix of
the authoritative Part closure. A successful Step result must contain that
same completed Part. Started Parts, child Steps, and child Runs must close
before their owning Step closes. Violations are reported as execution contract
errors rather than repaired by the presenter.

## Flow Step Footer

A Flow Step that owns child execution may append one dim footer:

```text
[2] Search the web for each query

• Mapped 6 items
  31s · 6 runs 12 models 8 tools · ↑18.4k(17%) ↓5.2k(3.1k) ≈$0.01        run_root.2
```

Facts retain their two-cell indentation at the left, and the complete canonical
StepPath is right-aligned two cells inside the available progress width. At
least two cells separate the fields. When they do not fit together, facts wrap
under the same indent and the untruncated StepPath follows on a right-aligned continuation
line. Undefined facts are omitted, and a StepPath is not displayed by itself.
Duration, execution counts, and usage are separate facts. Tokens and cost form
one usage group, separated by a space. Counts form one
`RUNS runs MODELS models TOOLS tools` group and omit zero categories.
All CLI duration displays use one compact format: `250ms`, `1s`, `1m8s`,
`1m0s`, or `1h1m1s`. This includes run/step facts, the Chat run status bar,
operational progress, and agent uptime (prefixed with `up `). Positive durations
below one second retain milliseconds; other durations round to whole seconds.
Units have no spaces or zero padding, and lower zero units remain visible.
Zero or negative durations render as `0s`. Chat's live timer and agent uptime
continue to floor their clock values before formatting. Stored timestamps
retain their original precision.
Token usage is `↑INPUT(CACHE%) ↓OUTPUT(REASONING)`: the input parenthetical is
the complete cache-read ratio, while the output parenthetical is the reasoning
token count. Output is inclusive and already contains reasoning. Explicit zero
reasoning renders `(0)`, partial known reasoning adds `+`, and unknown
reasoning omits the parenthetical. Exact zero cost is omitted. Positive cost
uses two decimal places when nonzero, then four; smaller exact and estimated
amounts render as `<$0.0001` and `≲$0.0001`. `≈$` marks an ordinary estimated
cost. Model, Tool, and direct-value Steps define no footer facts. Run facts
appear only in the root footer and aggregate the complete Run tree.

The footer immediately follows the owning Step's last visible output. A direct
single-Run Flow Step therefore places its footer directly after its child Model
output without opening another visual section. The normal trailing blank row
still separates the completed Flow Step from the next Step or root footer.

## Parallel Work

Parallel work keeps one aggregate live row and one physical row per observed
lane. A lane retains its latest activity until reuse or Step closure. Lane rows
are truncated rather than wrapped:

```text
• Running · 3 active · 4/18 succeeded
  0 | #4 | • Thinking
  1 | #5 | › [web] Searching: “agent runtimes”
  2 | #6 | • Source summary prepared
```

Counts describe items across all lane reuse. Running summaries contain only
`failed`, `active`, and `succeeded`, in that order. Active children include those
still canceling; their individual lane activity can say `canceling`. Queued and
canceled counts do not appear in the running summary. For example:

```text
• Running · 1 failed · 2 active · 2/6 succeeded
```

Terminal summaries contain `failed`, `canceled`, `not started`, and `succeeded`,
in that order. Only the success count includes the known total, and it always
appears last. Zero-valued counts are omitted except for `succeeded`. Before any
child starts, the summary is `Running · 0 succeeded`, without an invented total.
A terminal summary never labels unstarted items as canceled.

On success, the live lanes are cleared and one natural-language result remains:

```text
• Mapped 7 items
• Generated 7 items
• Kept 5 of 7 items
• Dropped 2 of 7 items; 5 remaining
• Sorted 10 items descending
```

On failure, successful, active, and canceled lanes are cleared. Each failed
lane retains its causal error, followed by the parallel Step's distinct
boundary error:

```text
• Stopped · 1 failed · 2 canceled · 11 not started · 4/18 succeeded
  1 | #5 | › failed fetch_page
             provider returned status 429

• parallel step stopped because lane 1 (#5) failed
  31s · 7 runs 12 models 8 tools · ↑18.4k(17%) ↓5.2k(3.1k) ≈$0.01
```

Cancellation uses the same counts without inventing a failure:

```text
• Canceled · 3 canceled · 11 not started · 4/18 succeeded
```

## Repeat and Reduce

Repeat and Reduce use the same loop presentation. Each iteration follows the
normal trace-or-lane rule for its child statement. Repeat condition markers
appear at the authored position and belong to the owning repeat, including when
a named Flow condition contains a nested loop:

```text
───────────────────────────────── 1/3 ──────────────────────────────────

<?> Run completion_check to check whether to break

• Thinking
• true
```

Iteration captions show only the count, such as `1/10`, dim and centered
between thin rules. Odd remaining widths add one cell to the right rule. If
there is insufficient room for three rule cells on each side, use the
left-aligned `- 1/10` layout with wrapping. When the total is unknown, show
only the current count: `1`.

The condition is a child Run, not a synthetic `executed completion_check`
Step. Generated condition names use `<?> Check whether to break` instead of
exposing an internal name. Terminal loop output identifies the actual cause:

```text
• Completed all 3 iterations
• Condition met after 2 of 3 iterations
• Completed all 3 iterations without meeting the condition
• Interrupted after completing 2 of 3 iterations
• Canceled after completing 2 of 3 iterations
• Reduced all 6 items in 6 iterations
```

Terminal iteration counts include only completed passes. A leading or middle
condition may exit with zero completed passes while retaining prefix work.
Historical Human and JSON trees preserve the same authored order. The causal
child error remains at the child Step or lane; the loop row describes termination.

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
▪︎ run_nrqpt0mf succeeded        1m16s · 26 runs 32 models 8 tools · ↑43.8k(12%) ↓17.6k(9.2k) ≈$0.01
▪︎ run_nrqpt0mf failed           1m16s · 26 runs 32 models 8 tools · ↑43.8k(12%) ↓17.6k(9.2k) ≈$0.01
▪︎ run_nrqpt0mf canceled         1m16s · 26 runs 32 models 8 tools · ↑43.8k(12%) ↓17.6k(9.2k) ≈$0.01
```

A CLI rerun identifies the operation in the same footer instead of
appending a separate result line:

```text
▪︎ run_zvczap2h: rerun succeeded        2s · 1 model
```

The `▪︎` text-style small square marks the complete root Run; square brackets
do not frame the footer. The Run caption stays at the left while facts end two
cells before the available width's right edge. At least two spaces separate the
fields, with no centered dot before the first fact. When both fields do not fit,
the caption is followed by facts wrapped on two-cell-indented continuation lines.

The marker and a successful Run caption are dim in the terminal's default
color. Failed and canceled captions use red and yellow respectively. Facts
always remain dim in the default color, independent of status.

The footer owns total duration and whole-tree Run facts. Script does not append
a separate `Run: RUN_ID` line. Errors before `RunBegin` are reported outside
execution progress; later terminal errors belong to progress and its footer.

## Reopened Chat Output Divider

The Chat TUI `/output` command introduces durable run output with a quiet
divider. `/show` is a compatibility alias:

```text
• run_ma8hccd9 output ────────────────────

• Result body rendered as Markdown.
```

The `•` marker, caption, and rule are dim, while the result body retains
normal intensity. The divider uses a fixed 42-cell width and shortens only when
the available width requires caption truncation. Exactly one blank line
separates the divider from the result body.

## Surface ownership

Script sends progress to stderr and leaves results in durable history unless
`--out` requests stdout or a file. Quiet mode suppresses prepare/execution
progress. [Scripts](scripts.md) owns invocation/output selection;
[Terminal UI](terminal-ui.md) owns live areas, display-cell widths, Markdown,
palette discovery, and TTY/non-TTY mechanics.

Chat's submitted controls, queue, session/run status, and command-result blocks
belong to [Chat](chat.md#control-bars-and-status). They consume progress without
introducing execution states or changing control adoption.

## Implementation evidence

- [Progress projection](../src/toolang/cli/common/execution_progress/projector.py), [operation normalization](../src/toolang/cli/common/execution_progress/operations.py), [control bars](../src/toolang/cli/common/control_bars.py)
- [Projector tests](../tests/unit/cli/test_execution_progress_projector.py), [Script presenter](../tests/unit/cli/test_script_run_presenter.py)
- [Chat TUI tests](../tests/unit/cli/test_chat_tui.py), [tool identity](../tests/unit/cli/test_tool_identity_progress.py), [Markdown](../tests/unit/cli/test_markdown_rendering.py)
