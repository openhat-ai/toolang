# Execution Vocabulary And Durable Invocation Records

## Goal

Define one canonical execution contract for runs, activations, steps, parts,
controls, failures, and Flow commits. It must support durable retry/rerun
reconstruction and a later progress state machine without Stage or Iteration
events.

Implementation starts only after a human explicitly approves this definition.


## Success Criteria

- Record and event types, fields, serialization, and invariants are exact.
- Run and Step success is `succeeded`; Control `finished` means applied.
- Every attempt is attributable to its invocation, policy, state, and Steps.
- Entry input persists `{primary, named}`; RunRecord stores only its reference.
- Run failures are structured and identify a causal Step or runtime error.
- Flow facts derive child resolution, transformation, and committed locals.
- Legacy execution schemas and events are neither migrated nor dual-read.


## Verified Current Behavior

- Run kind, runnable, root, model, arguments, ceilings, state fingerprint, and
  placement share generic Run `context`; Run events have no terminal facts.
- Entry controls store only a Message. Named input is in Run context; only root
  entry/retry controls store limits; retry omits model, ceilings, and state.
- Retry reuses the root ID, ejects a Step suffix, and overwrites Run outcome
  and timestamps. Steps do not directly identify their producing activation.
- Run and Step success is `finished`; errors are strings.
- StepKind includes `system`. Local Flow work and synthetic runtime failures
  both use it.
- Placement mixes `loop`, `item/items`, `lane/lanes`, and `role=until`.
- Flow Steps store one typed result, but a completed repeat does not persist
  every local changed by its nested body.
- Parts are streamed events only; completed Part data becomes Step output.
- SQLite currently migrates or dual-reads several legacy execution shapes.


## Scope

This feature defines the canonical vocabulary, records, events, control
semantics, Flow facts, clean-break persistence behavior, implementation
touchpoints, and acceptance tests.

It does not implement the contract, the progress state machine, presentation
changes, provider or scheduler behavior, parallelism changes, authored `.too`
syntax, legacy migration, or historical event replay.


## Vocabulary

```text
RunKind          agic | flow
RunStatus        pending | running | succeeded | failed | canceled
RunTerminal      succeeded | failed | canceled
StepStatus                 running | succeeded | failed | canceled
ControlStatus    pending | finished | canceled | failed
ControlTiming    immediate | next_step | next_call
RunControlKind   run | rerun | retry | steer | stop
StepKind         run | agent | human | model | tool | par | loop | value
Shape            none | item | list
```

`finished` is reserved for an applied Control. `value` is a lightweight Flow
operation over an available value; it does not run a child, parallelize, loop,
or represent runtime diagnostics.

| StepKind | Owners |
| --- | --- |
| run | run, scatter, gather |
| agent | seek |
| human | ask |
| par | storm, map, predicate keep/drop, rank |
| loop | repeat, settle |
| value | let, positional keep/drop |
| model | one model call |
| tool | one tool call |


## Common Data Shapes

```text
RunControlRef    {"run": RUN_ID, "index": INT}
ThreadControlRef {"thread": THREAD_ID, "index": INT}
RunInputRef      {"control": INT, "part"?: INT}
StepOutputRef    {"step": STEP_PATH, "part"?: INT}
ValueRef         RunInputRef | StepOutputRef

InvocationInput {
  "primary": PerceptPart[],
  "named": {NAME: JsonValue}
}

ErrorType
  model_error | tool_error | flow_error | store_error | limit_error |
  runtime_error

ExecutionError {"type": ErrorType, "message": STRING}

RunFailure
  {"kind": "step", "step": STEP_PATH}
  | {"kind": "runtime", "error": ExecutionError}

CeilingData {
  "models": STRING[],
  "tools": STRING[],
  "caps": STRING[]
}

RunLimitsData {
  "agic_model_calls": INT | null,
  "agic_tool_calls": INT | null,
  "tokens": INT | null,
  "cost": DECIMAL_STRING | null,
  "time": INT | null
}

Placement {
  "iter"?: INT, "iters"?: INT | null,
  "item"?: INT, "items"?: INT,
  "lane"?: INT, "lanes"?: INT
}
```

