# Execution Vocabulary And Durable Invocation Records

## Goal

Define one canonical execution vocabulary and one lossless durable contract for
runs, activations, steps, parts, controls, failures, and Flow commits. The
contract must let callers attribute every attempt to its invocation and policy
snapshot, rebuild effective retry and rerun history, and drive a later progress
state machine without adding Stage or Iteration events.

Implementation starts only after a human explicitly approves this definition.


## Success Criteria

- Run, Step, Part, and RunControl records and events have exact types, fields,
  cardinalities, serialized shapes, and lifecycle invariants.
- Successful Run and Step terminal state is `succeeded`; Control `finished`
  continues to mean applied.
- Every activation records the invocation or retry boundary, effective policy,
  state hash, attempt outcome, and the steps it produced.
- Entry invocation input persists `primary` and `named` together; RunRecord
  keeps only a reference to that input.
- Retry and rerun history can be reconstructed without current configuration,
  source reparsing, timestamp inference, or provider access.
- Run and Step failures are structured, and a Run failure identifies either
  its causal Step or an inline runtime error.
- Flow Step facts are sufficient to derive child resolution, transformation,
  and committed locals without Stage or Iteration events.
- The schema is a deliberate clean break: legacy execution databases and event
  payloads are neither migrated nor dual-read.


## Verified Current Behavior

The implementation currently has no first-class RunKind. Runnable kind, name,
root, call type, state fingerprint, model binding, arguments, ceiling
restrictions, and placement share `RunRecord.context` and `RunBegin.context`.
`RunEnd` has neither kind nor terminal facts.

An index-zero `start` or `rerun` RunControl stores only a user Message. Named
arguments are separately embedded in Run context. Root entry and retry controls
store effective limits in generic control context, while child entry controls
do not repeat the activation policy. Model, ceilings, and state fingerprint
are not stored on retry controls.

Retry reuses a root Run ID, ejects an invalid Step suffix, appends new physical
Step indexes, and overwrites Run status, output, error, and attempt timestamps.
Ejected Steps remain auditable, but they do not directly identify the
activation that produced them and an overwritten run-level failure is lost.
Rerun creates a new root, copies primary input and named arguments from the
source's split storage, and ejects the source tree.

Run and Step success is `finished`; Control `finished` means applied. Errors
are untyped strings. A runtime failure outside an existing Step creates a
synthetic failed `system` Step so the Run can point at visible failure work.

Step kinds are `run | agent | human | model | tool | par | loop | system`.
Deterministic `let` and positional keep/drop operations use `system`. Flow
Steps persist statement facts in `given` and a typed result in `noted`, but a
successful top-level repeat does not persist the complete set of locals
committed by its nested body.

Placement currently mixes `loop`, `item/items`, `lane/lanes`, and
`role=until`. Repeat body Steps use `loop`; settle child Runs use both `item`
and `loop`; an until evaluator is labeled by `role` even though its direct
parent already identifies it.

Run events are transient and project Run and Step records in emission order.
PartBegin, PartDelta, and PartEnd are streamed but Part is not a separate
durable row; completed parts are the ordered Step output. SQLite currently
contains compatibility migrations and dual names for some older Step fields.


## Scope

This feature defines:

- canonical execution vocabulary, references, structured errors, and facts;
- exact RunRecord, StepRecord, and RunControlRecord contracts;
- exact Run, Step, and Part event contracts;
- activation, retry, rerun, steer, stop, ejection, and failure invariants;
- clean-break SQLite and codec behavior;
- executor, store, API, inspection, and presentation adaptation touchpoints;
- acceptance tests for the new contract.

It does not define or implement:

- the progress state machine or script/TUI layout;
- Stage, Iteration, waiting, steering, or stopping events;
- provider, scheduler, parallelism, or authored `.too` syntax changes;
- legacy database migration, event translation, or compatibility aliases;
- exact historical event replay after reconnect.


## Canonical Vocabulary

