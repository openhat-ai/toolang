# Chat TUI Execution Presentation

Status: draft implemented for live presentation; durable history restoration pending

This document defines the proposed execution presentation for Toolang's
terminal chat UI. It is intentionally self-contained so the interaction,
transcript, state, and implementation boundaries can be reviewed together.

The design adopts the execution vocabulary and compact progress style already
used by script execution while preserving the parts that make chat a distinct
surface: full-width user controls, a bounded mutable live area, terminal
scrollback, and Rich Markdown for the final assistant response.


## Implementation Status

The live presentation described here is implemented in the current working
tree. Script and chat now share terminal-independent formatting, metrics, run,
statement, call, lane, ownership, and failure state. Chat applies its own
conversation visibility and Rich rendering through `ChatRunPresenter`.

The implemented event path includes confirmed root responses, progressive
tool and statement finalization, bounded parallel lanes, repeat live context,
compact successful root footers, terminal failure and cancellation frames, and
single-owner diagnostics. The live area is a terminal-height-aware viewport
that retains complete block state while showing the latest rows with an
omission count; confirmed output is still written untruncated to scrollback.
Existing script output remains unchanged.

The implementation is covered at three levels: presenter unit tests, a
deterministic pseudo-terminal exchange, and opt-in real-provider
pseudo-terminal cases for both agic and flow runs. The real-provider cases use
the existing `live_provider` marker and `--live-model` option, so normal test
runs do not make network calls.

Restoring the execution transcript when attaching to an existing durable
thread remains a follow-up. It does not block live presentation and must use
durable schemas rather than reconstructed event deltas.


## Goals

- Make the same execution look and read like the same execution in script and
  chat.
- Preserve a conversation-first chat transcript instead of copying script
  output verbatim.
- Keep mutable activity bounded and move stable content into real terminal
  scrollback as soon as its ordering and value are known.
- Bound parallel output by active concurrency rather than total item count.
- Show one useful diagnostic for a failure instead of repeating the same error
  at step, child-run, statement, and root boundaries.
- Share execution presentation semantics without coupling prompt-toolkit and
  Rich to the script console renderer.
- Keep the implementation small enough to evolve as execution events gain
  more precise facts.


## Non-Goals

- Changing `RunEvent`, durable execution records, or executor behavior.
- Defining a second chat-specific execution protocol.
- Making the chat TUI full-screen.
- Replacing Rich Markdown for assistant responses.
- Replacing the existing input box, queue, slash commands, or status bar.
- Making script and chat use one terminal renderer.
- Requiring database reads while handling a live event.


## Surface Model

The chat terminal remains a non-full-screen prompt-toolkit application with
four vertically ordered regions:

```text
terminal scrollback       stable controls, progress, and assistant responses
bounded live area         mutable execution activity
queued input              messages waiting for a new root run
prompt and status         current input and session selectors
```

The terminal owns scrollback. The application owns only the bounded live area,
queue, prompt, and status bar. A stable block is removed from the live area
before it is written to scrollback, so the same content is never visible in
both places.

The following existing chat elements remain surface-specific:

- the startup header;
- the full-width start control bar;
- the full-width steer control bar;
- slash-command results;
- queued-message presentation;
- the prompt and shortcut bar;
- Rich Markdown assistant output.

Execution activity between a control bar and the assistant response adopts the
shared execution transcript vocabulary described below.


## Presentation Policy

Chat does not initially expose script-style `-v` or `-vv` switches. It uses one
conversation-oriented visibility policy:

- always show the submitted start or steer control;
- show mutable activity while it is useful;
- retain completed tool calls when they are directly visible work;
- retain flow statement headers and meaningful statement outcomes;
- collapse successful batched work into one aggregate;
- retain failures and cancellations;
- retain an agic root response as Rich Markdown;
- retain a flow result reference that can be reopened with `/show`;
- retain a compact root run footer with duration and usage when known;
- retain script `-vv`-level work lines, child boundaries, and atomic step facts;
- omit a flow's final model value, stale activity, repeated child output, and
  successful per-item batch details.

This policy uses script's `-vv` detail level without exposing a separate chat
verbosity switch. A flow's potentially large final value remains represented
by its durable `/show` reference rather than an inline model preview.


## Shared Vocabulary

### Identities

Run controls and step paths keep their existing distinct forms:

```text
run_abc@0       accepted start control
run_abc@1       later steer or stop control
run_abc/2       top-level step
run_abc/2/0     nested step
```

All displayed control, step, item, loop, and lane positions are zero-based.
Counts remain counts, so `4 of 12 items` is not converted into an index.