Integers are non-negative. Money is finite and non-negative. Reference
`part` selects one part; omission selects all parts. RunInputRef resolves an
entry control's `input.primary` or a steer control's `message.parts`; it
never selects named input.

`ExecutionError.type` is a stable code; `message` is non-empty diagnostic text
and is not an API-stable identifier. Errors are normalized once at the boundary
that owns the failed operation, then propagated unchanged:

| Boundary | ErrorType |
| --- | --- |
| model adapter invocation or response application | `model_error` |
| tool lookup, invocation, or returned tool error | `tool_error` |
| Flow statement validation, coercion, or local operation | `flow_error` |
| store codec, invariant, transaction, or projection | `store_error` |
| any configured call, token, cost, or time limit check | `limit_error` |
| uncaught execution failure outside those boundaries | `runtime_error` |

The limit boundary takes precedence over the operation it interrupts. A child
Run retains its own error; its parent records a causal Step failure. Existing
`ExecutionError` values are never reclassified. Otherwise, `message` is the
trimmed exception text, or the exception class name when that text is empty;
returned tool-error strings use the same trimming rule. One central redactor
runs before persistence. Tracebacks, credentials, provider requests, and
arbitrary exception attributes are never persisted. Cancellation is not an
error and produces no `ExecutionError`.

Placement indexes are zero-based and appear with their total. `iters=null`
means unbounded. Empty placement is omitted.


## Activation Snapshot

`run`, `rerun`, and `retry` are activation controls. Each stores:

```text
model       string | null
ceilings    CeilingData[]
limits      RunLimitsData
state_hash  lowercase hexadecimal string
attempt     RunAttempt
```

`model` is the effective default selector. `ceilings` is the concrete,
ordered effective chain: resolved agent ceiling, distinct inherited Flow
ceiling, and distinct executable ceiling. Values are canonical selectors, tool
names, and prepared cap refs; equal adjacent layers are collapsed.

Every child activation records its own snapshot. Retry and rerun use newly
resolved values rather than implicit current defaults.

```text
RunAttempt {
  status: RunStatus
  started_at: timestamp | null
  finished_at: timestamp | null
  output: ValueRef | null
  noted: RunNoted
  failure: RunFailure | null
}
```

An attempt moves `pending -> running -> terminal` and is immutable after its
terminal transition. Control status separately records whether the activation
request was applied.


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

`activation` identifies the latest attempt; older attempts remain on their
controls. `input` always references the index-zero `run` or `rerun`
control, including after retry.

```text
RunnableRef {"kind": RunKind, "name": STRING}

RunGiven {
  root: RunId
  runnable: RunnableRef
  placement: Placement | null
}
```

`RunnableRef.name` is the resolved canonical declaration name, including the
generated name of an inline runnable; its kind equals the Run kind. `RunGiven`
is closed and always serializes `placement`, using null for a root or otherwise
unplaced Run.

`RunNoted` is the following closed union selected by Run status and, for
success, result shape:

```text
pending | running | failed
  {}
succeeded, none
  {"shape": "none", "type": null}
succeeded, item
  {"shape": "item", "type": STRING | null}
succeeded, list
  {"shape": "list", "type": STRING | null, "items": INT}
canceled
  {"reason": STRING | null, "stop": RunControlRef | null}
```

Pending and running Runs have null output and failure. A succeeded Run has
null failure; its output is null exactly for `shape=none`. A failed Run has one
failure and may retain partial output. A canceled Run has null failure, may
retain partial output, and records an applied stop when one caused it. Every
listed key is serialized; keys belonging to another variant are omitted.

### RunControlRecord

```text
run, index, kind, timing
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

| Kind | Required fields | Fixed constraints |
| --- | --- | --- |
| run | input and activation snapshot | index 0; immediate |
| rerun | input, source, activation snapshot | index 0; immediate |
| retry | anchor and activation snapshot | index > 0; immediate |
| steer | user message | no activation fields |
| stop | optional reason | no activation fields |

All unspecified payload/reference fields are null. Request IDs are unique when
present. Failed Controls require an error; canceled Controls do not.
Revision and claim remain storage-only fields.

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

Every Step directly names its producing activation. Failed Steps require an
error; succeeded Steps have none. Retry marks invalid Steps ejected and uses
fresh physical indexes.

There is no PartRecord. `(StepPath, part index)` identifies streamed parts;
ordered completed PartEnd data is the Step output.


## Step Facts

All fact objects are closed: unknown keys are rejected. Every key shown is
required, including keys whose value is null. A key is omitted only when its
selected union variant does not list it. `Message`, `MessagePart`, and
`ToolDefinition` use their existing canonical codecs unchanged.

### StepGiven

```text
FlowStatement
  run | seek | ask | scatter | storm | gather | settle | map | keep | drop |
  rank | repeat | let