```text
RunKind          agic | flow
RunStatus        pending | running | succeeded | failed | canceled
RunTerminalStatus          succeeded | failed | canceled
StepStatus                 running | succeeded | failed | canceled
ControlStatus    pending | finished | canceled | failed
ControlTiming    immediate | next_step | next_call
RunControlKind   run | rerun | retry | steer | stop
StepKind         run | agent | human | model | tool | par | loop | value
Shape            none | item | list
```

`succeeded` is durable vocabulary, not a display translation. `finished` is
reserved for a Control whose requested mutation was applied. An applied stop
therefore has Control status `finished` while its attempt has Run status
`canceled`.

Flow statements map to StepKind as follows:

| StepKind | Owners |
| --- | --- |
| run | run, scatter, gather |
| agent | seek |
| human | ask |
| par | storm, map, predicate keep/drop, rank |
| loop | repeat, settle |
| value | let, positional keep/drop |
| model | one provider model call |
| tool | one tool call |

`value` means a lightweight Flow operation that consumes an already available
value without invoking a child run, parallelizing work, or looping. It is not
a general runtime or diagnostic Step. `system` is removed.


## Scalar And Reference Types

`JsonValue` is null, Boolean, number, string, an ordered array of JsonValue, or
a string-keyed object of JsonValue. A MessagePart inside a language value uses
its existing discriminated JSON object.

```text
RunControlRef  = {"run": RUN_ID, "index": NON_NEGATIVE_INT}
ThreadControlRef = {"thread": THREAD_ID, "index": NON_NEGATIVE_INT}
RunInputRef    = {"control": NON_NEGATIVE_INT, "part"?: NON_NEGATIVE_INT}
StepOutputRef  = {"step": STEP_PATH, "part"?: NON_NEGATIVE_INT}
ValueRef       = RunInputRef | StepOutputRef
```

RunInputRef is local to its owning Run. It resolves to `input.primary` for a
`run` or `rerun` control and to `message.parts` for a `steer` control. Omitting
`part` selects the complete ordered part list; providing it selects exactly one
part. It never selects `input.named`. Named values participate in the
invocation and Flow locals but are not Run output parts.

Invocation input is always serialized with both keys:

```json
{
  "primary": [{"type": "text", "text": "hello"}],
  "named": {"tone": "brief"}
}
```

`primary` is an ordered PerceptPart array and may be empty. `named` is a
string-keyed JsonValue object and may be empty. Parameter validation remains a
runtime concern; storage preserves the accepted values losslessly.

Structured errors have one shape:

```json
{"type": "ValueError", "message": "output is not valid Number"}
```

Both fields are non-empty strings. `type` is the stable Toolang or exception
type selected at the boundary; raw traceback, provider request, credentials,
and arbitrary exception attributes are never persisted.

Run failure is a discriminated union:

```json
{"kind": "step", "step": "run_abc/2"}
```

or:

```json
{
  "kind": "runtime",
  "error": {"type": "ValueError", "message": "output is not valid Number"}
}
```

A Step failure does not duplicate its error on the Run. A runtime failure is
used only when no failed Step in that Run caused the terminal transition.


## Activation Snapshot And Attempt

`run`, `rerun`, and `retry` are activation controls. Every activation stores
these direct, non-generic fields:

| Field | Type | Meaning |
| --- | --- | --- |
| model | string or null | effective default model selector after call-site defaults |
| ceilings | CeilingData[] | concrete ordered ceiling chain actually applied |
| limits | RunLimitsData | complete effective limit object, including null fields |
| state_hash | string | lowercase hexadecimal AgentState content hash |
| attempt | RunAttempt | lifecycle and outcome produced by this activation |

Each CeilingData contains required `models`, `tools`, and `caps` ordered string
arrays; an empty array means that resource kind is unavailable. The chain
begins with the resolved agent ceiling after setup and caller restrictions,
includes the inherited containing-flow ceiling when one applies, and ends with
the resolved ceiling for this Run's executable. Repeated equal layers are
collapsed. Values are canonical selectors, tool names, and prepared cap refs,
not provider objects, credentials, or authored directives. Child `run`
controls record their own resolved chain so each Run is independently
attributable. Rerun and retry record the newly resolved snapshot rather than
inheriting an unrecorded current default.