### Status

Execution status words are shared with script:

```text
succeeded
failed
canceled
running
canceling
```

The durable `finished` status is presented as `succeeded`.


### Markers

```text
·   active or successful execution output
!   diagnostic or failed execution output
↳   statement result, child-run closure, or control decision
◇   durable root result that can be reopened
◆   root run terminal summary
```

Root summaries begin at column zero and use `◆`, so they cannot be mistaken
for indented step facts or child-run closures. Control bars and slash-command
decoration are not execution markers and keep their existing appearance.


### Facts

Facts use centered dots and the same compact formatting as script:

```text
8.2s · 18 runs · 64.2k/720 tokens · 12 tool calls
run_abc/2 · 820ms · exit 0
```

The chat default normally omits successful step-path fact lines. It includes a
complete path in a diagnostic and may include compact aggregate facts at a
statement or root boundary.


### Color and Emphasis

- active mutable activity uses normal brightness;
- final assistant Markdown uses normal Rich Markdown styling;
- completed progress, metadata, aggregates, and frames are dim;
- failure diagnostics use normal-brightness red;
- cancellation uses normal-brightness yellow;
- success is communicated primarily by structure, not bright green output.

Progress must not dim the assistant response. A marker's style must not leak
into the Markdown content following it.


### Width and Alignment

Root execution output starts at the left margin. Content after a marker starts
in column three, and wrapped continuation lines align with that content:

```text
· Alpha beta gamma delta epsilon zeta
  eta theta iota kappa.
```

A flow statement header is a stage marker. All statement content uses one
two-space hanging indent, including marker-bearing work and outcomes. Step-fact
continuations use four spaces so they align beneath their marker content:

```text
[1] map summarize par 4
  Summarize each source bundle.

  Run agic summarize in parallel (12 items, 4 lanes)
  · 12 runs succeeded · 4.2s
  ↳ 12-item list saved to _ · mapped from 12 items
```

A blank line separates an authored description from its work paragraph and
separates adjacent statements; work output, child summaries, and the statement
outcome remain adjacent.

Parallel lane rows are truncated rather than wrapped. Normal prose and
Markdown wrap according to terminal cell width, including wide Unicode
characters.


## Control Bars

### Start

Submitting a message immediately creates one mutable start control:

```text
<full-width neutral bar>
> Explain the failure and propose a fix.
  starting
<full-width neutral bar>
```

The root `RunBegin` inserts the accepted run id and finalizes the bar into
scrollback:

```text
<full-width neutral bar>
> Explain the failure and propose a fix.
  run_abc
<full-width neutral bar>
```

The bar remains prominent because it represents authored conversation input,
not execution progress.


### Steer

A steer control remains mutable until the next consuming `StepBegin` or the
root `RunEnd`:

```text
<full-width steer bar>
+ Focus on the concurrency bug.
  pending for next step
<full-width steer bar>
```

When consumed, the pending footer disappears and the bar moves to scrollback.
A rejected steer leaves the run active and reports the request error in the
status area; it does not fabricate a run failure.


## Atomic Execution

### Model Activity

A model step starts as one live line:

```text
· thinking…
```

Text deltas replace that line with a bounded live Markdown preview. Deltas are
never appended to scrollback individually.

At `StepEnd`, the complete model output becomes a pending response candidate.
It is not yet stable conversation output because the run may continue into a
tool call, a child run, or another model step.

- If a later execution step begins, the candidate was internal work and is
  removed from the live area without entering scrollback.
- If an agic root `RunEnd.output` references the candidate, it becomes the
  final assistant response and is written once as untruncated Rich Markdown.
- A flow root result remains durable and is reopened explicitly with
  `/show [run_id]` instead of being expanded automatically.
- If the completed model step contains only tool requests, it may update the
  next tool activity but does not create a separate permanent assistant line.
- Model output inside batched child work updates its owning lane and never
  creates per-item scrollback.

This confirmation rule prevents internal model turns and repeated assistant
responses from accumulating in the conversation.


### Tool Activity

A directly visible tool call uses one mutable activity line:

```text
· executing web_search.search…
```

Its `StepEnd` replaces the live activity with one stable result:

```text
· web_search.search: 5 results
```

When an exit code or duration materially helps explain the result, it appears
on a dim continuation line. Routine facts remain omitted from the chat
default.

A failed tool uses the same shape with a diagnostic marker:

```text
! web_search.search: provider returned status 429
  run_abc/1 · 820ms · exit 429
```

The same error is not repeated in the root footer.

