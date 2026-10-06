# Define async execution and awaitable results

Status: Proposed; revised feature definition. Implementation is pending. Resolve
the open decisions below and obtain human approval before implementation.

## Goal and Work Split

Start work without waiting and retrieve its complete result through a shared
awaitable contract, preserving input snapshots, ownership, and result provenance.
Two features remain to be implemented:

1. **Async/await:** this plan covers the shared contract, async run, single- and
   multiple-target waiting, and extension to other operations.
2. **Async/await blocks:** [#686](https://github.com/openhat-ai/toolang/pull/686)
   adds one parallel group with immediate handle delivery (`async:`) or immediate
   waiting (`await:`), using the same contract.

Complete-value arrays from [#684](https://github.com/openhat-ai/toolang/pull/684)
and [spawn handles](./flow-spawn.md) are implemented. Reuse those contracts.
Examples below are proposed syntax, not commands supported by today's CLI.
Remote dispatch, timeouts, and automatic restart remain outside scope.

## Verified Current Behavior

- Flow executes statements sequentially and binds results after success. Ordinary
  statements receive locals snapshots; repeat mutates enclosing locals and has no
  result or binding.
- Spawn returns a durable Run handle; native handles cannot currently be runnable
  inputs, results, or container elements. Python task handles are separate,
  process-local objects.
- Map acquires a lane before accepting each child Run; an empty map has no children.
  Async/await and the corresponding runtime tools are not implemented.

## Awaitable and Result Contracts

Use `_Awaitable` as the common internal runtime type identifier for all awaitable
handles, including Run and operation targets. It is not an authored type or
constructor. `Awaitable<T>` below is design notation only: the runtime tag stays
`_Awaitable`, with `T` carried by the result contract rather than a type-name suffix.
Each handle identifies an executor-recognized computation with a stable identity,
lifecycle state, complete result contract, and eventual outcome.

| Value | Meaning | Successful result |
| --- | --- | --- |
| Run handle | One async child or independent spawned root | Complete Run result `T` |
| Operation handle | One map, parallel block, or other supported computation | Complete operation result `T` |
| `Awaitable<T>[]` | Ordered collection of existing handles | Ordered `T[]` |
| `Awaitable<T[]>` | One computation producing an array | Its complete `T[]` |

An operation handle identifies the whole computation, not a preallocated list of
child run IDs. Children may be admitted as lanes become available. Async starts
scheduling after admission; await never launches work lazily or transfers ownership.

New handle outputs from async run, spawn, and async operations use `_Awaitable` in
the existing Output envelope. The payload identifies the target kind and identity;
the result contract determines what await returns. Run versus operation is a target
distinction, not a separate runtime type. Native handle arrays, when enabled, use
`_Awaitable[]`; one handle yielding an array still has type `_Awaitable`.

Replace the current Run-specific handle encoding outright; retain no separate Run
handle type, legacy alias, or compatibility decoder. Preserve Run metadata and the
public spawn tool reply from the spawn contract. Record the storage-format change
in the implementation changelog; old handle encoding is not a compatibility target.
Target identities/result contracts must be durable; live tasks stay in the
executor registry. Status does not prove a live owner exists. Repeated awaits reuse
the outcome. Rendering fields, ordinary values, and Json lookalikes do not await
or acquire handle semantics.

## Flow Forms and Binding

```too
let research = async run investigate
let review = async run: Review the proposed changes.
run draft
let results = await research, review
```

| Form | Completion and binding |
| --- | --- |
| `let h = async run R` | Return after admission; bind only `h` |
| `async run R` / `let async run R` | Launch, discard handle, preserve locals |
| `await h` | Wait; bind complete result to `_` |
| `let result = await h` | Wait; bind only `result` |
| `let await h` | Wait and discard result |
| `await ha, hb` | Wait for all; bind ordered result array to `_` |
| `let results = await ha, hb` | Bind only `results` |
| `await handles` | Apply collection rule to a native handle array, once enabled |

Async run accepts existing named/inline targets and return annotations. Named and
discarded bindings also apply to collection waits. Operands are local identifiers,
including `_`; comma operands are individual handles. No arbitrary expressions,
implicit argument awaits, or recursive unwrapping. Only success writes a destination.
Handles survive unless explicitly replaced, as in `let h = await h` or `await _`.

Collection results follow operand order, never completion order. Each operand
contributes its complete result: two `Text[]` results produce `Text[][]`.
Homogeneous contracts give `T[]`; differing/unknown contracts give `Json[]`, retaining
item provenance. Null is a result, not absent output. A single no-output target
completes without binding; a collection requires one result per operand. Empty
handle arrays retain the element contract when known, otherwise produce `Json[]`.

Comma syntax can use an operand list without first exposing general handle arrays.
Native arrays need a runtime container, codec, and static checks; Json serialization
is not a substitute. Their construction syntax and delivery phase remain open.

## Other Statements

For result-producing `S: T`, direct `async S` would return `Awaitable<T>`.
Async generate/map/reduce/keep/drop/sort return one operation handle; their lanes
control internal concurrency independently of whether the caller waits. Their
first-release scope remains open.

Do not extend async indiscriminately. Repeat has shared local mutation and no
result; use `async run` on a helper flow containing repeat. Direct async repeat
needs a separately approved isolated scope and completion/result contract, without
implicit merging into parent locals. Exec, spawn, literal bindings, and nested
async/await modifiers gain no new forms here.

## Agic Runtime Tools

Proposed tools share execution-owned launch/wait services with flow:

| Tool | Behavior |
| --- | --- |
| `_toolang/run(runnable, input, async=false)` | Preserve synchronous default; `true` returns a Run handle after admission |
| `_toolang/spawn(runnable, input)` | Preserve independent-root behavior and existing reply |
| `_toolang/await(target=REF)` | Return complete result as `{type, value}` |
| `_toolang/await(target=[REF, ...])` | Return ordered results using the collection rule |

The target interface must support Run and operation references. Validate access,
identity, result contract, and availability; reject self/ancestor waits. References
do not grant authority. Async run retains hands authorization and ordinary target
resolution. Python names can use `asynchronous` and `await_target`.

Sequential agic tool batches still allow async launches to overlap. Await pauses
that agic until its tool reply, not the executor. Target failure/cancellation is a
tool error; caller cancellation interrupts the caller. No output gives the existing
empty tool reply. Agic can call flows containing collection operations; no separate
map/sort tools or generic code-execution tool are required.

## Ownership and Durable Execution

- Capture inputs, locals, history, and context at launch. Background completion
  never changes parent bindings. Commit admission and handle output before delivery;
  admission failure publishes no handle.
- The immediate launching Run owns async children/operations under its root budgets.
  Parent return/failure/cancel/exec and executor shutdown cancel and drain unfinished
  owned work. Discarding handles or leaving a repeat iteration does not end ownership.
  Spawned roots retain their independent lifetimes.
- Record background failure immediately and surface it at await. Unawaited failure
  alone does not fail the parent; shared-root limits still apply.
- Waiting only observes existing targets. A failed collection wait stops remaining
  waiters without canceling targets; parent cleanup is separate. Blocks own newly
  launched branches and follow #686's cleanup rules. No failed wait publishes a
  partial binding or rolls back external effects.
- Retry restores committed handles/results and provenance without replaying launches
  or child bindings. Rerun may create fresh work. Read terminal outcomes from records
  and live targets through the registry. Missing, pruned, incompatible, or nonterminal
  ownerless targets fail explicitly without restart or an indefinite wait.

## Steps, Events, and Progress

The caller's flow Step sequence stays linear. `async run` is one launch Step:
live means starting the asynchronous Run, and success means started. End it as soon
as admission, executor registration, and durable handle output succeed, then execute
the next statement. Do not keep it live for background work or require a later await.

`await` is a separate, optional blocking Step. It stays live until the awaitable
has an outcome, then publishes its result or error. An already-terminal target
completes this Step immediately. Omitting await creates no wait Step and no implicit
wait for a successful result; owner-lifecycle cleanup remains a separate rule.

The caller's event sequence is:

```text
StepBegin(S1, async run)
StepEnd(S1, succeeded, output=handle)
StepBegin(S2, next statement)
StepEnd(S2, succeeded, output=value)
StepBegin(S3, await handle)                 # Only if authored
StepEnd(S3, succeeded, output=result_ref)
```

This is an illustrative trace, not new event constructor syntax. Flow can retain
a run-kind launch Step with `RunStmt.asynchronous` (historical default false) and
a value-kind AwaitStmt; agic uses the corresponding Tool Steps. Record ordered wait
targets at entry so waiting is inspectable before a result exists.

The background target has its own Run/Step/Part events and execution scope. Those
events describe its lifecycle, not additional caller Steps or live children of the
launch Step. Raw transport may multiplex scopes; route/project events by scope so
background progress never reopens or inserts rows into the caller's linear timeline.
Inspect background execution through its handle/target scope. Await observes its
outcome without replaying its events or moving its progress under the wait Step.

Preserve launch provenance and Run ownership independently of live Step state;
they do not imply temporal containment. A later target failure changes its outcome
and fails a wait with the original error reference, never the successful launch.
Target-only cancellation is an await error; caller cancellation cancels its wait.
Persist events before observation, serialize root accounting, and attribute target
cost once, regardless of how often it is awaited. Admission/delivery interruption
must not leave accepted work without registry ownership or durable recovery.

Current `execution_progress/projector.py` assumes live source Steps contain child
Runs and rejects StepEnd while they remain active. Async launch must not enter that
synchronous presentation scope. Adapt event routing, source/owner lookup, and metric
aggregation instead of retaining a live launch row. Preserve ordinary synchronous
nesting checks and apply the same scope separation to CLI/TUI, streams, and inspection.

## Invocation and Await Presentation

Design `run`, `exec`, `async run`, `spawn`, and `await` together as one execution family.
Within each existing CLI/TUI surface, use the same action/target layout, spacing,
status treatment, and secondary detail placement. Retain existing surface styling;
do not invent a separate async visual system. Identify the operation explicitly so
ownership and completion differences remain visible without relying on color alone.

| Operation | Live state | Successful state | Step success means |
| --- | --- | --- | --- |
| `run R` | Running | Completed | Child execution finished |
| `exec R` | Transferring | Transferred | Handoff committed; caller does not resume |
| `async run R` | Starting | Started | Owned background work admitted; handle available |
| `spawn R` | Spawning | Spawned | Independent root admitted; handle available |
| `await h` / `await ha, hb` | Waiting | Completed | Awaited outcomes received; no new work started |

These labels specify lifecycle meaning within a shared row layout. Keep the action
and readable target primary; put result summaries, handles, run/thread references,
and timings in the same secondary positions when applicable. Async run/spawn success
does not claim the target finished. Exec's target continues the same Run, not a new
child; preserve the handoff boundary. Await names its handle or ordered target set,
with available target descriptions as details; do not invent a runnable for a map
or group handle. A collection await is one blocking Step. An already-completed target
can complete that Step immediately without showing a start/launch phase. Use shared
Failed/Canceled styling, with errors attributed to the relevant operation or wait.
Keep internal type tags and payload encodings out of normal progress labels.

Normalize operation semantics once for native flow statements and agic runtime tools.
Determine async delivery from the statement/tool arguments, not `_Awaitable` output:
async run, spawn, and blocks share that type. Existing presentation treats any flow
RunHandle output as "Spawned" and runtime run tools as synchronous run scopes; replace
those assumptions when implementing this family. Review all five operations together
in live, successful, failed, and canceled states, including narrow terminal layouts.

## Implementation Touchpoints and Acceptance

- Grammar and `src/toolang/lang/{ast,lower,contracts,flow_validation,format,
  description}.py`: syntax, handle/result tracking, and diagnostics.
- `src/toolang/execution/executor/{executor,common,content,iteration}.py`,
  `stmts/`, `steps/`, and `runs/flow.py`: admission, snapshots, ownership, waiting.
- `src/toolang/base/protocols/tool.py`, `src/toolang/execution/tools/_toolang.py`,
  and `src/toolang/execution/executor/tool_runtime.py`: tool factory/schema and services.
- `src/toolang/execution/{types,records,store,events,schemas}.py`: trusted handles,
  operation identities, durable outputs, restoration, and inspection.
- `src/toolang/cli/common/execution_progress/`, `script_progress/`, event-stream
  consumers, and progress tests: linear caller Steps, background scopes, and accounting.

Acceptance scenarios:

1. Parse/check/format approved forms and binding variants; reject unsupported forms,
   non-handles, forged handles, and inaccessible targets without prose fallback.
2. Gates prove overlap, launch-time capture, lane-limited child admission, empty
   operations, and no background mutation of parent locals.
3. Single/repeated/multiple waits preserve types, nesting, order, metadata, and
   provenance. Cover Run/operation targets, duplicate operands, null/missing output,
   failures, and native empty arrays if that syntax is included.
4. Parent lifecycle drains owned work; observer cancellation preserves targets;
   group cleanup respects ownership. No leaked tasks or duplicate effects.
5. Round-trip `_Awaitable` Run/operation handles with scalar and array result
   contracts; all writes use the same tag, with no legacy handle type or decoder.
   Fault-inject admission/delivery; retry preserves bindings and rejects unavailable
   targets. Incompatible persisted handle encodings fail explicitly.
6. Flow/tool parity, authorization, error replies, and event round-trips pass.
7. Replay background events during later caller Steps, before await/handle delivery,
   with no await, and across repeated awaits. The caller remains linear: only launch
   is live while starting; only an explicit await waits for the target. Background
   events never reopen/inject caller rows. Cover admission cancellation, retained
   ownership, and accounting exactly once; synchronous nesting errors still fail.
   Run the default offline checks.
8. Review run/exec/async run/spawn/await together across flow and agic: consistent row
   anatomy, target/identity formatting, and failure/cancel styling; correct live and
   terminal wording, scope, and result/handle details. Include single/multiple and
   already-completed wait targets. Handle type alone never labels an operation as
   spawn or keeps a launch live for background execution.

Publish/pin matching grammar before release; validate proposed examples against it.
Generate the implementation changelog through the repository runnable.

## Open Decisions and Risks

Before implementation approval, settle:

1. Whether native handle arrays and direct async collection operators ship initially
   or later; if arrays ship, define construction/binding syntax and the runtime codec.
2. The `_Awaitable` payload codec, concrete tool target schema, and background
   execution-scope records shared with #686. The internal type identifier is fixed;
   the remaining representation must preserve linear caller Steps and cannot assume
   a Run identity or preallocated child IDs.

Risks are conflating handle collections with array-valued operations, admission
with completion, or observation with ownership. This revision records the design
direction, not approval of a complete implementation scope. No product behavior
or changelog changes belong to this definition.