RunLimitsData always contains `agic_model_calls`, `agic_tool_calls`, `tokens`,
`cost`, and `time`. Integer limits are non-negative integers or null. USD cost
is a finite non-negative decimal string or null.

RunAttempt is:

```json
{
  "status": "running",
  "started_at": "2026-08-15T01:02:03Z",
  "finished_at": null,
  "output": null,
  "noted": {},
  "failure": null
}
```

Its status uses RunStatus. At acceptance it is `pending` with null timestamps;
RunBegin changes it to `running`; RunEnd supplies a terminal status,
`finished_at`, output, noted facts, and failure. The attempt outcome is
immutable after it becomes terminal. Control status remains independent and
continues to describe application of the control itself.


## Durable Records

### RunRecord

```text
id           RunId
kind         RunKind
parent       StepPath | null
thread       string
activation   RunControlRef
input        RunInputRef
output       ValueRef | null
given        RunGiven
noted        RunNoted
status       RunStatus
failure      RunFailure | null
ejected      ThreadControlRef | RunControlRef | null
created_at   timestamp
started_at   timestamp | null
finished_at  timestamp | null
```

The RunRecord is the latest effective projection for fast history reads.
`activation` identifies the current attempt; prior attempts remain on their
activation controls. `input` always references the entry `run` or `rerun`
control and has no `part`, including after retry. No invocation values, policy
snapshot, state hash, or generic context are duplicated on RunRecord.

RunGiven is known before RunBegin and has this exact shape:

```json
{
  "root": "run_root",
  "runnable": "review",
  "placement": {"item": 2, "items": 8, "lane": 1, "lanes": 4}
}
```

`root` and `runnable` are required. `placement` is omitted when empty. Run kind
is a direct field, top-level versus child call is derived from `parent`, and
state/policy/input facts belong to the activation control.

RunNoted is `{}` while nonterminal. On success it contains `shape`, nullable
`type`, and `items` when shape is list. On cancellation it may contain one
non-empty `reason` and the applied stop as `stop: RunControlRef`; both are
omitted for cancellation without a stop control. Failed Runs keep failure data
in `failure`, not `noted`. RunNoted never copies the output value.

Invariants:

- a root has `parent=null` and `given.root=id`;
- a child has a parent Step in the same thread and copies that tree's root;
- pending has null start/finish, running has start only, terminal has both;
- succeeded has non-null output unless the runnable declares no result;
- failed has exactly one RunFailure; other statuses have none;
- canceled has no RunFailure and may retain a partial output reference;
- an ejected record remains immutable and is excluded from effective reads.


### RunControlRecord

```text
run          RunId
index        non-negative integer
kind         RunControlKind
timing       ControlTiming
input        InvocationInput | null
message      Message | null
reason       string | null
source       RunId | null
anchor       StepPath | null
request_id   string | null
model        string | null
ceilings     CeilingData[] | null
limits       RunLimitsData | null
state_hash   string | null
attempt      RunAttempt | null
status       ControlStatus
error        ExecutionError | null
created_at   timestamp
finished_at  timestamp | null
```

Storage uses the flat record above. Cardinality by kind is normative:

| Kind | timing | payload and references | activation fields |
| --- | --- | --- | --- |
| run | immediate | input required; source/anchor/message/reason null | all required; index 0 |
| rerun | immediate | input and source required; others null | all required; index 0 |
| retry | immediate | anchor required; input/source/message/reason null | all required; index > 0 |
| steer | any | user message required; all other payload refs null | all null |
| stop | any | optional reason; all other payload refs null | all null |

`request_id` is optional but globally unique across Run controls when present.
`error` is required exactly when Control status is failed. A canceled Control
has no error. A storage-only monotonic revision and claim flag remain outside
the public record.


### StepRecord

```text
path         StepPath
activation   RunControlRef
kind         StepKind
input        (RunInputRef | StepOutputRef | Message)[]
output       MessagePart[]
given        StepGiven
noted        StepNoted
status       StepStatus
error        ExecutionError | null
ejected      RunControlRef | null
created_at   timestamp
started_at   timestamp
finished_at  timestamp | null
```

`activation` must name the activation currently running the Step's owning Run.
This direct edge, rather than timestamp ordering, attributes effective and
ejected work to the model, ceilings, limits, and state that produced it.