Tool results nested inside batched statements update their owning lane. They
do not finalize independently.


### Unknown Step Kinds

An unknown step kind receives a conservative fallback:

```text
· running STEP_KIND…
```

Successful unknown steps disappear unless they have a meaningful message or
result. Failures remain visible with `!` and the complete step path. The
fallback never invents a marker or status vocabulary specific to the step
kind.


## Flow Statements

A flow statement is one visible block owned by its outer statement step. It
may contain a header, bounded work, and an outcome:

```text
[2] map summarize par 4
  Summarize each source bundle.

  Run agic summarize in parallel (12 items, 4 lanes)
  · 12 runs succeeded · 4.2s
  ↳ 12-item list saved to _ · mapped from 12 items
```

The header is derived from the authored statement head and preserves source
order and binding syntax. Generated inline-agic names remain hidden. The
bracketed ordinal is a presentation position, not a durable identity.

The complete statement remains live until its outer `StepEnd`. At that point,
the stable header, work aggregate, and outcome move to scrollback together.
This prevents child completion order from reordering the authored transcript.


### Child Runs

A child `RunBegin.parent` associates that run with the owning statement. A
non-batched, directly visible child may close with a compact line:

```text
  ↳ run_reduce succeeded · 2.6s
```

The child does not repeat its final model output or resource totals when those
are already represented by the statement outcome.

When a child failure also fails its owning statement, the statement owns the
single `!` diagnostic. The failed child becomes an unmarked continuation fact;
the renderer does not place a failed `↳` closure before a second `!` marker:

```text
[1] scatter 6 expand_queries
  ! run_parent/1 failed: provider rejected the request
    run_child failed · 1.0s
```


### Batched Work

Scatter, map, keep, drop, rank, and other batched statements allocate one row
per active lane, not one row per item:

```text
[1] map summarize par 3
  Run agic summarize in parallel (12 items, 3 lanes)
  · 3 active
  0 │ item 8 | thinking…
  1 │ item 5 | executing web_search.search…
  2 │ item 7 | starting…
```

Lane assignment is presentation state only. Reusing a lane replaces its row.
Completed rows are not retained. The outer `StepEnd` replaces all lane rows
with one stable aggregate:

```text
[1] map summarize par 3
  Run agic summarize in parallel (12 items, 3 lanes)
  · 12 runs succeeded · 4.2s
  ↳ 12-item list saved to _ · mapped from 12 items
```

The live area's height is therefore proportional to declared or observed
concurrency, never total input size.

A failed or canceled statement replaces the successful aggregate marker with
one diagnostic. Partial-work facts are an unmarked continuation, not a second
execution event:

```text
[2] map search_web par 4
  ! run_abc/2 canceled
    5 runs succeeded · 27.0s · 13.6k/3.1k tokens
```

Cancellation does not invent `statement failed`; a failed statement uses its
actual diagnostic when one is available.

A canceled model step uses the semantic activity name rather than repeating
its model selector; the selector remains available in the facts line:

```text
! model call canceled
  run_abc/0 · 820ms · deepseek/deepseek-chat
```


### Settle

Settle uses one sequential live slot regardless of input size. The slot shows
the current source item and latest activity. Completion produces one aggregate
and the declared data effect. Historical per-item sections are never added to
scrollback.


### Repeat

At normal chat visibility, repeat keeps its current iteration and nested
activity in the live area:

```text
[0] repeat max 5
  === iteration 2 ===
  [0] run revise
  · thinking…
```

Nested statement ordinals restart at zero for each iteration. The `until`
evaluation does not consume an ordinal. When repeat completes, its live trace
collapses to one stable aggregate and control decision:

```text
[0] repeat max 5
  · 3 iterations completed · 8.4s
  ↳ stop repeating
```

If `until` fails or cannot coerce its result to Boolean, repeat fails and does
not print `continue` or `stop repeating`.


## Root Completion

### Success

For an agic, the final assistant response is the visually primary result. A
compact dim footer follows it when useful facts are known:

```text
· The race occurs because both workers update the same pending entry.
  Use one transaction to allocate and insert the control.

◆ run_abc succeeded · 8.2s · 64.2k/720 tokens · 3 tool calls
```

The assistant response is rendered as Markdown; the example uses plain text
only to illustrate alignment. Chat does not copy script's full success frame
because the start bar already identifies the run and the response should
remain visually primary.

A flow keeps its stable statement progress, reports its child-run count in the
root footer, and leaves the potentially expensive result in durable history.
The result reference occupies the same primary-output position as an agic
response, before the root summary:

