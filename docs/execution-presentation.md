# Execution Presentation Language

This document defines the shared presentation language for durable execution
inspection, one-shot script runs, and the interactive chat TUI.

It does not define another execution model or another event protocol.
`RunDetail` remains the source for completed inspection, and native `RunEvent`
values remain the source for live script and TUI rendering.


## Goals

Execution presentation should be:

- concise enough to follow during a long run;
- descriptive enough to identify the runnable, flow statement, model, tool, or
  failing item involved;
- consistent across inspection, scripts, and chat;
- stable in terminal scrollback;
- useful in both interactive terminals and redirected command output;
- faithful to the recursive run tree without exposing every internal detail by
  default.

The surfaces share vocabulary, labels, summaries, status marks, and failure
language. They do not share one renderer:

- inspection renders a completed snapshot and never renders deltas;
- script mode renders a linear live trace to stderr and the run result to
  stdout;
- the chat TUI renders colored mutable blocks, consumes deltas, and
  progressively finalizes stable blocks into terminal scrollback.


## Sources Of Truth

| Surface | Source | Live deltas |
| --- | --- | --- |
| `threads` and `runs` | `ThreadInfo` and `RunInfo` | no |
| `inspect` | `ThreadDetail`, `RunDetail`, and `StepData` | no |
| script invocation | ordered native `RunEvent` values | yes |
| chat TUI | ordered native `RunEvent` values | yes |
| restored chat history | durable thread and run detail | no |

Presentation code must not introduce display-specific execution events or query
SQLite on the live event path. A live renderer may retain bounded presentation
state needed to reconcile part deltas with `PartEnd`, `StepEnd`, and `RunEnd`.


### Required Execution Facts

Presentation depends only on execution facts that are also meaningful outside
the CLI:

- `RunBegin.parent` identifies the calling step for a child run and is `None`
  for a root run, matching durable `RunRecord.parent`;
- `RunBegin.context` identifies the root run, runnable, call kind, and parallel
  position when applicable;
- flow `StepBegin.given` contains statement kind, authored doc, source line,
  binding, runnable or predicate, and parallel position when applicable;
- model `StepBegin.given` contains the effective non-secret model target;
- tool `StepBegin.given` contains the stable tool and plugin names;
- `StepEnd.noted` contains stable shape, item count, usage, and other
  step-specific facts;
- `RunEnd.output` identifies the root run result.

`RunBegin.parent` and statement doc are execution metadata, not presentation
instructions. They let every tracer reconstruct the same recursive run tree
and semantic operation without querying the store. The remaining choices about
visibility, wording, color, truncation, and finalization stay in the caller.


## Presentation Vocabulary

The presentation layer uses these terms consistently:

| Term | Meaning |
| --- | --- |
| run result | the value referenced by the root `RunEnd.output` |
| activity | user-visible progress such as a flow statement, model call, or tool call |
| live block | a mutable TUI block that still expects updates |
| finalization | removing a stable block from the live area and writing its final form to scrollback |
| scrollback entry | immutable terminal output produced by finalization or history restoration |
| pending response | streamed model content that may become the chat run result |
| diagnostic | one user-facing explanation of a failure |
| semantic label | the authored or derived operation name shown before implementation details |
| fact | compact secondary data such as item count, usage, duration, or source line |

The following distinctions are intentional:

- `finished` is an internal durable execution status;
- `succeeded` is its user-facing presentation;
- `finalize` is a TUI presentation action;
- `settle` is reserved for the Toolang flow statement and is not a synonym for
  block completion;
- `control` means a run control such as start or steer;
- `command` is reserved for CLI, slash, and shell commands.

`output` continues to name step and event data. `result` names the root value
presented to a caller. An assistant response is the chat rendering of a run
result; not every model output is an assistant response.


## Shared Status Language

Internal execution keeps the durable status name `finished`. User-facing CLI
surfaces render that status as `succeeded`.

| Internal status | Display status | Mark | Meaning |
| --- | --- | --- | --- |
| pending | pending | `·` | accepted but not running |
| running | running | `…` | currently active |
| finished | succeeded | `✓` | completed successfully |
| failed | failed | `✗` | terminated with an error |
| canceled | canceled | `−` | stopped by control or shutdown |

