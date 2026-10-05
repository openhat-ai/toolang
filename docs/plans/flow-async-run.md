# Define async child runs and Run awaiting

Status: Proposed; feature definition, group 2 of 4. No implementation changes.

## Goal, Scope, and Dependencies

Start a child without waiting, optionally retain its typed Run handle, and await
its result at an explicit later statement. Ordinary run remains synchronous.
Success means execution overlaps without changing input snapshots, parent
ownership, root accounting, or result provenance.

Implement after [group 1](https://github.com/openhat-ai/toolang/pull/684)'s
complete-value Local model. Share the Run handle contract with
[spawn #687](https://github.com/openhat-ai/toolang/pull/687): the first launch
implementation supplies common value support; neither requires the other's
launch behavior. Scope includes async run, handle await, and let combinations.
Exclude await blocks, spawn execution, async modifiers on other operators,
authored generics, remote-agent launch syntax, timeouts, and handle methods.

## Verified Current Behavior

- Flow awaits statements before applying bindings. Bare run writes `_`, named
  let writes its destination, and nameless let discards the result.
- Run Steps accept then execute children. Children inherit root identity, thread,
  ceilings, limits, settings, and shared accounting; each has a durable run ID.
- Executor/RunClient handles are process-local interfaces, not language values.
- Run is not a built-in language type. Authored type syntax supports names and
  `[]`; retry reconstructs locals from committed Step outputs.

## Syntax, Types, and Binding

Let `T = Return<R>`, the complete output type of target runnable R:

```too
let research = async run investigate
async run review_risks
run draft
let evidence = await research
run: Research {{research.id}} is {{research.status}}.
```

| Form | Statement value | Destination |
| --- | --- | --- |
| `run R` | `T`, after completion | `_` |
| `let value = run R` | `T` | value only |
| `let run R` | `T` | Discard |
| `async run R` | `Run<T>`, after admission | None; preserve locals |
| `let job = async run R` | `Run<T>` | job only |
| `let async run R` | `Run<T>` | Discard |
| `await job` | `T`, after completion | `_`; retain job unless job is `_` |
| `let value = await job` | `T` | value only; retain job if names differ |
| `let await job` | `T` | Discard; retain job |

Await follows ordinary value-statement binding, also used by await blocks. It
never implicitly replaces its operand. Use `let job = await job` to explicitly
replace a named handle with its result; `await _` also replaces `_` through the
normal default destination. Only success writes a destination; failure preserves
all bindings.

Async run accepts ordinary named/inline targets: `async run [-> T]: BODY`, with
Text as the inline default and no using. No alternative declaration prefix or
async placement. Await takes exactly one local identifier, including `_`, not
an arbitrary expression, array of handles, or implicit await on argument use.

The operand must be a runtime handle, written `Run<T>` in this design, including
a handle for a completed or spawned run.
Retained handles can be awaited repeatedly without relaunching work. Replacing a
handle with `T` makes that local non-awaitable. Background completion never
changes bindings; status reads do not consume handles or deliver their results.

## Run Handle Contract

Use #687's runtime handle and public struct view: `id: Text`, `thread: Text`,
`status: Text`. Identity/thread are stable; execution projects current persisted
status once per referenced run per statement evaluation. Field projections and
explicit view rendering are ordinary data, not implicit awaits. General handle
parameters and containers remain out of scope. Returning the handle confirms
admission; it does not imply completion.

`Run<T>` and `Return<R>` are design notation, not language types or authored
generics. Track handle locals and their complete result contracts separately
from authored value types. Use the shared execution-owned record variant for
identity, thread, and result contract; do not persist status, output caches,
tasks, or executors. Add no Run built-in, reserved type name, or struct codec.
Execution validates identity, thread, and accepted output contract. Json objects,
authored structs named Run, and rendered views do not acquire handle semantics.

For `Run<Text[]>`, await returns `Text[]`, not one element. A spawned root and
an async child have the same field/await interface; their original ownership
rules determine cancellation and accounting. Await never transfers ownership.

## Launch, Ownership, and Failure

- Validate and bind at launch using ordinary run resolution, contract, ancestry,
  and authorization rules. Capture inputs, named locals, iteration history, and
  execution context. Later local changes do not alter the invocation; await
  neither resolves the runnable again nor launches it lazily.
- Return after durable acceptance and executor registration, without promising
  a provider call has started. Admission failure publishes no handle.
- The immediate launching run owns each child inside its root tree and budgets.
  Serialize shared accounting/events while parent and children overlap; add no
  global task pool or separate root accounting.
- On normal parent return, failure, or cancellation, cancel and drain unfinished
  owned children before marking the parent terminal. Do not wait for successful
  business results. Exec drains the outgoing segment's async children before
  replacement execution. Spawned roots retain independent lifetimes.
- Discarding/overwriting handles does not cancel work or remove ownership. Repeat
  iteration exit does not end the owner or cancel earlier iteration children.
- Record child failures immediately and surface them at await, without changing
  locals. An unawaited child failure does not fail an otherwise successful
  parent. Shared-root exhaustion and parent cancellation still affect the tree.
- A canceled wait leaves independent targets alone; normal parent cleanup still
  applies to its owned children. A failed/canceled target fails await with its
  error/status reference, never a null success or unrelated parent cancellation.
  External effects are not rolled back.

## Durable Execution and Recovery

- Add RunStmt's asynchronous flag, default false for old records, and AwaitStmt
  with an operand local and ordinary result binding (bare default `_`).
- Async launch is a run-kind Step with runtime handle output, including when unbound.
  Its success means acceptance; the child retains its parent Step and outcome.
  Commit acceptance and handle output atomically before delivery.
- Await is a value-kind Step recording the target and successful output reference
  and binding. Preserve the complete result and provenance.
- Retry restores committed handles/results, including repeats, without replaying
  launches or applying child locals to the parent. A failed/canceled handle stays
  tied to that outcome; whole-run rerun intentionally creates fresh launches.
- Await live targets through the current executor registry and terminal targets
  through records. A nonterminal target without a local owner fails promptly as
  unavailable; status remains inspectable. Missing/pruned or incompatible targets
  fail explicitly. Do not hang, fabricate terminal state, or recreate work.

## Implementation Touchpoints

- Upstream grammar and `src/toolang/lang/{ast,lower,contracts,flow_validation,
  format,description}.py`: async modifier, AwaitStmt, handle/result contract tracking,
  binding transitions, field access, and diagnostics.
- `src/toolang/execution/executor/{executor,common,content,frame}.py`,
  `stmts/{run,await}.py`, `steps/run.py`, `runs/flow.py`, and `iteration.py`:
  concurrent ownership, cleanup, status projection, waiting, accounting, retry.
- `src/toolang/execution/{types,records,store,events,schemas}.py`: shared Run codec,
  atomic handle output, provenance, restoration, and inspection.
- Parser/static, execution, ownership, metadata, persistence, and retry tests.

Publish/pin the matching grammar and update the lockfile. Reserve async/await
statement starts with useful errors; unsupported forms must not become prose.
Keep historical records readable and generate the implementation changelog
through the repository runnable. This definition changes no product behavior.

## Acceptance Tests

1. Parse/check/format all table forms, targets, annotations, and repeat nesting.
   Verify bare await writes `_`, named let writes only its destination, explicit
   self-binding replaces the handle, and discard preserves locals. Reject
   non-handles and unsupported syntax without implicit text fallback.
2. Gates prove overlapping execution, launch before completion, captured inputs/
   history, and no background binding mutation. Check stable id/thread, current
   status snapshots, metadata captures, and view rendering without result access.
3. Await every complete result shape with exact type/provenance. Reuse retained
   handles and preserve metadata after `let result = await job`; reject reawait
   of a replaced `T` and lookalike Json. Root/child handles obey the same interface.
4. Parent return/failure/cancel/exec and executor shutdown drain owned children;
   discarded handles do not leak tasks. Waiting/canceling a wait does not adopt
   or cancel an independently owned target.
5. Distinguish admission failure from target failure/cancellation, shared limits,
   and event ordering. No failed await writes a destination.
6. Round-trip runtime handles; fault-inject admission/delivery. Retry preserves targets,
   bindings, and effects. Cover terminal, missing, incompatible, ownerless, and
   repeated handles. Run remains an available authored struct name, with no handle
   semantics; existing struct records retain their encoding.
7. Validate documentation/examples/links. Implementation runs all default checks
   with offline deterministic concurrency tests.

## Risks and Approval

Risks are leaked children, admission/completion confusion, stale status mistaken
for live ownership, duplicate launches, implicit awaiting, and accidental handle
replacement. The type/binding rules and ownership checks address these. The
complete definition still requires human approval; no implementation is shipped.