```text
◇ result saved · /show run_abc

◆ run_abc succeeded · 58.0s · 32 runs · 39.8k/7.2k tokens
```

`/show` reopens the latest result in the current chat thread; `/show run_abc`
reopens a specific durable result. Neither form starts a new
run. Structured slash-command results end with one blank line before the next
prompt, matching the spacing boundary used by ordinary slash-command output.

If the run succeeds without a text response, the footer remains visible and a
bounded shape description may precede it:

```text
· 3-item list returned

◆ run_abc succeeded · 2.1s
```


### Failure

A root failure keeps its selected diagnostic in the primary-output position
and closes with the same compact summary shape as success:

```text
! No configured model matched the active ceiling.

◆ run_abc failed · 1.2s
```

If a tool, child run, or statement already displayed the selected diagnostic,
the root boundary contains status and aggregate facts without repeating the
message.


### Cancellation

The first interrupt requests cancellation and changes the root live state to:

```text
canceling…
```

The application remains alive for cleanup and terminal events. Cancellation is
a terminal status rather than a diagnostic, so the eventual root `RunEnd`
prints only the shared compact summary:

```text
◆ run_abc canceled · 4.1s · 2 completed · 1 active
```

A repeated cancel request is ignored while the first request is pending. A
cancel API failure appears in the status area and does not finalize the run.
Generic payloads such as `canceled` or `interrupted by user` are suppressed;
an independent cleanup or provider error remains a real `!` diagnostic.


## Progressive Finalization

Stable content moves to scrollback at these boundaries:

| Content | Finalization boundary |
| --- | --- |
| start control | root `RunBegin` |
| steer control | consuming `StepBegin` or root `RunEnd` |
| direct tool activity | tool `StepEnd` |
| parallel lanes | owning statement `StepEnd`, as one aggregate |
| flow statement | outer statement `StepEnd` |
| agic assistant response candidate | root `RunEnd.output` confirmation |
| flow result reference | root `RunEnd` |
| root success footer | root `RunEnd` |
| root failure or cancellation | root `RunEnd` |

Finalization preserves event order and block ownership. It does not simply
append whichever child finishes first.

Scrollback never retains:

- `thinking…` placeholders;
- token deltas;
- stale lane assignments;
- internal model response candidates;
- per-item successful child summaries for batched work;
- duplicated tool or child output;
- duplicated failure messages.


## Failure Ownership

The presentation state keeps a set of normalized diagnostics already shown to
the user. A diagnostic is assigned to the nearest useful visible boundary in
this order:

1. directly visible atomic step;
2. visible child run;
3. owning flow statement;
4. root completion.

A hidden batched child never consumes the diagnostic before its visible parent
can report item, run, expected shape, and complete step-path context.

Full exception chains remain in per-run logs. The TUI displays a bounded,
actionable cause.


## Live Presentation State

The live renderer consumes ordered native `RunEvent` values. It does not query
SQLite, reparse source, or infer missing executor facts.

The shared CLI presentation state owns terminal-independent execution facts:

```text
ExecutionProgress
├── runs by RunId
├── statements by StepPath
├── atomic calls by StepPath
├── completed outcomes by StepPath
├── run and statement metrics
├── child ownership and placement
├── active batch lanes
└── reported diagnostics
```

Applying an event updates these facts and identifies which semantic objects
started, changed, or completed. These changes are internal CLI state, not a
new execution event protocol and not persisted data.

Surface adapters make the remaining decisions:

- script applies verbosity and writes text through `ProgressConsole`;
- chat applies conversation visibility and builds Rich renderables;
- each surface owns width, color, live replacement, and scrollback policy.

This split shares the difficult ownership, aggregation, metrics, and failure
logic without forcing Rich blocks through an ANSI text renderer or teaching
the script renderer about prompt-toolkit.


## Proposed Module Boundaries

```text
toolang.cli.common.execution_progress
├── formatting.py     shared vocabulary and pure value formatting
├── state.py          run, statement, call, lane, metrics, and ownership state
└── script.py         ConsoleRunTracer and script visibility policy

toolang.cli.toolang.commands.chat
├── presenter.py      chat event policy and progressive finalization
├── blocks.py         chat control and Rich execution renderables
├── rendering.py      Rich/prompt-toolkit conversion and terminal width
├── tui.py            input, queue, application loop, and scrollback sink
└── transcript.py     durable history-to-stable-block construction
```

`state.py` must add semantic meaning; it must not become a collection of
parameter-forwarding wrappers. `presenter.py` owns chat-specific visibility
and block lifetime. `tui.py` should not contain execution-event routing beyond
passing events to the presenter and applying its finalized/live block changes.