`→` marks a transition that has just started. It is useful in a linear live
script trace. A mutable TUI block normally uses `…` while it remains active and
replaces it with its terminal mark before finalization.

Marks, words, and color agree. Color adds emphasis but never carries status by
itself.


## Shared Labels

### Runs

A run label contains its executable kind and unique runnable name:

```text
agic:search_web
flow:research
```

When identity is important, the run id precedes the executable label:

```text
run_8te228b5  flow:research
```

Root run ids are shown:

- on every inspection detail;
- in verbose script output;
- on every script failure;
- in the chat start control bar after `RunBegin`;
- on chat failures and cancellations.

Successful, compact chat activity does not need to repeat the run id on every
line.


### Flow Steps

Flow steps use authored semantics rather than their execution mechanism.

The primary label is selected in this order:

1. the statement's authored doc comment;
2. a concise label derived from the statement;
3. the step kind as a final fallback.

For example:

```too
## Rank the remaining evidence by relevance
rank top 8: Return only a numeric score.
```

is presented as:

```text
Rank the remaining evidence by relevance
rank top 8 · line 51
```

The underlying step kind may be `par`, but `par` is an execution mechanism and
is not the primary user-facing label.

Authored docs used as labels are stored with the step's other statement
metadata so live and persisted presentation produce the same text.


### Model Steps

A model label uses the canonical selected model ref when available:

```text
model deepseek/deepseek-chat
model openai/gpt-5
```

Useful terminal facts include:

```text
15.8k/394 tokens
4.1s
3 tool requests
```

Provider, adapter, base URL, prompt hashes, and normalized call internals are
inspection data. They are not part of the default step label.


### Tool Steps

A tool label uses the most specific stable tool name available:

```text
tool web_search.search
tool shell
```

Completion may add a compact result:

```text
5 results
exit 0
820ms
```

Arguments and complete tool output belong in verbose diagnostics or JSON.
Secret-bearing input must never be rendered merely because verbosity is high.


### Parallel Items And Lanes

Parallel work uses one-based human-facing positions:

```text
item 3/5
lane 2/4
completed 4/6
```

Durable indexes and step paths remain zero-based. Human-facing item positions
do not replace durable identities.

The number of lanes is bounded while the number of items may be large. A live
parallel operation therefore shows one aggregate progress line plus the
current item assigned to each active lane:

```text
… Search the web · 42/100 · L1→43 L2→40 L3→41 L4→42
```

Successful item completions update that line and are never appended as
individual progress lines, including at high verbosity. A failed item may be
expanded with its item position, child run identity, and bounded diagnostics.


### Time And Counts

Facts are separated with a centered dot:

```text
6/6 items · 18 tool calls · 21.7s
15.8k/394 tokens · 4.1s
```

Preferred duration forms are:

```text
420ms
4.2s
1m 08s
```

Counts use exact integers. Token counts may use compact thousands only in
human-facing output; JSON retains exact values.


## Run Presentation

### Agic Runs

An agic is presented as a model-tool loop.

Default presentation emphasizes:

- the selected agic;
- tool activity;
- the current pending response;
- the assistant response;
- failures.

Successful internal model calls need not remain visible after they have only
requested tools. The model step that supplies the root run result becomes the
assistant response.

Example live activity:

```text
… model deepseek/deepseek-chat
→ tool web_search.search
✓ tool web_search.search · 5 results · 820ms
… model deepseek/deepseek-chat
```

Example terminal summary:

```text
✓ agic:search_web · 2 model calls · 3 tools · 3.4k/620 tokens · 6.8s
```


### Flow Runs

A flow is presented as an ordered sequence of authored statements. Top-level
statement order is the main visual structure. Script progress folds successful
child runs into the statement that invoked them, even at maximum verbosity.
Inspection may expand the durable child-run tree when explicitly requested.

Example:

```text
  ✓ step 0 · Expand the research question · 6 items · 4.2s
  ✓ step 1 · Search the web · 6/6 items · 21.7s
  ✓ step 2 · Keep relevant evidence · kept 5/6 · 8.1s
  ✗ step 3 · Rank evidence · item 3/5 · line 51
```

Successful parallel children are summarized by their parent statement.
Failures expand the relevant child:

```text
  ✗ step 3 · Rank evidence · item 3/5
    └─ ✗ run run_pa74s6cc · inline agic
        ├─ ✓ step 0 · model deepseek/deepseek-chat
        └─ ✗ step 1 · output coercion
```

