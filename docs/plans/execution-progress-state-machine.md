# Execution Progress State Machine

## Goal

Define one strict, terminal-independent state machine that converts the
canonical execution events from
[#270](https://github.com/openhat-ai/toolang/issues/270) into ordered live and
terminal presentation effects for script mode and the prompt-toolkit Chat TUI.
The machine validates causality before changing presentation state, supports
interleaved parallel work by identity, and never reads durable storage or
creates execution entities.

Implementation starts only after #270 and this definition are approved.


## Success Criteria

- Script and Chat use one event validator, ownership model, state transition
  implementation, diagnostic selector, and semantic block vocabulary.
- Every event is accepted or rejected before state changes or effects occur.
- Runs, Steps, Parts, attempts, and live blocks have explicit stable keys; no
  transition depends on a single current-event stack.
- Parallel events may interleave while Parts affect only their owning Step and
  lane rows remain bounded by declared lane count.
- Every accepted Step and root Run closes exactly once, including failure,
  cancellation, runtime failure, retry, and interrupted-stream boundaries.
- The core imports no terminal, prompt-toolkit, Rich, CLI command, store, or
  history module.
- Event-sequence tests and surface golden tests deterministically cover every
  event and lifecycle variant.


## Dependency And Approval Boundary

This definition consumes the exact target contract in #270 and its approved
definition PR, including:

- `RunKind = agic | flow`;
- terminal Run and Step success named `succeeded`;
- `StepKind = run | agent | human | model | tool | par | loop | value`;
- direct `activation`, `kind`, `given`, `noted`, structured error, and failure
  fields on canonical events;
- normalized `iter/iters`, `item/items`, and `lane/lanes` placement;
- Flow result and commit facts; and
- no `Stage`, `Iteration`, or synthetic runtime-failure Step.

If #270 changes any of those decisions, reconcile this document before
implementation. Compatibility with the current legacy event shape is not part
of this feature.


## Verified Current Behavior

The current implementation has useful shared value objects in
`cli/common/execution_progress`, but script's `ConsoleRunTracer` and Chat's
`ChatRunPresenter` still interpret events separately.

| Event | Script today | Chat TUI today |
| --- | --- | --- |
| `RunBegin` | Creates a Run state, prints the root header, or associates a child with a statement. | Creates a Run state, finalizes a local submission bar, opens a root footer, or associates a child with a statement. |
| `StepBegin` | Classifies a Flow statement versus a call, opens output, and selects a batch/repeat owner. | Repeats that classification, finalizes steer bars, discards stale model candidates, and creates a Rich block. |
| `PartBegin` | Ignored. | Ignored. |
| `PartDelta` | Text deltas update a model preview or the selected statement activity. Other deltas are ignored. | The same text-only routing is implemented separately. |
| `PartEnd` | Ignored; final Step output is trusted instead. | Ignored; final Step output is trusted instead. |
| `StepEnd` | Records metrics and output, selects a diagnostic, and renders or collapses the Step. | Repeats those decisions and additionally retains a possible final model response. |
| `RunEnd` | Aggregates child metrics, renders child closure or repeat decisions, and prints the root frame. | Repeats aggregation and decisions, confirms an agic response or Flow result reference, and finalizes the root footer. |

Neither path validates event order. Unknown, duplicate, inconsistent, and
ownerless events are commonly ignored after partial mutation. State is keyed
by Run ID and StepPath, but Parts are not tracked and retry activation is not
part of identity. Diagnostic de-duplication compares strings rather than
following structured causal failure. Both surfaces derive iteration and until
state from legacy `loop` and `role` placement.

`ProgressConsole` already separates stable writes from replaceable TTY lines.
The Chat TUI already removes a live block before writing its final form to real
terminal scrollback. Those mechanisms remain useful sinks; they must stop
interpreting execution events.


## Scope

This feature defines:

- state and identity for one observed root activation and its Run tree;
- complete acceptance, rejection, and lifecycle rules for all Run events;
- canonical live and terminal execution blocks and their ownership;
- deterministic effects for append, live display, replacement, clearing, and
  finalization;
- model/tool output, child Run, Flow statement, parallel, settle, repeat,
  failure, cancellation, runtime-failure, retry, and shutdown transitions;
- TTY, non-TTY, resizing, narrow-width, and incomplete-stream behavior;
- script and prompt-toolkit sink responsibilities; and
- event-sequence and rendered-output acceptance tests.

It does not change execution records or events, scheduler or executor
behavior, providers, authored `.too` syntax, inspect output, start/steer input
controls, prompt/status layout, or historical reconstruction. Script-specific
width, `let`, and nested-error wording remains with #260. Chat-specific bottom
bar and summary wording remains with #261. Those definitions may alter how a
block is rendered, but not its identity, fields, owner, lifecycle, or effects.


## Machine Boundary

One `ExecutionProgress` instance observes exactly one root activation. It is
created with the expected root Run ID and activation reference. A retry of the
same Run ID has a different activation and creates a new instance; completed
attempt scrollback is not mutated or replayed. This keeps retry history
visible and prevents a reopened Run from colliding with terminal state.

The only live input is the ordered `RunEvent` stream from #270. The caller may
also close the stream through the shutdown operation defined below. Invocation
metadata already known to a command may configure a surface policy, but it may
not supplement or repair an event.

The core must not:

- query `RunStore`, history, SQLite, an API, source files, or a watcher;
- infer a Run, Step, Part, lane, result, status, or execution event that was
  not supplied by the canonical stream;
- parse authored source to recover missing statement facts;
- emit terminal escape sequences or Rich/prompt-toolkit objects; or
- accept a legacy event by translating its fields.


## Identity And State

```text
AttemptKey = (RunId, RunControlRef)
StepKey    = StepPath
PartKey    = (StepPath, part_index)
BlockKey   = (AttemptKey, owner_kind, owner_id)
```

StepPath remains globally identifying within an observed activation because
#270 gives retry Steps fresh physical indexes. Every Step also stores and
checks its activation. Part indexes are zero-based and Step-local. Block keys
include the root attempt so a sink may retain earlier retry output safely.

```text
ExecutionProgress
├── lifecycle: open | terminal | detached | broken
├── root: AttemptKey
├── runs: AttemptKey -> RunState
├── active_attempt: RunId -> AttemptKey
├── steps: StepPath -> StepState
├── parts: PartKey -> PartState
├── sibling_order: (RunId, parent StepPath?) -> last physical index
├── statements: StepPath -> StatementState
├── pending_models: RunId -> completed model candidate
├── blocks: ordered BlockKey -> live block
├── metrics: attempt and statement aggregates
└── diagnostics: causal failure -> selected visible owner
```

`RunState` retains its begin event, parent Step, root, placement, status,
children, active Steps, metrics, and terminal event. `StepState` retains its
begin event, parent Step, active Parts, completed Part data, child Runs,
presentation owner, status, metrics, and terminal event. `PartState` retains
its declared type, accumulated delta preview, and optional final data.

`StatementState` owns authored header facts, derived presentation ordinal,
child Runs, completed/failed counts, active lanes, current settle/repeat
context, commits, and selected diagnostic. An ordinal is display context, not
an execution identity: top-level statements follow begin order, and repeat
body ordinals restart at zero for each `iter`.

Metrics count Run, model Step, and tool Step begins, not only successful ends.
Known/missing token and cost contributions follow the approved script-summary
definition. A child attempt aggregate is merged into its parent statement and
Run exactly once at child `RunEnd`.


## Transactional Application

`apply(event)` has three phases:

1. Resolve every referenced owner and validate the complete event against an
   immutable view of current state.
2. Build the next state and the complete ordered effect batch without calling
   a sink.
3. Commit the next state and return the immutable effect batch.

Any failed rule raises `ProgressSequenceError` containing event type, event
identity, and rule name. State remains byte-for-byte equivalent and the effect
batch is empty. Exact duplicates are errors rather than idempotent no-ops.
Conflicting duplicates use the same error type with the mismatched field.

A sink applies one returned batch atomically in order. If sink application
fails, the caller stops feeding events and performs broken shutdown; it must
not retry a partial batch against the same machine.


## Effect Protocol

```text
Append(block)
ShowLive(block)
UpdateLive(key, block)
ClearLive(key)
FinalizeLive(key, block)
```

- `Append` writes one terminal block after all prior terminal blocks.
- `ShowLive` registers a new live key at its event-order position.
- `UpdateLive` replaces the value of an existing live key without changing
  its order.
- `ClearLive` removes a live key without adding scrollback.
- `FinalizeLive` atomically removes a live key and appends its terminal
  replacement at that key's logical position.

Only block names ending in `Progress` are live and removable. All other block
names are terminal. A terminal block cannot be updated or cleared. The sole
replacement transition is `FinalizeLive`, whose final block keeps the same
owner but has a non-`Progress` name. Showing an existing key, updating or
clearing an absent key, finalizing to a different owner, or appending a
`Progress` block is a sink contract error.

Effects contain semantic block data and logical indentation, never already
wrapped strings. No effect contains a raw execution event.


## Canonical Block Catalog

| Block | Owner | Lifetime | Required semantic content |
| --- | --- | --- | --- |
| `ModelProgress` | model Step | live | StepPath, model label, accumulated Part previews, active/completed state |
| `ToolProgress` | tool Step | live | StepPath, tool name, call identity, active state |
| `StatementProgress` | Flow Step | live | ordinal, source head, doc, work description, iteration history, aggregate, active slots/lanes |
| `ModelStep` | model Step | terminal | final output or completion label, status, diagnostic, duration, model and usage facts |
| `ToolStep` | tool Step | terminal | final tool result, status, diagnostic, duration, exit/result facts |
| `FlowStep` | Flow Step | terminal | header, stable work/iteration history, aggregate, result/commit facts, status, diagnostic |
| `RunResult` | root attempt | terminal | Run kind, output reference, result shape, and available event-carried Parts |
| `RunDiagnostic` | root attempt | terminal | one inline runtime failure or otherwise unowned terminal diagnostic |
| `RunSummary` | root attempt | terminal | Run ID, terminal status, result state, duration, descendant/call/usage aggregates |
| `StreamInterrupted` | root attempt | terminal | last accepted event identity and neutral incomplete-stream message |

Child Run, lane, iteration, commit, and control-decision rows are structured
children of the owning `StatementProgress` or `FlowStep`; they are not
top-level blocks. This ensures child completion cannot reorder authored Flow
Steps. `RunBegin` creates state only and emits no block or effect.


## Shared Block Grammar

The following grammar fixes semantics and alignment. Color, Markdown styling,
and optional visibility are sink policy.

```text
. STEP_OUTPUT
  CONTINUATION_OR_FACTS

! DIAGNOSTIC
  CONTINUATION_OR_FACTS

[ORDINAL] STATEMENT_HEAD
  DESCRIPTION

  WORK
  . AGGREGATE_OR_ACTIVITY
  -> LANE item ITEM . ACTIVITY
  ↳ CHILD_OR_COMMIT_OR_CONTROL_RESULT
```

The canonical lane glyph is the Unicode right arrow `→`; the ASCII diagram
uses `->` only to remain legible in the code block. Rendered lanes are
`  → N ...`, where `N` is the zero-based lane. Stable and active Step output
uses the literal period-and-space marker `. `. Diagnostics use `! ` and child,
commit, or control closure uses `↳ `. Headers and continuation facts have no
marker.

At root level, content begins in column three after `. ` or `! `; wrapped
continuations begin with two spaces. Statement content adds two spaces to both
prefixes, so marked content begins in column five and continuations use four
spaces. A marker is never stranded on a line by itself.

Facts use centered dots in this order:

```text
duration · descendant runs · tool calls · model calls · usage
```

Step facts begin with StepPath, followed by duration and kind-specific facts.
Statuses use `succeeded`, `failed`, and `canceled` exactly. Counts are counts;
all indexes and lane/item/iteration positions are zero-based.


## Step Presentation Policy

### Model And Tool Steps

An independently visible model `StepBegin` shows `ModelProgress` with
`. thinking…`. `PartBegin` adds the typed Part slot. `PartDelta` updates only
that slot and then its owning `ModelProgress`. Text is a bounded preview;
tool-call deltas use a bounded call summary. `PartEnd` replaces the slot's
preview with canonical final data.

A successful model `StepEnd` clears its live block and retains a completed
candidate in state. The next Step in that Run proves it is internal work and
appends it as `ModelStep` before opening the next progress block. A successful
root `RunEnd` whose output references that candidate appends it once as
`RunResult` instead. A root end referencing another value first appends the
candidate as `ModelStep`, then appends the distinct result. Failed and canceled
model Steps finalize immediately as `ModelStep` and cannot become a result.

A tool `StepBegin` shows `. executing TOOL…`. Its terminal `StepEnd` immediately
finalizes `ToolProgress` to `ToolStep`. A successful result uses `. `; failure
or cancellation uses `! ` when it has a diagnostic, followed by Step facts.

Non-parallel agic model and tool Steps are independent stable Steps. A call is
compacted into a `StatementProgress` instead when its Run is concurrent child
work or is the active child of a parent Run Step. Compacted Parts update only
that statement's active slot or lane; they never create per-child top-level
blocks.

### Flow Steps

Every Flow-owned `StepBegin` shows one `StatementProgress`, selected by the
#270 StepKind/statement matrix. The header comes only from `given.source.head`;
the work description uses the resolved runnable and explicit cardinality facts
in `given`. A missing child `RunBegin` never causes a synthetic child row.

| Step kind | Statements | Active presentation |
| --- | --- | --- |
| `run` | run, scatter, gather | At most one child Run slot and its compact activity. |
| `agent` | seek | One local agent-operation activity; no child Run is inferred. |
| `human` | ask | One local waiting/response activity. |
| `par` | storm, map, predicate keep/drop, rank | Aggregate plus one row per active declared lane. |
| `loop` | repeat, settle | Ordered iteration context; repeat body/until or one settle child slot. |
| `value` | let, positional keep/drop | Local value activity; no child Run or Part stream. |

`StepEnd` finalizes the complete owner block to `FlowStep`. Successful result
and commit rows come only from `noted.result`, `noted.commit`,
`noted.source_items`, `noted.iters`, and `noted.stopped`. Failure and
cancellation omit successful commit wording. The exact binding destination is
the keys of `commit`; the machine never reconstructs locals.

Parallel lanes are keyed by `(owner StepPath, lane)`. A child `RunBegin`
replaces that lane row only after its prior Run ended. `RunEnd` removes the row
only if the ending Run still owns it. Completed lane rows disappear; the final
`FlowStep` contains one aggregate and selected failure context, not per-item
success history. Live height is therefore bounded by `lanes` plus the fixed
statement rows.

Settle accepts one active direct child Run at a time. Each `iter/item` boundary
is retained as compact history before the slot is reused. Repeat retains an
ordered section for each observed `iter`; nested body statement ordinals
restart at zero, and a direct child Run of the repeat Step is its until
evaluator. Until does not consume an ordinal. The final control row uses
`StepEnd.noted.iters` and `stopped`, rather than guessing from the last model
text. These repeat and settle rows are presentation context, not new execution
entities or events.


## Event Acceptance And Transitions

The canonical codec and closed fact schemas from #270 validate field types.
The state machine adds the following sequence rules.

| Event | Required active state | Accepted transition and effects |
| --- | --- | --- |
| `RunBegin` | First event is the configured root; a child names an active owning Step. | Add one running attempt and update internal ownership/slot state. Every RunBegin emits no effect; the next Step or Run transition makes changed activity visible. |
| `StepBegin` | Its Run attempt is active; its activation and input references match the stream boundary; its sibling index is fresh and ordered; any StepPath parent is active. | Flush a prior independent model candidate in that Run, add the Step, count the call when applicable, and show or update the selected progress owner. |
| `PartBegin` | The Step is active and is model/tool; the index is the next fresh Part index. | Add one active typed Part and update only the Step's presentation owner. |
| `PartDelta` | The named Part is active; delta kind matches Part type. | Extend only that Part's bounded preview and update only its presentation owner. |
| `PartEnd` | The named Part is active; final data type matches Part type. | Mark that Part complete, retaining canonical data for Step output validation and presentation. |
| `StepEnd` | The Step is active; activation and kind match; Parts, nested Steps, and child Runs are closed. | Verify terminal output, finish metrics, and finalize/clear the Step presentation according to the policy above. |
| `RunEnd` | The attempt is active; activation and kind match; all Steps and descendants are closed. | Verify result/failure causality, finish metrics, update an owning statement for a child, or emit root result/diagnostic and exactly one summary. |

### Global Rejection Rules

Reject an event when any of these conditions holds:

- it precedes the configured root `RunBegin`, follows root `RunEnd`, or belongs
  to another root activation;
- its identity was already begun or ended;
- its `activation`, Run kind, Step kind, parent, root, placement totals, or
  ownership conflicts with the corresponding begin event;
- a Step begins outside its active Run, a nested Step begins outside an active
  repeat Step, or sibling physical indexes repeat or move backward;
- a child Run begins outside `run`, `par`, or `loop` work;
- a non-parallel owner receives overlapping child Runs;
- a parallel item or active lane is reused, an index is outside its total, or
  `items/lanes` changes within the statement;
- repeat or settle iteration indexes move backward, skip an iteration, or
  conflict with their declared totals;
- a Part is sparse, duplicated, ended before it begins, receives a delta after
  ending, or has a mismatched delta/final type;
- a Step ends with an active Part, nested Step, or child Run;
- terminal Step output differs in index, length, or value from its ordered
  completed `PartEnd` data when Parts were emitted;
- a model/tool Step has nonempty output without matching completed Parts;
- a Step input reference names another Run, names an observed Step that has not
  completed, or names an unseen Step outside a retry activation;
- a Run ends with active Steps or descendants;
- a Run output Step reference names another Run; or, when it names a Step
  observed in this machine, that Step is not complete or selects a nonexistent
  Part;
- a causal Run failure does not name a failed local Step, a runtime failure is
  empty, or cancellation carries a failure; or
- terminal status and required error/noted variants violate #270.

Part indexes are contiguous from zero. Parallel events may otherwise
interleave freely across Run IDs. There is no global current Step.

### Kind-Specific Causality

- An agic Run accepts only top-level `model` and `tool` Steps. A Flow Run
  accepts only Flow-owned Step kinds; nested Steps occur only below an active
  repeat Step. Every Run's event kind and `given.runnable.kind` agree.
- `run` work accepts zero children before a failed/canceled Step and exactly
  one completed child before a succeeded Step.
- `par` work accepts zero or more children with complete `item/items` and
  `lane/lanes`; a succeeded Step requires every begun child to have succeeded.
- settle accepts direct children with matching `iter/iters` and `item/items`
  and never overlaps them.
- repeat body Steps carry matching `iter/iters`; its direct child Run is the
  until evaluator and is allowed only when `given.until` is non-null.
- `agent`, `human`, and `value` work accepts no child Run or Part event.
- model and tool Steps accept no child Run or nested Step. A succeeded tool
  output is Part zero of type `tool_result`; tool Parts do not accept deltas.

A failed or canceled containing Step may follow failed/canceled child work.
Success never hides an unsuccessful child.


## Failure, Cancellation, And Runtime Failure

Structured causality replaces string-based de-duplication.

1. An independent failed Step owns its `ModelStep`, `ToolStep`, or `FlowStep`
   diagnostic.
2. A compacted child failure is retained by the nearest visible owning
   `StatementProgress` and emitted once when that Flow Step ends.
3. A parent Run failure referencing that failed Step emits no second message.
4. A root inline runtime failure, which has no Step, emits one `RunDiagnostic`
   immediately before `RunSummary`.

For multiple parallel failures, the final statement reports counts and one
primary diagnostic chosen by lowest item, then lane, then Run ID. Remaining
failures are counts and identities, not repeated messages. This ordering is
independent of completion timing.

Cancellation is not a failure. It clears incomplete success previews, retains
completed-work facts, uses canceled status, and emits no error unless the
canonical event carries an independent runtime failure—which #270 normally
rejects. An applied stop reason remains structured summary data; #260/#261 own
whether a generic reason is hidden.


## Root Completion And Retry

Root `RunEnd` produces one ordered terminal batch:

1. resolve any completed independent model candidate;
2. on success, append one `RunResult` when the canonical result is present or
   its shape requires an explicit empty/no-result description;
3. on an unowned runtime failure, append one `RunDiagnostic`; and
4. append exactly one `RunSummary` and set lifecycle to `terminal`.

Agic `RunResult` may contain event-carried Parts from its referenced completed
Step. Flow `RunResult` contains its reference and shape but does not expand a
potentially large value. A pass-through `RunInputRef` is described from
`RunEnd.noted`; the input value is not fetched. Script stdout and Chat's
durable-result affordance remain sink policy.

A retry stream begins with the same Run ID and a new activation reference in a
new machine. It does not erase or amend the prior `RunSummary` and does not
replay ejected Steps. Root events must match the retry activation; descendant
events must match the activation established by their own RunBegin.

A retry may validly consume or return a retained Step from an earlier
activation. A same-Run StepOutputRef not observed by the new machine is
therefore retained as an opaque historical reference. The machine uses current
event facts for shape and availability, never queries the store, and validates
full Part/output consistency only when the referenced Step was observed in the
current stream. This rule applies to `StepBegin.input` and `RunEnd.output`.
New-run and rerun activations reject unseen Step references.
Steer references in `StepBegin.input` are recorded as consumption facts; start
and steer bars remain command-owned UI and are not invented from Run events.


## Shutdown And Incomplete Output

`shutdown(mode)` is separate from `apply(event)`:

- `complete` is valid only after root `RunEnd` and emits nothing;
- `detach` may occur while open, clears every live `Progress` block, marks the
  machine detached, and emits no terminal status;
- `broken` may occur while open, clears every live block, appends one
  `StreamInterrupted`, and marks the machine broken.

`StreamInterrupted` says `progress stream ended before run completion` and
identifies the last accepted event. It is neutral presentation state, not a
failed/canceled Run, synthetic Step, or durable fact. No `RunResult` or
`RunSummary` is fabricated. A later event after any shutdown mode is rejected.

An open Part may therefore exist only at detach/broken shutdown. Its bounded
preview is cleared and never promoted to stable Step output. Normal `StepEnd`
and `RunEnd` still require all Parts to be balanced.


## Sink Responsibilities

Both sinks consume only block/effect batches and shared formatting helpers.
They do not inspect `RunEvent`, choose owners, aggregate metrics, select
diagnostics, resolve result references, or derive Flow semantics.

### Script

- `Append` and `FinalizeLive` write stable progress to stderr in effect order.
- On a TTY, Show/Update/Clear use `ProgressConsole`'s bounded cursor-managed
  live area.
- On non-TTY output, live blocks are retained in memory; only their terminal
  `FinalizeLive` form is written. `ClearLive` produces no bytes, so deltas and
  abandoned candidates never leak into logs.
- Existing quiet/verbosity policy may hide optional fields or whole blocks but
  cannot change lifecycle or reinterpret an event.
- Canonical successful value encoding on stdout remains command-owned.

### Chat TUI

- The sink maps semantic blocks to Rich renderables and keeps the complete
  ordered live block set for prompt-toolkit.
- One effect batch removes finalized blocks from the live area, erases the
  application frame, writes terminal blocks to scrollback in order, and
  invalidates layout once.
- A live block is never visible in prompt-toolkit and scrollback at the same
  time. Cursor hiding/restoration is scoped with `finally`.
- Submission, steer, queue, slash, prompt, and status blocks remain outside
  the execution sink. Root completion notifies the app only after the terminal
  batch is committed.


## Width, Resize, And Terminal Modes

Semantic blocks store logical rows and hanging indents. Sinks measure terminal
cell width, including wide Unicode.

- Prose and Markdown wrap with the continuation alignment defined above.
- Lane rows never wrap. They retain, in order, `→ lane`, item, then as much
  activity as fits, ending with `…` when truncated.
- At widths too small for a marker plus one content cell, retain the marker and
  truncate content rather than producing negative widths or an orphan marker.
- A resize re-renders only live blocks; terminal scrollback is never rewritten.
- A height-constrained Chat viewport keeps the newest complete logical rows
  and one omission row. Semantic state and lane bounds are unchanged.
- Styling and cursor movement are forbidden for non-TTY sinks. Stable text and
  event selection remain the same as TTY output after live lines are removed.

#260 may choose the exact minimum width and wrapping library. #261 may choose
Chat-specific styling and footer density. Neither may change the prefix,
continuation, truncation priority, or block lifetime above without revising
this contract.


## Representative Transitions

### Root Agic With Tool Use

```text
StepBegin model       ShowLive(ModelProgress: . thinking…)
PartDelta text        UpdateLive(ModelProgress: . searching…)
StepEnd model         ClearLive(ModelProgress); retain candidate
StepBegin tool        Append(ModelStep); ShowLive(ToolProgress)
StepEnd tool          FinalizeLive(ToolProgress, ToolStep)
StepBegin model       ShowLive(ModelProgress)
StepEnd model         ClearLive(ModelProgress); retain candidate
RunEnd succeeded      Append(RunResult); Append(RunSummary)
```

The last model candidate appears once as the root result. Earlier non-parallel
Steps remain independent stable Steps.

### Parallel Flow Step

```text
[2] map summarize par 3
  Run agic summarize in parallel (12 items, 3 lanes)
  . 4 runs succeeded · 3 active
  → 0 item 7 . thinking…
  → 1 item 5 . executing web_search.search…
  → 2 item 6 . starting…
```

Interleaved Part events update only the row whose Run owns that lane. At
`StepEnd` every arrow row disappears and `FlowStep` retains one aggregate plus
the exact result/commit description.

### Repeat With Until

```text
[0] repeat 5 until <agic:L42>
  === iteration 0 ===
  [0] run revise
    ↳ run_revise0 succeeded
  [?] until
    ↳ run_until0 succeeded
  === iteration 1 ===
  [0] run revise
    ↳ run_revise1 succeeded
  [?] until
    ↳ run_until1 succeeded
  . 2 iterations completed
  ↳ stopped after 2 iterations
```

Sections and ordinals are derived from StepPath parentage and `iter/iters`;
there is no Stage or Iteration event. The final count and decision come from
the repeat `StepEnd.noted` facts.


## Implementation Touchpoints And Order

Implementation must follow #270 and proceed in this order:

1. Add terminal-neutral identities, blocks, effects, and
   `ProgressSequenceError` under `cli/common/execution_progress/types.py` and
   `errors.py`.
2. Refactor `state.py` to hold the maps and aggregates defined here; retain
   pure shared text/value helpers in `formatting.py`.
3. Add `machine.py` with transactional validation and transitions. It may
   import only execution event/value types and package-neutral helpers.
4. Convert `script_progress/tracer.py`, `blocks.py`, and `console.py` into a
   thin policy/renderer/sink over effects.
5. Convert Chat `presenter.py`, `blocks.py`, and `tui.py` to the same effects;
   keep local control bars and prompt state outside the machine.
6. Remove duplicate event interpretation only after both adapters pass the
   same sequence fixtures.
7. Update public execution presentation documentation to the implemented
   canonical block names and target #270 vocabulary.

Likely tests are a new
`tests/unit/cli/test_execution_progress_machine.py`, the existing script and
Chat presenter suites, CLI integration tests, and pseudo-terminal system
tests. No `toolang.execution` production change belongs to this feature; a
missing event fact must return to #270 or receive separate scope approval.


## Acceptance Tests

1. Feed one shared valid fixture for every RunKind, StepKind, and RunEvent type
   through the machine; assert exact state and effect batches.
2. Parameterize every rejection rule, including duplicates, mismatched
   activation/kind, missing owners, backward siblings/iterations, lane
   collisions, Part mismatches, output mismatches, active descendants, and
   invalid causal failure. Assert unchanged state and zero effects.
3. Interleave model/tool Parts across all lanes of a parallel statement and
   assert each delta changes only its Step and lane owner; final live height is
   bounded by lane count.
4. Cover model-to-tool-to-model, output confirmation, non-output model
   candidates, no-Part output, multimodal Parts, tool result Part zero, and
   pass-through Run input without store reads.
5. Cover every Flow statement, empty/single/many work, direct child failure,
   partial parallel failure, lane reuse, settle ordering, nested repeat body,
   until success/failure, final commit facts, and no inferred child activity.
6. Cover succeeded, failed, and canceled Steps/Runs, causal diagnostic
   ownership, multiple parallel failures, inline runtime failure, and exactly
   one root summary.
7. Run two machines for an initial attempt and retry with the same Run ID;
   assert activation isolation, prior scrollback preservation, fresh Step
   identities, and rejection of cross-attempt events.
8. Assert TTY and non-TTY stable golden output agree after cursor operations
   are removed; no Part delta appears in non-TTY output.
9. Cover 1-cell through normal widths, wide Unicode, lane truncation, resize,
   height clipping, and marker/continuation alignment.
10. Cover complete, detach, and broken shutdown with active model, tool,
    statement, lane, and Part state; assert no fabricated execution status.
11. Assert script and Chat adapters never import a store and never branch on a
    `RunEvent` subtype after handing the event to the machine.
12. Run the default verification:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```


## Risks

- Strict validation will expose existing executor ordering defects. Fix the
  producer or #270 contract rather than weakening a causal rule silently.
- Delaying successful model finalization until the next boundary requires the
  candidate map to preserve event order exactly.
- Large repeat/settle history may grow. Keep Part previews bounded and render
  compact iteration rows; do not discard the context required to reconstruct
  hierarchy.
- Parallel completion timing can make diagnostics nondeterministic. Select the
  primary failure by stable placement/identity order, not arrival order.
- A sink failure can leave terminal pixels partially written even though core
  state is valid. Stop event consumption and use broken shutdown; never replay
  a partially applied batch.


## Open Questions

None. The dependency, validation rules, ownership, block grammar, effects,
surface boundary, terminal behavior, implementation order, and acceptance
criteria are fixed.