A running Step has no finish time or error. A succeeded Step has no error. A
failed Step requires an error. A canceled Step may omit an error. Completed
parts appear in output order even when a tool Step reports a structured
failure. Retry never overwrites a Step; it sets `ejected` and uses fresh
physical indexes.


### Part Data

There is no standalone durable PartRecord. `(StepPath, part index)` is the
transient identity. PartBegin fixes MessagePartType, PartDelta carries the
existing typed Delta, and PartEnd carries the complete MessagePart. A terminal
Step output equals its ordered completed PartEnd data. Open streamed parts may
be discarded when a Step fails or is canceled.


## Flow Step Facts

Every Flow StepBegin uses these common StepGiven keys:

```json
{
  "statement": "rank",
  "binding": "findings",
  "current": {"shape": "list", "type": "Finding", "items": 18},
  "source": {"line": 42, "head": "let findings = rank relevance top 8 par 4"},
  "doc": "Keep the best findings.",
  "placement": {"iter": 1, "iters": 3},
  "scorer": "relevance",
  "limit": "top",
  "count": 8,
  "par": 4
}
```

Required keys are `statement`, `binding`, `current`, and `source`. Binding is a
string or null. `current` contains shape, nullable type, and `items` exactly
when the shape is list; it deliberately omits the value. `doc`, `placement`,
and statement operands are omitted when absent. The canonical operand keys are
`runnable`, `agent`, `count`, `par`, `position`, `predicate`, `scorer`,
`limit`, and `until`, with the authored scalar types already used by the AST.

Every succeeded Flow StepEnd uses:

```json
{
  "result": {
    "shape": "list",
    "type": "Finding",
    "value": [{"title": "..."}],
    "items": 8,
    "ref": {"step": "run_root/2"}
  },
  "commit": {
    "findings": {
      "shape": "list",
      "type": "Finding",
      "value": [{"title": "..."}],
      "items": 8,
      "ref": {"step": "run_root/2"}
    }
  }
}
```

ValueSnapshot requires `shape`, nullable `type`, JSON-compatible `value`, and
nullable `ref`; `items` is present exactly for list shape. `value` is the typed
runtime value used for retry reconstruction, while Step output remains the
MessagePart rendering. The duplication is intentional because MessagePart
output is not a lossless encoding of every language value.

`commit` is the exact set of locals atomically changed by successful StepEnd:

- `_` or a named binding maps to the resulting ValueSnapshot;
- a discarded result has an empty commit;
- repeat records every net local change made by its completed nested body;
- failed or canceled Steps have no result or commit and change no locals.

Repeat additionally notes `iters` as the number of fully completed iterations
and `stopped` as a Boolean indicating an early successful until decision.
List-consuming statements additionally note `source_items`; this repeats a
cardinality, not the source value, and makes empty gather, settle, map,
keep/drop, and rank transformations durable. The statement kind plus
`current`, child Run parent edges, result, source_items, and commit fully derive
the three semantic phases:

1. resolve: direct child Runs whose parent is this Step, plus their placement;
2. transform: statement kind and source/result cardinalities after child work;
3. commit: the atomic local assignments in `commit`.

No Stage or Iteration event or record is added.

Model StepGiven has exactly `model` and `call`. `model` contains `ref`,
`provider`, `name`, `model`, `adapter`, nullable `base_url`, `scope`, ordered
`tags`, JSON `options`, `tools`, and `streaming`; it excludes credentials and
headers. `call` contains normalized `instructions`, ordered `messages`, ordered
`tools`, and nullable adapter `state`. Content-addressed storage may replace
large call components internally, but caller-facing reconstruction has this
single shape. A succeeded model StepNoted contains nullable `tokens` with
integer `input` and `output`, nullable `price` with decimal-string `input` and
`output`, nullable decimal-string `cost`, nullable `reasoning_content`, and
nullable JSON `state`.

Tool StepGiven has exactly `tool`, `plugin`, `tool_call_id`, and `call_id`, all
strings. Its StepNoted is empty. Its result is the emitted ToolResultPart,
including the normalized tool output or tool-reported error. Model and tool
Steps use structured Step errors and `succeeded`; no generic fact keys are
added.