Completion order may update live progress, but finalized presentation uses
source order and item order.


## Step Presentation

Run and step boundaries are explicit rather than inferred from identifiers or
color:

```text
→ run run_8te228b5 · flow:research
  ✓ step 0 · Expand research queries · 6 items
  … step 1 · Search the web · 4/6
✓ run completed · 28.1s
```

Root runs are not steps. A run line always contains the word `run`; an
execution step always contains `step` plus its root-relative path. Root steps
are indented under their run. Nested root-run steps use paths such as
`step 2/0`; folded child-run steps are shown only by explicit inspection.

`StepKind` is deliberately smaller than the set of flow statements. Display
uses statement metadata to recover user intent.

| Step kind | Default presentation | Typical stable facts |
| --- | --- | --- |
| `run` | invoked runnable or statement label | runnable, output shape, item count |
| `agent` | target agent and runnable | agent, runnable, remote result |
| `human` | requested human input | prompt summary, response state |
| `model` | selected model | streamed text, tool requests, usage, duration |
| `tool` | tool name | compact result, exit status, duration |
| `par` | authored parallel statement | completed/total, concurrency, failed item |
| `loop` | authored loop statement | iteration, limit, stop condition |
| `system` | meaningful runtime operation | binding, coercion, runtime failure |


### `run`

`run` wraps a child agic or flow. The runnable name is more useful than the
generic kind:

```text
… expand_queries
✓ expand_queries · 6 items
```

For `scatter` and `gather`, the statement label remains primary:

```text
✓ scatter 6 expand_queries · 6 items
✓ gather synthesize_report
```


### `agent`

`agent` represents a cross-agent invocation such as `seek`. Presentation
identifies both target agent and runnable:

```text
… seek researcher/search
✓ seek researcher/search · 2.4s
```

Transport details are hidden unless needed for a failure.


### `human`

`human` represents a human checkpoint such as `ask`:

```text
… waiting for input
✓ received human input
```

The input surface owns the actual prompt interaction. Inspection reports the
step result without pretending that a historical run is still waiting.


### `model`

Live script and chat surfaces consume model part events. Inspection uses only
the completed output and noted usage.

Default completed forms include:

```text
✓ model deepseek/deepseek-chat · 2.1k/180 tokens · 2.7s
✓ model deepseek/deepseek-chat · requested web_search.search ×3
```

Reasoning is transient by default. It may be shown while live or summarized as
elapsed thinking, but it does not become assistant transcript unless the
canonical output defines it as user-visible content.


### `tool`

Tool execution is visible while active and finalizes as a compact line:

```text
… tool web_search.search
✓ tool web_search.search · 5 results · 820ms
```

A failed tool retains a bounded useful excerpt:

```text
✗ tool shell · exit 1
  pytest: 2 failed, 38 passed
```


### `par`

`par` is normally rendered as its authored `storm`, `map`, `keep`, `drop`, or
`rank` statement. Live output shows aggregate progress:

```text
… Search the web · 4/6
```

Final output shows one stable summary:

```text
✓ Search the web · 6/6 · par 4 · 21.7s
```

Individual successful children are shown only in sufficiently detailed
inspection or verbose script output. A failed child is always identifiable.


### `loop`

`loop` is rendered as its authored `repeat` or `settle` statement:

```text
… repeat · iteration 3
✓ repeat · 4 iterations
```

Nested steps remain under the loop. Finalized summaries do not repeat every
successful iteration unless the user requests detailed inspection.


### `system`

Successful internal system work is quiet unless it represents an authored
operation such as `let` or local filtering.

A runtime-generated failure system step is durable trace truth, but default
presentation folds it into the owning step's diagnostic. It must not print the
same error a second time.

Examples:

```text
✓ let query
✗ output coercion · expected Number
```


## Flow Statement Presentation

