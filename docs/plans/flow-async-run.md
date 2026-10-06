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

`Awaitable<T>` is design notation, not an authored generic type. It represents an
executor-recognized computation with a stable identity, lifecycle state, complete
result contract, and eventual outcome. Run is one implementation, not the interface.

| Value | Meaning | Successful result |
| --- | --- | --- |
| Run handle | One async child or independent spawned root | Complete Run result `T` |
| Operation handle | One map, parallel block, or other supported computation | Complete operation result `T` |
| `Awaitable<T>[]` | Ordered collection of existing handles | Ordered `T[]` |
| `Awaitable<T[]>` | One computation producing an array | Its complete `T[]` |

An operation handle identifies the whole computation, not a preallocated list of
child run IDs. Children may be admitted as lanes become available. Async starts
scheduling after admission; await never launches work lazily or transfers ownership.

Preserve existing Run metadata and `_Run` encodings from the spawn contract.
Operation identities/result contracts must be durable; live tasks stay in the
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

Keep the existing Run/Step/Part event families for async run. Flow uses a run-kind
launch Step with `RunStmt.asynchronous`, defaulting to false for historical records;
agic uses a Tool Step whose run arguments identify async delivery. The launch Step's
success means admission and durable handle output, not completion of the child.

A typical interleaving is:

```text
StepBegin(launch, run, asynchronous=true)
RunBegin(child, parent=launch)
StepEnd(launch, succeeded, output=handle)
StepBegin(other parent work)
... child and parent Step/Part events interleave ...
StepBegin(wait, value, targets=[child])
RunEnd(child, succeeded, output=result)
StepEnd(wait, succeeded, output=result reference)
```

This is an illustrative trace, not new event constructor syntax. Record ordered
wait targets at entry, not only operand names or success-time output references.
Required order is launch begin before child admission before successful launch end;
each entity's begin precedes its end. Child completion may precede handle delivery
or the wait. An already-terminal target needs no replayed Run/Part events. Waiting
never emits a second RunBegin, reparents the child, or duplicates its accounting.
Persist events before observation and serialize shared-root event/accounting updates.

The child's parent remains the launch Step after that Step ends; its lifetime owner
remains the launching Run. Wait edges are dependencies, not ownership edges. A later
child failure ends the child and fails a wait with the original error reference;
the successful launch stays successful. Target-only cancellation is an await error,
while cancellation of the caller cancels its wait. Parent RunEnd and exec replacement
must follow cleanup of unfinished owned work. Admission/delivery interruption must
not leave an accepted child without registry ownership or durable handle recovery.

Progress consumers must stop assuming that every child finishes before its source
Step. Currently `execution_progress/projector.py` rejects StepEnd with an active
child Run and aggregates child metrics through active source Steps. Permit this
overlap for explicit async launches, retain source/owner metadata after launch end,
and preserve synchronous nesting checks. Show launch success, live child progress,
and waiting as separate facts; do not display child success when only launch ended.
Attribute child cost once even if its handle is awaited repeatedly or in a group.
Audit CLI/TUI, event streams, stored inspection, and retry for the same ordering.

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
  consumers, and progress tests: overlapping lifetimes, presentation, and accounting.

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
5. Round-trip handles and fault-inject admission/delivery; retry preserves bindings
   and promptly rejects unavailable targets. Historical Run records still decode.
6. Flow/tool parity, authorization, error replies, and historical event decoding pass.
7. Replay traces where child events continue after launch end, children finish before
   await or handle delivery, multiple awaits observe one outcome, and cancellation
   races admission. Progress retains owner links and counts child metrics once;
   synchronous nesting errors still fail. Run the default offline checks.

Publish/pin matching grammar before release; validate proposed examples against it.
Generate the implementation changelog through the repository runnable.

## Open Decisions and Risks

Before implementation approval, settle:

1. Whether native handle arrays and direct async collection operators ship initially
   or later; if arrays ship, define construction/binding syntax and the runtime codec.
2. The operation-handle representation, concrete tool target schema, and durable
   launch/completion Step layout shared with #686. A completion Step reference is a
   candidate; neither a Run identity nor all child IDs may be assumed.

Risks are conflating handle collections with array-valued operations, admission
with completion, or observation with ownership. This revision records the design
direction, not approval of a complete implementation scope. No product behavior
or changelog changes belong to this definition.