```

`ValueMeta` is a closed shape union:

```text
{"shape": "none", "type": null}
| {"shape": "item", "type": STRING | null}
| {"shape": "list", "type": STRING | null, "items": INT}
```

`FlowGiven` is `FlowGivenBase` plus exactly one row from the variant table.
`binding` is `_`, a canonical local name, or null for discarded output and
repeat. `current` describes `_` immediately before the Step. Source lines are
one-based; `head` is the canonical formatted statement head.

```text
FlowGivenBase {
  "statement": FlowStatement,
  "binding": STRING | null,
  "current": ValueMeta,
  "source": {"line": INT, "head": STRING},
  "doc": STRING | null,
  "placement": Placement | null
}
```

| FlowStatement variant | Additional required keys and constraints |
| --- | --- |
| `run` | `runnable: RunnableRef` |
| `seek` | `agent: STRING`, `runnable: RunnableRef` |
| `ask` | none |
| `scatter` | `count: INT`, `runnable: RunnableRef` |
| `storm` | `count: INT`, `runnable: RunnableRef`, `par: INT \| null` |
| `gather` | `runnable: RunnableRef` |
| `settle` | `runnable: RunnableRef` |
| `map` | `runnable: RunnableRef`, `par: INT \| null` |
| positional `keep` or `drop` | `position: first \| last`, `count: INT` |
| predicate `keep` or `drop` | `predicate: RunnableRef`, `par: INT \| null` |
| `rank` | `scorer: RunnableRef`, `limit: top \| bottom \| null`, `count: INT \| null`, `par: INT \| null` |
| `repeat` | `count: INT \| null`, `until: RunnableRef \| null` |
| `let` | none |

Counts are non-negative; non-null `par` is positive. Rank `limit` and `count`
are either both null or both present. Repeat requires `count` or `until`.
Runnable operands are resolved to canonical refs before StepBegin, including
generated inline agics. Authored `ask`/`let` bodies and the nested repeat AST
are intentionally omitted: the activation `state_hash` and source line identify
their immutable source, while nested Steps record repeat execution.

Model and tool Steps use these closed shapes:

```text
ModelTargetData {
  "ref": STRING, "provider": STRING, "name": STRING, "model": STRING,
  "adapter": STRING, "base_url": STRING | null, "scope": STRING | null,
  "tags": STRING[], "options": {STRING: JsonValue},
  "tools": BOOL, "streaming": BOOL
}

ModelCallData {
  "instructions": STRING,
  "messages": Message[],
  "tools": ToolDefinition[],
  "state": {STRING: JsonValue} | null
}

ModelGiven {"model": ModelTargetData, "call": ModelCallData}

ToolGiven {
  "tool": STRING, "plugin": STRING,
  "tool_call_id": STRING, "call_id": STRING
}

StepGiven = FlowGiven | ModelGiven | ToolGiven
```

Model headers and API keys are never facts. Step kind `model` requires
`ModelGiven`; `tool` requires `ToolGiven`; Flow-owned kinds require the
statement variant assigned to that kind in the vocabulary table.

### StepNoted

`ValueSnapshot` is a closed shape union. A list's `items` equals its array
length. A successful value-producing Step uses a `StepOutputRef` to itself;
repeat commit snapshots preserve the refs of their last nested producers.

```text
{"shape": "none", "type": null, "value": null, "ref": null}
| {"shape": "item", "type": STRING | null,
   "value": JsonValue, "ref": ValueRef | null}
| {"shape": "list", "type": STRING | null,
   "value": JsonValue[], "items": INT, "ref": ValueRef | null}