| Statement | Primary summary |
| --- | --- |
| `run NAME` | `Run NAME` |
| `seek AGENT/NAME` | `Seek AGENT/NAME` |
| `ask` | authored doc or `Request human input` |
| `scatter N NAME` | `Scatter with NAME` plus produced item count |
| `storm N NAME par P` | `Generate with NAME` plus completed count |
| `gather NAME` | `Gather with NAME` |
| `settle NAME` | `Settle with NAME` plus iteration count |
| `map NAME par P` | `Map with NAME` plus completed count |
| `keep ...` | `Keep matching items` plus kept/input count |
| `drop ...` | `Drop matching items` plus retained/input count |
| `rank ...` | `Rank items` plus selection and failed item |
| `repeat ...` | `Repeat` plus iteration count and stop condition |
| `let NAME` | `Set NAME` |

An authored doc replaces these generated primary summaries. The exact
statement remains available as secondary detail.


## Thread And Run Lists

`threads` and `runs` are index surfaces. They remain tables rather than traces.

Thread columns:

```text
THREAD  TITLE  RUNS  STATUS  UPDATED
```

Run columns:

```text
THREAD  RUN  TITLE  STATUS  CREATED
```

When filtered to one thread, `THREAD` is omitted. Status words use the shared
display mapping. Titles prefer a meaningful result summary and fall back to
input text.


## Inspection

Inspection presents completed durable truth. It never animates, displays a
spinner, or reconstructs historical deltas.


### Thread Inspection

Thread inspection shows:

- thread identity, status, origin, and run count;
- root runs in visible thread order;
- each run's mark, id, executable, elapsed time, and summary.

Child runs are omitted from the top-level list and remain available through
run inspection.


### Run Inspection

Run inspection uses stable sections:

```text
# run
# input
# result
# steps
# failure
```

Empty sections are omitted. `failure` is distinct from a successful `result`.

Example:

```text
# run
✗ run_8te228b5  flow:research · 42.8s
  thread script_g0k8vm63 · origin script

# input
agent framework

# steps
✓ step 0 · Expand research queries · 6 items
✓ step 1 · Search the web · 6/6
✓ step 2 · Keep relevant evidence · kept 5/6
✗ step 3 · Rank evidence · item 3/5 · line 51

# failure
Expected Number, but item 3/5 returned explanatory text.
Child run: run_pa74s6cc
```


### Step Inspection

The default step view translates `given` and `noted` into named facts:

```text
# step
✗ run_8te228b5:3  rank top 8 · line 51

Input       step run_8te228b5:2
Items       5
Concurrency 5
Scorer      inline agic at line 51
Binding     _
Failure     item 3/5 returned invalid Number
```

`--json` remains the complete protocol view for raw `given`, `noted`, input,
output, model-call data, and durable identities.


### Child Run Identity

The displayed tree must not invent ambiguous flattened paths for parallel
children. A child run boundary includes its real run id:

```text
✗ step 3 · Rank evidence
  ├─ ✓ item 1/5 · run_6390e3nf
  ├─ ✓ item 2/5 · run_v9assth7
  └─ ✗ item 3/5 · run_pa74s6cc
      ├─ ✓ step run_pa74s6cc:0 · model deepseek/deepseek-chat
      └─ ✗ step run_pa74s6cc:1 · output coercion
```

The corresponding precise inspection target is:

```bash
too SCRIPT inspect run_pa74s6cc:0
```


## Script Presentation

Script mode preserves a strict output-channel contract:

- stdout contains only the runnable's run result;
- stderr contains progress, transient delta previews, and diagnostics;
- `--quiet` suppresses progress without suppressing the result;
- logging remains separate from presentation.

This keeps pipelines safe:

```bash
too report.too research "agent frameworks" > report.md
```


### Interactive Terminal

When stderr is a TTY, a live delta preview may update in place. Intermediate
child model text is never copied to stdout.

Example:

```text
→ run run_8te228b5 · flow:research
  ✓ step 0 · Expand research queries · 6 items
  … step 1 · Search the web · 4/6 · L1→5 L2→6 L3→3 L4→4
```

When the live operation completes, its transient preview is replaced by a
stable summary. The canonical run result is written to stdout after execution.


### Non-Interactive Output

When stderr is not a TTY:

- default script execution does not emit delta fragments;
- verbose execution emits stable newline-delimited boundaries;
- terminal cursor movement and live rewriting are disabled;
- stdout retains the same run-result contract.


### Verbosity

| Level | Script stderr |
| --- | --- |
| default | direct semantic steps, aggregate progress, and failures |
| `-v` | statement details, source locations, duration, shape, and item counts |
| `-vv` | nested root-run steps and failed child-run identity |
| `-vvv` | bounded non-secret model and failed-child output previews |