The current `chat.events` responsibilities move into `presenter.py`; keeping a
second event router after the migration would add an unnecessary layer.


## Live Block Contract

A chat execution block has a stable semantic identity and one owner. Rendering
is a pure view of its current state.

The presenter exposes two results after applying an event:

- the complete ordered set of live blocks;
- zero or more newly finalized blocks in scrollback order.

The TUI applies them atomically:

1. replace the live block list;
2. invalidate the prompt-toolkit layout;
3. render finalized blocks to stdout in order;
4. leave prompt, queue, and status state unchanged;
5. finish the active root run only after its terminal block is finalized.

This contract prevents flicker, duplicated blocks, and completion-order
reordering while keeping terminal I/O outside the semantic state.


## Restored Threads

Attaching to an existing thread constructs stable conversation history from
durable thread and run detail. It does not replay historical `PartDelta`
events or recreate historical lane assignments.

The restored transcript uses the same stable rendering policy:

- controls become start or steer bars;
- a confirmed agic root output becomes assistant Markdown;
- a flow root output becomes a durable `/show` reference;
- durable tool and statement facts may produce stable progress blocks;
- failures and cancellations use the same boundary language;
- facts absent from durable schemas are omitted rather than guessed.

Live continuation then begins from newly received native events. Historical
construction and live event handling share formatting and stable block
renderers, but not an artificial reconstructed event stream.


## Terminal Behavior

- The live area must remain bounded on narrow terminals.
- Lane columns truncate by terminal cell width.
- Markdown and prose wrap with correct wide-character measurement.
- A resize invalidates live renderables without rewriting scrollback.
- Finalization removes a block from prompt-toolkit before writing it to stdout.
- Cursor hiding is scoped to one scrollback write and restored in `finally`.
- No ANSI cursor movement is emitted for finalized scrollback blocks.
- Interactive changes do not alter the existing non-TTY chat fallback.


## Validation Scenarios

The same ordered event fixtures should exercise both script and chat
presentation semantics. Raw output is surface-specific, but both surfaces must
agree on ownership, labels, facts, indexes, aggregation, and selected failure.

Required scenarios are:

- root model response with and without deltas;
- model-to-tool-to-model conversation;
- tool success, failure, exit code, and structured result;
- nested visible child run;
- scatter and map with lane reuse;
- empty and single-item batches;
- partial batch failure;
- keep/drop and rank transformation failure;
- settle with many items and one sequential slot;
- repeat with nested statements and an `until` decision;
- failed `until` evaluation and Boolean coercion;
- steer consumed by the next step and steer pending until run end;
- root failure already owned by a child boundary;
- cancellation and cancel-request failure;
- resize, narrow width, and wide Unicode text;
- removal from live area before scrollback output;
- attachment to durable history without replaying deltas.

Script regression tests remain byte-for-byte golden tests during extraction of
the shared state. Chat tests assert both rendered text and block lifetime so a
visually correct result cannot hide stale live state.


## Acceptance Criteria

- The same model, tool, statement, status, duration, token usage, and failure
  use the same words in script and chat.
- Chat execution output uses `·`, `!`, and `↳` for execution progress,
  `◇` for a durable root result reference, and `◆` for the root terminal
  summary.
- Agic assistant Markdown is emitted exactly once after root output
  confirmation; flow output is emitted only when requested with `/show`.
- Parallel live height is bounded by active lane count.
- Stable statement output follows authored order rather than child completion
  order.
- A failure diagnostic is displayed once at the nearest useful visible
  boundary.
- Start and steer bars retain their current prominence and behavior.
- Queue, slash commands, prompt editing, and interrupt behavior do not regress.
- Script progress output does not change while common state is extracted.
- Live event handling performs no durable-store reads.


## Decisions Proposed by This Draft

The following choices are part of this proposal and should be accepted or
changed explicitly during review:

1. Chat starts with one fixed conversation-oriented policy equivalent to
   script `-vv`, rather than adding `-v` and `-vv` switches.
2. Successful chat runs use a compact dim footer instead of script's full root
   success frame.
3. Every root status uses the same compact summary shape. Failure may add a
   primary diagnostic; ordinary cancellation does not.
4. A model `StepEnd` is only a pending response; agic `RunEnd.output` confirms
   inline assistant output, while flow completion exposes a durable `/show`
   reference.
5. Complex execution state is shared between script and chat, while terminal
   rendering and surface visibility remain separate.
6. Existing thread restoration is part of the target design but can be
   implemented after live presentation reaches parity.
