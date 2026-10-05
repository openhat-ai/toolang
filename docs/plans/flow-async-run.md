# Define Async Child Runs and Future Awaiting

Status: Proposed; feature definition, group 2 of 4. The human confirmed bare
async does not bind, await's destination rules, and cancellation/cleanup of
unfinished children when their parent run ends. Other details below are proposed
for approval. This PR contains no implementation.

## Goal, Scope, and Dependencies

Start a child run without waiting for its result, optionally retain a typed
future, and wait at an explicit later statement. Ordinary run remains synchronous.
Success means execution overlaps deterministically without changing input
snapshots, parent ownership, root accounting, or result provenance.

Implement after [group 1](https://github.com/openhat-ai/toolang/pull/684)'s
complete-value Local model. Scope is async run, handle await, and their let
combinations. Exclude await blocks (group 3), spawn (group
4), async modifiers on other operators, general authored generics, remote-agent
launch syntax, timeouts, and future cancellation/steering methods.

## Verified Current Behavior

- Flow execution awaits each statement before applying its binding. Bare run
  binds _, named let preserves _, and let without a name discards the result.
- Run Step execution calls accept_child followed by _execute_child_binding.
  Child runs inherit the root identity, thread, ceilings, limits, settings, and
  shared root accounting; they already have durable Run identities.
- The executor has process-local awaitable handles for roots, and the public
  RunClient returns handles with wait(). Neither is a language value or suitable
  for persisting in a Flow local.
- No Future is a built-in value type; authored type syntax supports names and []
  suffixes. Current retry reconstructs committed locals from Step outputs.
  Execution records already support run and value Step kinds.

## Syntax and Binding Rules

```too
let research = async run investigate
async run review_risks

run draft

let evidence = await research
await research

run finish
```

| Form | Wait for child completion? | Binding |
| --- | --- | --- |
| `run R` | Yes | Complete output to _ |
| `let value = run R` | Yes | Complete output to value |
| `let run R` | Yes | Discard output |
| `async run R` | No | No binding; preserve all caller locals |
| `let job = async run R` | No | Future to job only |
| `let async run R` | No | Explicitly discard future; same as bare async |
| `await job` | Yes | Replace job with its complete result |
| `let value = await job` | Yes | Result to value; retain job if names differ |
| `let await job` | Yes | Discard result; retain job |

Run accepts its existing named and inline targets; inline syntax is
`async run [-> T]: BODY`, with Text as the default and no using.
No fut/future declarations or alternate async placement.

Await takes exactly one local identifier, including _. It is not a block,
arbitrary expression, array-of-handles operation, or implicit await on argument
use. Bare await's destination is its operand, rather than the normal _ default.
Explicit let destination wins; `let job = await job` matches bare await job.
Only a successful wait writes the destination; failure preserves all bindings.

The operand must contain a Future even if its work has already completed.
Awaiting a retained future again returns the same result without launching work.
After in-place awaiting, the local is T; another await of that local is a type
error. Background completion never changes caller locals by itself.

## Launch, Ownership, and Failure

- Validate and bind target/input at launch, using ordinary run's live-resolution,
  contract, ancestry, and authorization rules. Capture inputs, named locals,
  applicable iteration history, and relevant execution context at this boundary.
  Later local changes do not affect the invocation. Await does not resolve the
  runnable again or launch it lazily.
- Return only after the child has been durably accepted and registered for
  execution. No guarantee that a provider call starts before the next statement.
  Preflight/admission failure fails the launch statement and publishes no future.
- Children belong to the immediate launching run, remain inside its root tree,
  and share that root's budgets and cancellation. No new global lane pool or
  separate accounting is introduced. Serialize shared accounting/event updates
  correctly while parent and children run concurrently.
- On normal parent return, cancel all unfinished owned children and await their
  cleanup before marking the parent terminal. Do not wait for their successful
  business results. On parent failure/cancellation, cancel and drain them too.
  Successful exec handoff closes the outgoing execution segment's async children
  before replacement execution. Independent lifetime is reserved for spawn.
- Overwriting or discarding a future does not cancel its work or remove ownership.
  Repeat iterations capture separate launch inputs; iteration exit alone does
  not end the owner run or cancel prior iteration children.
- Child failure is recorded immediately and surfaces at await, without replacing
  locals. An unawaited child's failure does not fail an otherwise successful
  parent; retrieve its exception and preserve ordinary failure inspection.
  Shared-root budget exhaustion and parent cancellation still affect the tree.
- Canceling a waiting parent triggers normal child cleanup. Propagate a child's
  failed/canceled terminal outcome as a failed await with the child error/status
  reference; do not silently return null or mark an unrelated parent canceled.
  External side effects are not rolled back.

## Future Value and Durable Execution

Use a language-owned Future value with immutable target run ID and expected
complete result type. Future<T> is explanatory/static notation, not authored
generic syntax. Add the boxed stored form `{"?":"Future!","!":{"run":ID,
"result_type":T}}`; public locals use type Future with the inner payload.
Static checking carries T separately. Reserve Future as a built-in name. The
boxed tag distinguishes new handles from historical structs named Future;
preserve those historical reads, but require such authored structs to be renamed.

Keep the pure value vocabulary in lang; execution validates run references and
loads outputs. No asyncio.Task, executor, store, mutable status, or result cache
is serialized in the value. The target's accepted contract/state and final
record provide authoritative output validation and provenance. A stored handle
must not resolve to a missing or contract-incompatible run.

T is the complete declared output: Future<Text[]> resolves to Text[], not to
one element. Known future values cannot be used as Text/Json/array inputs, prompt
content, tools' data, or ordinary Flow outputs. Await them first. Authored Future
parameters/fields and containers of handles are outside this group; diagnostics
must not silently stringify, await, or coerce them.

- Add an asynchronous flag to RunStmt, defaulting false for old records, and an
  AwaitStmt with an operand local and resolved output binding.
- An async launch is a run-kind Step whose committed output is the Future,
  including binding=None for discarded handles. Its success means acceptance,
  not child completion. The child Run retains its ordinary parent Step relation
  and its own terminal status. Inspector/progress must show these separately.
- Persist child acceptance and the launch receipt together; cancellation or
  failure between receipt and event delivery must not produce an untracked child
  or duplicate acceptance. Reuse the existing durable admission-receipt pattern.
- Await is a value-kind Step. Record the awaited target and successful output
  reference/binding; preserve the child's full value/provenance.
- Retry restores committed launch futures and await results, including within
  repeats, without replaying launches or applying child locals to the parent.
  A retained failed/canceled future stays tied to that outcome; retry does not
  implicitly rerun it. A fresh whole-run rerun creates fresh launches.
- A live target owned by the current executor can be awaited through its registry.
  A terminal target can be read from records. A nonterminal target with no local
  owner fails promptly as unavailable; do not invent a terminal status, hang,
  or recreate work from its handle. Missing/pruned targets fail explicitly.
  No cross-process resumption or new owner-loss recovery service.

## Implementation Touchpoints

- `src/toolang/lang/{ast,lower,types,flow_validation,format,description}.py` and
  the grammar/CST/highlighter: RunStmt modifier, AwaitStmt, target-dependent
  binding, Future vocabulary, sequential type transitions, and diagnostics.
- `src/toolang/execution/executor/{executor,common,limits}.py`,
  `runs/flow.py`, `stmts/{run,exec}.py`, new `stmts/await_value.py`, and
  Step helpers: split admission from completion; owner/task registry and cleanup.
- `src/toolang/execution/{types,records,store,events,schemas}.py`: Future codec,
  launch receipts, status/provenance projection, and retry restoration.
- Execution inspection/progress, prepared caches, and focused language, execution,
  record, retry, cancellation, and offline concurrency tests. Keep parsing and
  host/CLI defaults out of the core runtime.

Publish/pin the required grammar release and update the lockfile. Reserve async
and await statement starts with useful diagnostics. Reject unsupported async
targets and block syntax until group 3 implements it; no implicit prompt fallback.
Keep existing historical records readable. Document the new Future representation
and generate the implementation changelog entry through the repository runnable.

## Acceptance Tests

1. Parse/check/format every table form, named/inline run, annotations, and repeat
   nesting. Check exact destination behavior, including _, self-binding, and
   discarded awaits; reject malformed/unsupported forms and non-Future operands.
2. Use deterministic gates to prove launch returns before completion, inputs and
   history stay captured, and siblings/parent overlap without background mutation.
3. Await scalar, array, empty array, nested, struct, and Part outputs with exact
   types/provenance. A retained future is reusable; an in-place resolved local
   is no longer awaitable. Reject implicit awaiting/coercion at data boundaries.
4. Normal parent exit, failure, cancellation, exec handoff, and executor shutdown
   cancel/drain owned unfinished children. Discarded/overwritten handles do not
   leak tasks; unawaited child failures remain inspectable without failing parents.
5. Verify launch failure versus accepted-child failure, canceled targets, shared
   root limits, event ordering, and that no binding changes on failed await.
6. Round-trip futures/receipts; inject admission/event-delivery faults. Retry
   restores original targets and bindings without duplicate side effects; test
   successful/failed/canceled/missing targets, repeat prefixes, and owner loss.
7. Check documentation/examples and links; implementation runs all default
   repository verification with offline deterministic concurrency tests.

## Risks and Approval

Key risks are leaked children, admission/completion confusion, race-dependent
local mutation, duplicate launches on retry, and implicit conversion of futures.
The specified receipts, owner cleanup, explicit await, and acceptance cases
address these. No remaining implementation choice requires a product decision;
the complete proposed definition still needs human approval. Documentation-only
checks do not claim this syntax or lifecycle is implemented.