Failures always include enough context to identify the source step and child
run, even without verbosity. Verbosity never enumerates successful flow child
runs or their internal model and tool steps.


## Chat TUI Presentation

The existing TUI implementation framework is a design constraint, not a
migration target.

It retains:

- non-full-screen `prompt_toolkit` operation;
- real terminal scrollback;
- the dynamic live-block area above the queue and prompt;
- mutable blocks that update and finalize progressively;
- wide start and steer control bars;
- run id insertion into the start control bar after `RunBegin`;
- aligned control-bar padding and activity markers;
- Rich Markdown output and colored status;
- the current queue, prompt, and status-bar organization.

Presentation unification changes wording and data use without replacing this
layout or block lifecycle.


### Start And Steer Controls

Start and steer are visually prominent controls, not ordinary activity lines.

The start bar contains the submitted input and initially displays `starting`.
After `RunBegin`, it displays the root run id and moves into scrollback:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
> agent framework
  run_8te228b5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

A steer uses its distinct control-bar color and remains live while pending:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
+ Focus on implementation details
  pending for next step
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

When a `StepBegin.input` shows that the steer was applied, the bar finalizes.
Its footer may identify the application boundary. Control bars retain their
existing padding and alignment with following activity markers.


### Progressive Finalization

The live area contains only live presentation blocks. A block moves into
scrollback as soon as its content and ordering are stable.

| Item | Safe finalization boundary |
| --- | --- |
| start control | root `RunBegin` |
| steer control | consuming `StepBegin` or terminal `RunEnd` |
| top-level flow statement | its `StepEnd` |
| tool activity | its `StepEnd` |
| parallel children | owning parent `StepEnd`, as one aggregate |
| internal model tool request | model `StepEnd` once classified |
| pending response | root `RunEnd.output` confirms it |
| failure diagnostic | owning visible `StepEnd` |
| cancellation | root `RunEnd` |

Top-level flow statements execute in source order, so each completed statement
can finalize immediately. Parallel child completion order updates the current
parent block but does not append child lines to scrollback in completion order.


### Live-Area Bound

The number of visible live blocks should track active execution depth, not the
total number of events or parallel children.

A large parallel statement normally needs:

```text
… Search the web · 42/100 · L1→43 L2→40 L3→41 L4→42
```

It does not need one visible block per child. The presentation state retains
only aggregate counters, the current item per active lane, aggregate usage,
and bounded failure details. Terminal child state is discarded after it is
incorporated into its parent.


### Part Streaming

Part identity is `(step, part index)`.

The TUI handles the complete lifecycle:

```text
PartBegin -> create or reset the typed live part
PartDelta -> update that part
PartEnd   -> reconcile with the canonical MessagePart
StepEnd   -> reconcile the complete ordered output
RunEnd    -> confirm the root result and terminal status
```

Text deltas update the Markdown pending response. Reasoning remains
transient or compact. Tool-call deltas update the active request description.
Image, audio, and document parts use stable attachment summaries until a richer
terminal representation exists.

The canonical part or step output wins over accumulated deltas. This prevents
duplicate text and handles adapters that omit, combine, or correct delta
fragments.


### Model Classification

Not every model output is an assistant transcript message.

- A model output containing tool calls finalizes as internal activity.
- A completed model text remains one pending response when it may
  become the root result.
- A subsequent tool or execution step can demote that response to internal
  activity.
- `RunEnd.output` promotes the confirmed root result to permanent assistant
  scrollback.
- Child model output used only by a flow remains under the owning flow
  statement.

At most one pending response should remain visible for the active
conversational branch.


### Scrollback

Scrollback is a readable transcript, not a copy of the event stream.

It contains:

- finalized start and steer control bars;
- compact finalized activity;
- final assistant content;
- one useful failure diagnostic;
- cancellation boundaries.

It excludes:

- token-by-token deltas;
- spinners and `thinking...`;
- transient parallel completion order;
- complete successful internal model output;
- duplicated step and run errors;
- secret-bearing model or tool request data.

An agic turn normally retains compact tool activity and its answer. A flow turn
may retain one line per top-level statement while folding successful child
runs into those lines.


### Restored History

Opening an existing thread reconstructs the same finalized scrollback from
durable thread and run detail. It does not replay historical deltas.

