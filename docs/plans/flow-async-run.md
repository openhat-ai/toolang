# Define async run and single-target await

Status: Phase-one definition. The agreed implementation scope is async run,
single-target await, and the shared contracts below. This PR contains no product
implementation. Phase two is saved as a deferred draft in
[#686](https://github.com/openhat-ai/toolang/pull/686).

## Goal and Scope

Start a child Run, continue the caller immediately, and optionally retrieve its
complete result later. Support flow syntax and equivalent agic runtime tools.
Replace the internal Run handle type with `_Awaitable`, including spawn outputs.
Keep caller Steps linear and present run, exec, async run, spawn, and await together.

Phase two owns `await ha, hb`, native handle arrays, async/await blocks, and direct
async collection operations. Phase one provides their extension points without
implementing their syntax, containers, or scheduler. Remote dispatch, timeouts,
automatic restart, and direct async repeat remain outside scope.

Verified baseline: flow dispatch is sequential; spawn returns an independent-root
handle; map admits children as lanes become available; repeat mutates enclosing
locals and has no result. Async/await is not implemented. Integrate a matching
grammar before shipping; the installed grammar does not recognize these forms.

## Shared Rules for Both Phases

1. **One handle type.** `_Awaitable` identifies a computation with a separate
   complete result contract. Run and operation are target variants. No `_Run`,
   generic type-name suffix, compatibility alias, or legacy handle decoder remains.
2. **Whole computation and result.** An operation handle does not contain
   preallocated child Run IDs. `Awaitable<T[]>` is one computation yielding an
   array; `Awaitable<T>[]` is a handle collection. Await never flattens results.
3. **Separate launch and wait.** Async schedules after admission; await observes
   existing work. Repeated waits reuse the outcome and never relaunch it.
4. **Separate ownership and observation.** Launch determines ownership; receiving,
   discarding, or awaiting a handle does not transfer it. Canceling an observer
   does not cancel its target. Owner cleanup is a separate action.
5. **Linear caller Steps.** Async launch ends at startup; an authored await is one
   blocking Step. Background progress has its own scope: it never keeps launch
   live, reopens it, or moves beneath await. Account for target costs once.
6. **One presentation family.** Flow and agic share action/target layout, lifecycle
   meanings, and secondary details for all five operations below.

Both implementation acceptance suites must verify these rules. Phase two extends
target variants and wait inputs, not these contracts.

## Phase-One Syntax and Binding

```too
let research = async run investigate
let review = async run: Review the proposed changes.
run draft
let findings = await research
await review
```

| Form | Completion and binding |
| --- | --- |
| `let h = async run R` | Return after admission; bind only `h` |
| `async run R` / `let async run R` | Launch, discard handle, preserve locals |
| `await h` | Wait; bind complete result to `_` |
| `let result = await h` | Wait; bind only `result` |
| `let await h` | Wait and discard result |

Async run supports existing named, inline, and typed-inline targets. Await requires
one retained named local handle, including one returned by spawn. Follow existing
variable-name rules: `_` remains the primary result destination, not an authored
handle name. Reject expressions, comma operands, arrays, nested `await spawn`, and
unsupported async/await heads instead of parsing them as prose.

Only successful output writes a destination. No output preserves it; null is a
result. The source handle survives unless explicitly replaced by `let h = await h`.
Preserve complete scalar/array/struct/Part results and provenance. Handles are not
authored types, runnable inputs/returns, or container elements. Template projections
remain ordinary `id`, `thread`, and `status` metadata; Json lookalikes are not handles.

## Handle and Runtime Interface

Use the existing Output envelope with type `_Awaitable` and native payload
`{kind: "run", id, thread, result_type}`. References are canonical; `result_type`
is an authored type name or null. Persist the complete contract, including struct
definitions, with target admission and validate the handle against it. A null
declared contract does not preclude a flow's eventual inferred output. Live tasks
belong only in the executor registry.

Dispatch handle resolution by `kind`; initially accept only `run` and explicitly
reject unknown kinds. Phase two can add an operation identity without synthetic
Runs or Run arrays. No placeholder operation scheduler/records are required now.
Keep existing spawn metadata and public replies. Document the incompatible internal
encoding change and its retry/inspection impact in the implementation changelog.

| Agic tool | Behavior |
| --- | --- |
| `_toolang/run(runnable, input, async=false)` | Existing synchronous default; true returns `{id, thread, status}` after admission |
| `_toolang/spawn(runnable, input)` | Existing independent-root behavior and reply |
| `_toolang/await(target=REF)` | One target reference; complete `{type, value}` result or existing empty reply |

Phase one uses a string target with Run references. The resolver boundary permits
future operation references; phase two can extend the input with an ordered array.
Use `asynchronous` and `await_target` for Python names. Flow/tools share execution-
owned admission/wait services and ordinary run authorization/target resolution.

A tool may await a target admitted by its caller Run, including its spawned roots;
arbitrary IDs grant no access. Validate native handles against the same provenance.
Reject self/ancestor waits. Sequential agic tool calls can overlap background work.
Target failure/cancellation gives a tool error; caller cancellation interrupts the
caller. No operator-specific tools or implicit/recursive awaits are introduced.

## Admission, Ownership, and Recovery

- Capture inputs, locals, history, iteration values, and context at launch.
  Completion never writes parent locals. Commit admission and launch handle output
  together; register execution before exposing success. Interrupted delivery must
  not lose accepted work or duplicate admission.
- The immediate launching Run owns async children under its root budgets. Parent
  return/failure/cancel/exec and executor shutdown cancel/drain unfinished owned
  work. Discarding a handle or leaving an iteration does not end ownership.
  Spawned roots retain independent lifetimes and executor-shutdown cleanup.
- Record background failure immediately; await surfaces the original error reference.
  Unawaited failure alone does not fail the parent; shared-root limits still apply.
  Await cancellation stops only the waiter. No failed wait publishes partial output
  or rolls back external effects.
- Retry restores committed handles/results and provenance without replaying launches
  or child bindings. Rerun can create fresh work. Resolve terminal outcomes from
  records and live targets through the registry. Missing, pruned, incompatible, or
  nonterminal ownerless targets fail explicitly without restart or indefinite waiting.

## Steps, Events, and Presentation

Use a run-kind Step with `RunStmt.asynchronous` (historical default false), and a
value-kind `AwaitStmt`; agic retains Tool Steps. Record the resolved wait target
at Step entry so live waits are inspectable. Durable launch output and public
StepEnd must agree even when delivery is interrupted.

```text
StepBegin(S1, async run)
StepEnd(S1, succeeded, output=handle)
StepBegin(S2, next statement)
StepEnd(S2, succeeded, output=value)
StepBegin(S3, await handle)                 # Only if authored
StepEnd(S3, succeeded, output=result_ref)
```

This illustrates ordering, not new event constructors. Background Run/Step/Part
events retain their Run identity and source link; routing recognizes async sources
from recorded statement/tool arguments. Preserve launch provenance and immediate
Run ownership independently of live Step containment. Raw transport may interleave
scopes; caller CLI/TUI projections stay linear, while inspection follows the handle
into its target scope. Await never replays target events/costs, and a later failure
never rewrites successful launch. Already-terminal waits may complete immediately.

Adapt the progress projector's active-child, source lookup, and metric assumptions;
retain synchronous nesting checks. Serialize root accounting and persistence. A
target may finish before handle delivery or after source Step completion.

| Operation | Live | Success | Step success means |
| --- | --- | --- | --- |
| `run R` | Running | Completed | Child execution finished |
| `exec R` | Transferring | Transferred | Same-Run handoff committed; caller does not resume |
| `async run R` | Starting | Started | Owned background work admitted |
| `spawn R` | Spawning | Spawned | Independent root admitted |
| `await h` | Waiting | Completed | Existing target outcome received |

Keep action/readable target primary; place results, identity, timing, and errors
in consistent secondary positions. Share Failed/Canceled styling and existing
surface conventions, including narrow layouts. Hide internal type tags. Normalize
semantics across flow/agic from syntax/tool arguments: `_Awaitable` alone cannot
identify spawn or imply target completion.

## Touchpoints and Acceptance

- Grammar and `src/toolang/lang/{ast,lower,contracts,flow_validation,format,
  description}.py`: syntax, result tracking, diagnostics, formatting.
- `src/toolang/execution/executor/` and `execution/{types,records,store,events,
  schemas}.py`: admission, snapshots, ownership, registry, durable results/recovery.
- `src/toolang/base/protocols/tool.py`, `execution/tools/_toolang.py`, and
  `execution/executor/tool_runtime.py`: schemas/tools and shared services.
- `src/toolang/cli/common/execution_progress/`, `script_progress/`, and stream/
  inspection consumers: linear scopes, unified rows, accounting.

Acceptance scenarios:

1. Parse/check/format every phase-one binding/target form; diagnose phase-two forms,
   non-handles, forged handles, and inaccessible references.
2. Deterministic gates prove overlap and launch-time isolation, including repeat
   capture; discarded handles start work without waiting or mutating parent locals.
3. Single/repeated waits preserve whole scalar/null/array/struct/Part results and
   provenance. Cover no output, failure, target/caller cancellation, spawned roots,
   and replacing the operand binding.
4. Parent return/failure/cancel/exec and shutdown drain owned tasks. Observer
   cancellation preserves targets; spawned roots retain their independent lifetime.
5. Round-trip only `_Awaitable`; reject old encoding. Fault-inject admission/delivery
   and retry terminal/live/unavailable targets without duplicate work.
6. Flow/agic parity covers authorization, errors, and root limits. Interleave events
   before delivery/during later Steps, with omitted/repeated awaits: caller rows stay
   linear, costs occur once, synchronous nesting checks remain effective.
7. Review all five operations together across live/success/failure/cancel states and
   narrow layouts. Keep these shared-rule cases when phase two is implemented.

Pin matching grammar, validate public examples, generate the changelog through the
repository runnable, and pass default offline checks before implementation handoff.
No phase-one product-scope questions remain. Main risks are admission/delivery races,
losing ownership during exec, and treating provenance as live UI containment.