CommitMap {LOCAL_NAME: ValueSnapshot}
```

Succeeded `FlowNoted` is selected by statement:

| Statements | Closed StepNoted shape |
| --- | --- |
| `run`, `seek`, `ask`, `scatter`, `storm`, `let` | `{"result": ValueSnapshot, "commit": CommitMap}` |
| `gather`, `settle`, `map`, `keep`, `drop`, `rank` | `{"result": ValueSnapshot, "commit": CommitMap, "source_items": INT}` |
| `repeat` | `{"commit": CommitMap, "iters": INT, "stopped": BOOL}` |

`commit` is the exact atomic local change. `_` or a named binding stores the
result; discarded output uses `{}`. Repeat has no result or binding and commits
every net local change from its completed body; `iters` counts completed body
executions and `stopped` is true exactly when `until` returned true.
`source_items`, including zero, is the input-list size before the operation.

```text
ModelNoted {
  "tokens": {"input": INT, "output": INT} | null,
  "price": {"input": DECIMAL_STRING | null,
            "output": DECIMAL_STRING | null} | null,
  "cost": DECIMAL_STRING | null,
  "reasoning_content": STRING | null,
  "state": {STRING: JsonValue} | null
}

ToolNoted {}

StepNoted = FlowNoted | ModelNoted | ToolNoted | {}
```

Token and money values are non-negative. A succeeded model Step requires
`ModelNoted`; a succeeded tool Step requires `{}` and outputs exactly its
`ToolResultPart`. Status fixes fact and error presence for every Step kind:

| Step status | output | noted | error |
| --- | --- | --- | --- |
| `running` | `[]` | `{}` | null |
| `succeeded` | all completed parts | kind-specific success variant | null |
| `failed` | completed parts, possibly `[]` | `{}` | `ExecutionError` |
| `canceled` | completed parts, possibly `[]` | `{}` | null |

Terminal Steps require `finished_at`; running Steps require it to be null.
StepEnd accepts only terminal rows and serializes empty arrays, empty facts,
and null errors explicitly.

The progress model derives resolve from child Runs whose parent is the Step,
transform from statement plus source/result cardinalities, and commit from
`commit`. No Stage or Iteration record/event is needed.


## Placement And Until

- Repeat body Steps use `iter/iters`.
- The until evaluator is a child Run directly parented by the repeat Step and
  uses the same pair. The parent edge identifies until; `role=until` is gone.
- Parallel child Runs use `item/items` and `lane/lanes`.
- Settle child Runs use `iter/iters` for reducer calls and `item/items` for
  source positions.
- Placements compose for nested work. `loop` and `role` are removed.


## Event Schemas

All events keep their `type` discriminator and use the record shapes above.

```text
RunBegin   run, activation, kind, input, parent, given, started_at
RunEnd     run, activation, kind, status, output, noted, failure, finished_at
StepBegin  step, activation, kind, input, given, started_at
StepEnd    step, activation, kind, status, output, noted, error, finished_at
PartBegin  step, part, part_type
PartDelta  step, part, delta
PartEnd    step, part, data
```

RunEnd and StepEnd accept terminal statuses only. Optional fields serialize as
null; empty arrays and facts serialize explicitly.

RunBegin.input remains the immutable entry input; `activation` identifies
run/rerun/retry. Pass-through output uses RunEnd.output with RunInputRef.
RunEnd.noted identifies an applied stop. StepBegin.input identifies consumed
steers and value dependencies.


## Lifecycle And Control Invariants

- Run plus index-zero entry control is accepted atomically.
- Run events pair once per activation and agree on kind and activation.
- Step events occur inside their Run attempt and agree on kind and activation.
- Child Run events occur strictly inside their parent run Step.
- Part events balance inside one Step; succeeded streamed output equals ordered
  completed parts.
- Event projection and referenced Control transitions share one transaction.
- Root Runs have `parent=null` and `given.root=id`; children share the thread
  and root of their parent tree.
- Failed Runs have exactly one RunFailure. A causal Step is local to that Run;
  parent Runs reference their own failed containing Step.
- Runtime failure outside a Step is inline. No synthetic diagnostic Step exists.
- Cancellation is not failure and may retain a partial output.

Control behavior:

- `run` activates a new root or child from its stored invocation.
- `rerun` creates a new root from the latest visible terminal root, copies its
  invocation, records new activation settings, and ejects the source tree.
- `retry` appends to a visible terminal root, resolves an anchor, ejects the
  invalid Step/child suffix, fails stale pending controls, restores successful
  commits, and reopens the Run under a new activation.
- `steer` stores only `message`, applies in index order at its selected
  checkpoint, and is referenced as input by its consumer.
- `stop` stores optional `reason`, unwinds active work at its checkpoint, and
  is referenced by canceled RunNoted.
- Pending steer/stop may be canceled before claim. Activation or claimed
  controls cannot be canceled.

Each activation's RunAttempt retains its terminal outcome, so retry never
erases prior failure, output, facts, or timestamps.


## Persistence And API

The new schema:

- adds direct Run `kind`, `activation`, `given`, `noted`, and structured
  `failure`; removes Run `context` and string error;
- gives RunControl direct payload, activation snapshot, attempt, and structured
  error fields; removes generic context and overloaded input;
- adds Step `activation`, structured error, and only the new vocabulary.

The schema version is bumped. An empty database creates only the new schema.
Opening an older non-empty schema fails before mutation in read/write and
read-only modes. The store does not migrate, translate statuses, accept
`start` or `system`, dual-read, or delete history.

History and inspection expose direct kind/facts, structured failure, current
activation, and ordered activation attempts. HTTP creation converts transport
`input` and `args` once into InvocationInput. SSE uses the exact event codec.
Existing endpoint purposes, timing modes, request IDs, and StepPath syntax stay
unchanged.


## Design Touchpoints

- `execution/types.py`, `records.py`, and `events.py`: vocabulary, records,
  errors, facts, and codecs.
- `execution/store.py`: clean schema, attempt projection, commits, ejection,
  and atomic Control transitions.
- `execution/schemas.py` and `history.py`: direct caller-facing history.
- `execution/executor/`: activation binding, new events, value Steps, Flow
  facts, normalized placement, retry restore, and structured failures.
- `api/schemas.py`, `api/routers/runs.py`, and `api/common.py`: input
  conversion, responses, and SSE.
- CLI progress, script/chat tracers, and inspect: consume the new contract
  without layout changes.
- Execution/API/CLI tests and public execution documentation: replace legacy
  fixtures and describe the approved contract.


## Acceptance Tests

1. Codecs round-trip every closed record, fact variant, reference, failure, and
   event; unknown fact keys and legacy shapes are rejected.
2. New storage initializes cleanly; an old non-empty database fails unchanged.
3. Entry acceptance, event projection, retry reopen, and attempt termination
   are transactionally atomic.
4. Root, child, rerun, and retry activations persist model, ceilings, limits,
   state hash, and outcome; every Step identifies its activation.
5. Primary/named input persists together; rerun reconstructs it without current
   source or Run facts.
6. Retry retains attempts and ejected Steps, restores exact Flow commits, and
   attributes new Steps to fresh physical indexes and the new activation.
7. Every Flow statement emits its exact Given/Noted variant and StepKind,
   including nullable operands, empty work, list cardinality, and repeat commit.
8. Placement tests cover repeat, until, nested parallel work, and settle using
   only the three normalized pairs.
9. Model, tool, Flow, store, limit, fallback runtime, nested, and cancellation
   cases assert the normalization table and causal failures without synthetic
   Steps; empty exception text and redaction are covered.
10. Event integrity covers success, failure, cancellation, retry, nesting,
    parallelism, streaming, and pass-through output.
11. History, API, SSE, inspect, script, and chat consume the new vocabulary.
12. Default verification passes:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```


## Risks

- Existing history becomes unreadable until explicitly archived or removed;
  fail-before-mutation prevents accidental loss.
- RunRecord duplicates the latest activation outcome; atomic projection must
  prevent divergence.
- Lossless Flow snapshots may be large; inspection remains bounded and
  provider-only secrets remain excluded.
- Repeat commit reconstruction and stable error types require focused tests.


## Open Questions

None. The contract, lifecycle, clean break, implementation boundary, and
acceptance criteria are fixed.