## Placement

Placement has only these normalized pairs:

```text
iter / iters
item / items
lane / lanes
```

Indexes are zero-based non-negative integers. Totals are positive integers;
`iters` may be null for an unbounded repeat. A position key and its total key
must appear together. Placements compose, so a parallel child inside repeat
may carry all six keys.

- Repeat body Steps carry `iter/iters`.
- An until evaluator is a child Run directly parented by the repeat Step and
  carries the same `iter/iters`. That direct parent edge identifies until;
  `role=until` is removed.
- A child Run from a nested repeat-body statement is parented by that nested
  Step, so it cannot be confused with the direct until evaluator.
- Parallel child Runs carry `item/items` and `lane/lanes`.
- Settle child Runs carry `iter/iters` for reducer-call position and
  `item/items` for source-list position; the values are currently equal but
  remain separate concepts.

RunGiven records child placement. Nested Flow StepGiven records inherited
placement. No `loop`, `role`, or presentation-only iteration identity remains.


## Event Schemas

All serialized events keep the existing `type` discriminator. Timestamps are
UTC strings. Empty arrays and fact objects are serialized; optional references
and errors are serialized as null.

```text
RunBegin
  type = run_begin
  run, activation, kind, input, parent, given, started_at

RunEnd
  type = run_end
  run, activation, kind, status, output, noted, failure, finished_at

StepBegin
  type = step_begin
  step, activation, kind, input, given, started_at

StepEnd
  type = step_end
  step, activation, kind, status, output, noted, error, finished_at

PartBegin
  type = part_begin
  step, part, part_type

PartDelta
  type = part_delta
  step, part, delta

PartEnd
  type = part_end
  step, part, data
```

RunBegin.input is the immutable entry RunInputRef, while `activation`
identifies the attempt-causing run/rerun/retry control. A pass-through output
uses RunEnd.output with a RunInputRef. Stop causality is represented by the
activation attempt's canceled outcome and the applied stop Control referenced
in `noted.stop` as RunControlRef. RunEnd status is terminal only. RunEnd
failure follows the exact RunFailure union.

StepBegin input keeps data-dependency edges. A consumed steer is a RunInputRef
to that control's `message`; applying it changes the steer Control to finished
in the same projection transaction. StepEnd status is terminal only.

Event integrity requires:

- exactly one RunBegin and RunEnd per activation attempt;
- Run and Step begin/end kind and activation agreement;
- Steps strictly inside their owning Run attempt;
- child Run events strictly inside the parent run Step;
- balanced Part lifecycle inside one active Step;
- no Step or Run events after that activation's terminal event;
- succeeded Step output equal to its ordered completed PartEnd data when parts
  exist;
- a Run output StepOutputRef point to a terminal Step;
- projection of an event and its Control transitions in one SQLite transaction.


## Control Behavior

### run

Atomically insert a pending RunRecord and index-zero `run` control. The control
owns the complete invocation and activation snapshot. Root and child Runs use
the same kind. RunBegin marks the control applied, sets its attempt running,
and starts the Run projection.

### rerun

Require the latest visible terminal root in the same thread. Atomically create
a new root and index-zero `rerun` control, copy the source invocation into the
new control, and eject the complete source tree by the new control reference.
Use the newly resolved activation snapshot. Source records and outcomes remain
immutable and available to audit reads.

### retry

Require a visible terminal root. Resolve an explicit or default anchor, append
a `retry` control, mark the invalid Step/child-Run suffix ejected by it, fail
stale pending steer/stop controls, and reopen the Run with that activation.
The control is finished when this atomic mutation applies; its RunAttempt
starts at the subsequent RunBegin. New Steps use fresh indexes and reference
the retry activation. Successful prefix commits are restored from Step
`commit`; current source heads must still match the activation's state.

### steer

Require an active Run and a user Message. Store it only in `message`. The
runtime claims it at the selected checkpoint, references it through
StepBegin.input or a pass-through RunEnd.input, applies it in durable index
order, and then marks the Control finished. It never overloads invocation
input.

### stop