The reconstructed order is:

1. start input;
2. applied steer controls at their consuming boundaries;
3. finalized visible activity in logical step order;
4. assistant response or failure;
5. cancellation when applicable.

No separate durable chat-history store is introduced.


### Alignment And Color

The current control-bar dimensions, padding, and marker alignment are retained.
Activity wording must fit the established marker column rather than moving that
column.

Color remains TUI-specific:

- control bars keep their prominent backgrounds;
- active work is subdued but visible;
- success is low emphasis;
- failure and cancellation are distinct;
- streamed assistant text remains the visual focus.

Colors may be tuned as a palette, but presentation must remain understandable
without color.


## Failure Presentation

A failure is rendered once at the most useful visible boundary.

The preferred structure is:

```text
✗ Rank the remaining evidence by relevance
  item 3/5 · line 51 · run_pa74s6cc

  Expected: Number
  Received:
    "A relevance score of 10 requires...
     ...
     Score: 8"
```

Failure presentation should identify, when available:

- the semantic operation;
- source line;
- parallel item;
- child run id;
- expected type or contract;
- bounded actual output;
- the underlying provider or tool error when it is actionable.

A runtime-generated system failure step and its containing run failure do not
repeat the same message. Full exception chains belong in diagnostic logs.


## Multimodal Presentation

Text is rendered directly. Other canonical parts use compact stable labels:

```text
[image] diagram.png
[audio] response.wav · transcript available
[document] design.pdf
[tool call] web_search.search
[tool result] web_search.search · 5 results
```

Inspection JSON retains the complete protocol shape. Script stdout serializes
the run result according to its output type. The chat TUI keeps
attachments in transcript order and may add richer rendering without changing
the shared label.


## Implementation Boundary

Shared CLI presentation code may own pure helpers for:

- display status and marks;
- runnable and statement labels;
- item, usage, duration, and output summaries;
- safe truncation;
- failure selection and deduplication.

Inspection, script, and TUI retain separate renderers. In particular, the TUI
continues to own its existing mutable block classes, control bars, colors,
padding, live-area layout, and scrollback writes.

`toolang.execution` supplies execution truth only. Presentation concerns such
as terminal width, Rich styles, verbosity, scrollback, and stdout/stderr never
enter the executor.


### Intended TUI Naming

Implementation names should describe the visual or execution concept rather
than the event that happens to update it:

| Intended name | Responsibility |
| --- | --- |
| `MutableBlock` | common live-block update and rendering contract |
| `StartControlBlock` | submitted start input and accepted root run id |
| `SteerControlBlock` | pending and applied steer control |
| `RunStatusBlock` | running, canceling, canceled, or failed root-run boundary |
| `FlowStepBlock` | one visible authored flow operation |
| `RunStepBlock` | one `run` step that invokes a child runnable |
| `ModelStepBlock` | model part streaming and completed model output |
| `ToolStepBlock` | active and completed tool execution |
| `GenericStepBlock` | fallback for a step without a specialized block |
| `BlockKey` | live-block identity |
| `BlockFamily` | `control`, `step`, or `run` |

`RunStatusBlock` must not be named as a stop action: it is created while a run
is active and also represents failure and cancellation. `RunStepBlock` uses
the canonical step kind; the fact that its invoked run is a child is already
expressed by the run tree. A fallback block is `GenericStepBlock`, not a
default execution behavior.

The plain, non-TUI chat renderer should be named for its medium, such as
`PlainChatRenderer`, rather than `ScriptedRunRenderer`; script invocation and
plain chat are separate caller surfaces.


## Acceptance Rules

Presentation changes should verify:

- the same status and semantic label appear across inspect, script, and TUI;
- successful flow steps appear in source order;
- parallel completion order does not reorder scrollback;
- child run identities remain unambiguous;
- a delta-streamed message appears exactly once after final reconciliation;
- non-TTY script stdout contains only the run result;
- script failures identify the failing semantic step;
- parallel progress uses one aggregate line and bounded lane slots rather than
  one line per item;
- the TUI live area does not grow with completed parallel children;
- start and steer control bars retain their dimensions, run-id behavior,
  padding, and marker alignment;
- one failure is not repeated by its step, system failure, and root run;
- restored chat history has the same finalized semantic content as the original
  scrollback.
