# Draft multi-target await and async operations

Status: Deferred draft. Preserve these considerations without further design or
implementation work while phase one in
[#685](https://github.com/openhat-ai/toolang/pull/685) is implemented. This is not
an approved, decision-complete implementation definition. Examples are proposed.

## Scope and Mandatory Invariants

Phase two contains multiple-target await, native handle arrays, async/await blocks,
and direct async collection operations. Preserve phase one's shared rules: one
`_Awaitable` type; complete results; launch separate from wait; ownership separate
from observation; linear caller Steps; and unified run/exec/async run/spawn/await
presentation. Reuse and extend their acceptance tests in both phases.

Run and operation are variants of one handle. An operation may admit children in
batches as lanes become available, so its identity cannot be a preallocated Run
array. `Awaitable<T>[]` is a handle collection; `Awaitable<T[]>` is one handle yielding
a complete array. Do not reintroduce `_Run` or encode results in the type name.

## Multiple-Target Await

```too
let results = await ha, hb
```

Wait for every target and return complete results in operand order, independent
of completion order. This is one blocking Step. Two `Text[]` results produce
`Text[][]`, without flattening. Homogeneous contracts produce `T[]`; mixed/unknown
contracts produce `Json[]` with per-item provenance. Null is a result; absent
output cannot supply a required result slot. Publish no partial binding.

Apply existing named/default/discarded binding rules. Preserve operand handles
unless the destination explicitly replaces one. Repeated/duplicate operands observe
existing work. Failure stops remaining waiters, not their targets; owner cleanup is
separate. Extend `_toolang/await(target=REF)` to accept an ordered reference array.

Native handle arrays need a trusted `_Awaitable[]` container and codec, not Json
lookalikes. `await handles` follows the collection rule; empty arrays retain a known
element result contract, otherwise `Json[]`. Construction syntax remains a draft
question, not a phase-one dependency.

## Async and Await Blocks

```too
let h = async:
  run review_accuracy
  run review_risks
  generate 3 using suggest_improvements
run draft
let reviews = await h
```

```too
let reviews = await:
  run review_accuracy
  run review_risks
  generate 3 using suggest_improvements
```

Both forms use one parallel-group scheduler. `async:` returns one operation handle
after admission; `await:` starts and waits in one visible blocking Step. No
`async await:` form is needed. The result is
`[accuracy_result, risk_result, [idea1, idea2, idea3]]` in declaration order.

Current design considerations:

- Support named/default/discarded bindings and optional `in P lanes`. Branches
  share the entry snapshot but have isolated locals, even with one lane. This is
  a parallel group, not a sequential background flow.
- Allow unbound run, generate, map, reduce, keep, drop, sort, nested await blocks,
  and handle waits. Reject direct local bindings, async launches/blocks, repeat,
  exec, and spawn as immediate branches. Put multi-step work in helper flows.
- Require at least one branch and one complete result per branch. Preserve null
  and nested/empty arrays without flattening. Preflight known contracts.
- Limit active direct branches using explicit lanes or the enclosing flow's lanes.
  Nested operations retain separate limits; do not hold a shared permit across
  nested work that needs it. No global leaf-task cap.
- The launching Run owns an async group; the group owns work it starts. Failure
  cancels/drains unfinished owned branches. Wait-only branches observe existing
  targets without adopting them, including independent spawned roots.
- Give the group a durable operation identity/scope before all child Runs exist.
  Background events never keep launch live or move beneath await. Retry restores
  only outer bindings; costs occur once.

## Direct Async Operations and Deferred Decisions

Direct async map/generate/reduce/keep/drop/sort would return one operation handle
for the complete result; internal lanes are independent of whether the caller waits.
Their supported forms and rollout need a later definition. Direct async repeat has
no agreed isolation/result contract; use async run on a helper flow. Never merge
background locals implicitly.

Before implementing phase two, settle handle-array construction, operation records/
codecs and recovery, supported direct-async forms, and final branch restrictions.
Extend phase one's resolver/target-kind boundary instead of overloading Run IDs.
Agic can call flows containing these operations; no generic block-evaluation or
operator-specific runtime tool is proposed.

Likely touchpoints: grammar; `src/toolang/lang/`; execution statement/group dispatch;
records/codec/resolver; progress projections; and offline language/execution/retry
tests. Future acceptance must cover operand/branch order, whole nested results,
lane-delayed admission, isolation, ownership-aware cancellation, recovery, and all
shared rules. Risks include flattening, branch-binding leaks, nested-lane deadlocks,
and cancellation crossing ownership boundaries. No product or changelog changes
belong to this draft.