Require an active Run. Store an optional plain-text reason and timing. The
runtime claims it at the selected checkpoint, unwinds active Steps and child
Runs, records the stop reference in canceled RunNoted, and marks the Control
finished. Pending controls that can no longer apply become failed with a
structured error.

Canceling a pending steer or stop changes only that Control to canceled. An
activation control and a claimed control cannot be canceled. Claim/revision
remain storage concurrency details rather than public statuses.


## Failure And Causality

A thrown error inside a Step terminates that Step with ExecutionError. Its
owning Run terminates with a `kind=step` RunFailure naming that Step. Parent
Runs identify their own failed containing Step, so every causal reference is
local to the failed Run and callers can walk the nested chain.

An error after the last Step boundary, including input/output coercion or
Run-level limit handling, terminates the Run with an inline `kind=runtime`
failure. No synthetic Step, output part, or `value` Step is emitted. A
cancellation is not a failure and stores its reason and stop reference in
RunNoted.

The activation RunAttempt retains every terminal outcome. Retrying updates the
RunRecord to the new attempt but never erases a prior attempt failure, output,
facts, or timestamps.


## Persistence And Clean Break

SQLite continues to store Runs, RunControls, Steps, Threads, ThreadControls,
and content-addressed model-call components. The new Runs table adds direct
`kind`, `activation`, `given`, `noted`, and structured `failure` columns and
removes generic `context` and string `error`. RunControls add the direct fields
defined above and remove generic context and overloaded input. Steps add
`activation`, replace string error with structured error, and use only the new
vocabulary.

The implementation bumps the execution schema version and creates only the
new tables for an empty database. Opening any older non-empty execution schema
fails before mutation with an actionable unsupported-schema error. Writable
and read-only modes behave the same. The store does not rename columns, copy
rows, translate statuses, accept `start`, accept `system`, decode old events,
or silently delete history. Operators may explicitly archive or remove an old
`runs.db` outside this feature.

Canonical JSON serialization uses UTF-8, sorted object keys where storage
hashing requires determinism, decimal strings for money, arrays for ordered
tuples, and the reference shapes in this plan. Caller-facing schemas expose
the same vocabulary rather than adding a second compatibility translation.


## API And Inspection Contract

Run and Thread history derive root, runnable, input summaries, outputs,
failures, and retry attempts from the new direct records. Run detail exposes
RunGiven, RunNoted, structured failure, current activation, and ordered
activation controls. Step detail exposes activation and structured error.
RunControl detail exposes invocation input, steer message, stop reason,
activation snapshot, and RunAttempt without a generic metadata object.

HTTP run creation still accepts transport-level `input` and `args`, but the
router converts them once into InvocationInput. SSE emits the exact new event
codec. Retry and rerun responses return their activation control. Existing
endpoint purposes, control timing flags, request IDs, and StepPath syntax stay
unchanged; their response schemas make the intentional vocabulary break.

Inspection and CLI progress consumers adapt to `succeeded`, direct Run kind,
Run/Step facts, structured failures, normalized placement, and activation
references. This feature does not change their layout or add progress phases.


## Design Touchpoints

- `src/toolang/execution/types.py`: replace status, kind, placement, and error
  vocabulary; add RunKind and terminal aliases.
- `src/toolang/execution/records.py`: define InvocationInput, activation
  snapshot/attempt, structured failure, direct Run/Step/Control records, and
  canonical codecs.
- `src/toolang/execution/events.py`: define the exact Run, Step, and Part event
  unions and serialization.
- `src/toolang/execution/store.py`: create only the clean-break schema; project
  activation attempts, commits, ejections, failures, and atomic control state.
- `src/toolang/execution/schemas.py` and `history.py`: expose direct durable
  truth and reconstruct input, attempts, values, and failure chains.
- `src/toolang/execution/executor/executor.py`: bind activation snapshots,
  emit activation-aware Run events, retain attempt outcomes, and remove
  synthetic failure Steps.
- `src/toolang/execution/executor/common.py`, `runs/flow.py`, `stmts/`, and
  `steps/`: emit value Steps, normalized placement, current/result/commit
  facts, and structured Step errors.
- `src/toolang/execution/executor/runs/agic.py`, `steps/model.py`, and
  `steps/tool.py`: use activation-aware events, `succeeded`, and structured
  errors while preserving normalized call/accounting facts.
- `src/toolang/api/schemas.py`, `api/routers/runs.py`, and `api/common.py`:
  convert transport input once and expose the new records and SSE events.
- `src/toolang/cli/common/execution_progress/`, script/chat tracers, and
  `src/toolang/cli/toolang/commands/thread.py`: consume the new vocabulary and
  facts without presentation redesign.
- execution unit, integration, API, CLI, architecture, and fixture tests:
  replace legacy factories and assert the acceptance contract below.
- `docs/run-step-records.md`, `docs/execution.md`, `docs/executor.md`, and API
  reference material: update after implementation so public documentation
  matches the approved contract.


## Acceptance Tests

1. Codec tests round-trip every new record, reference, structured error,
   failure variant, and event with exact JSON; old `finished`, `start`,
   `system`, `context`, and overloaded-control shapes are rejected.
2. Store schema tests create the new schema from empty storage and prove an old
   non-empty schema fails without modifying, deleting, or migrating it.
3. Atomicity tests prove Run plus entry control acceptance, event plus Control
   transition, retry ejection plus reopen, and terminal attempt projection each
   commit or roll back as one unit.
4. Activation tests prove every root, child, rerun, and retry stores model,
   ordered ceilings, complete limits, state hash, and RunAttempt; every Step
   names the activation that produced it.
5. Invocation tests persist empty and populated primary/named values together,
   keep only RunRecord.input, resolve primary parts through RunInputRef, and
   reconstruct rerun input without reading RunGiven or current source.
6. Retry tests preserve every activation outcome, structured failure,
   timestamp, and ejected Step; restore exact successful Flow commits; append
   fresh Step indexes; and attribute restored and new work correctly.
7. Flow tests cover every statement-to-StepKind mapping, current/result/commit
   facts, discarded and named bindings, structured values, empty batches,
   cardinality-changing transforms, and repeat's complete net local commit.
8. Placement tests cover bounded and unbounded repeat, nested parallel work,
   settle, and until. They assert only iter/iters, item/items, and lane/lanes
   exist and identify until from its direct parent without a role field.
9. Failure tests cover model, tool, Flow transform, nested child, Run-level
   coercion, limits, and cancellation. They assert structured Step errors,
   local causal Step references or inline runtime failure, and no synthetic
   diagnostic Step.
10. Event-integrity tests cover succeeded, failed, canceled, retry, nested,
    parallel, streaming, and pass-through runs with balanced activation, Run,
    Step, and Part boundaries and exact durable projection.
11. History, API, SSE, inspect, script, and chat tests consume `succeeded`,
    direct Run kind/facts, activation history, normalized placement, and
    structured failures without adding Stage or Iteration presentation.
12. The default verification remains offline and passes:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```


## Risks

- The clean break makes existing execution history unreadable until an
  operator explicitly archives or removes its old database; the fail-before-
  mutation rule prevents accidental loss.
- Activation outcomes duplicate the latest attempt on RunRecord. Atomic
  projection and equality assertions are required to prevent divergence.
- ValueSnapshot may contain large structured Flow values. The implementation
  must preserve losslessness while inspection remains bounded and must never
  store secrets introduced only through provider configuration.
- Complete repeat commits require comparing pre/post locals carefully; tests
  must cover nested named and primary assignments so retry cannot silently
  restore an incomplete environment.
- Direct structured error types become protocol vocabulary. Boundaries must
  choose stable types instead of leaking provider-specific classes casually.


## Explicit Non-Goals

- No implementation is included in this issue.
- No schema migration, compatibility decoder, or deprecation window is added.
- No new authored statement, provider feature, scheduler behavior, parallel
  algorithm, or control timing is added.
- No Stage or Iteration record/event is introduced.
- No script, TUI, chat, or inspect layout is redesigned.


## Open Questions

None. The vocabulary, record ownership, serialization, lifecycle, clean-break
behavior, implementation boundaries, and acceptance criteria are fixed by this
definition.
