# Define async and await blocks

Status: Proposed; revised feature definition. Implementation is pending and depends
on the shared awaitable contract and open decisions in
[#685](https://github.com/openhat-ai/toolang/pull/685). Human approval is still required.

## Goal and Scope

Provide one parallel group with two entry forms: `async:` returns its handle after
admission; `await:` waits immediately for its complete result array. Both forms use
the same branch execution, ordering, isolation, lanes, and failure rules. Ordinary
flow and repeat bodies remain sequential.

This is the second pending feature after async/await. It extends the original
await-block proposal with nonblocking group launch; it does not require
`async await:`. Existing [array semantics](./flow-array-semantics.md) and
[spawn ownership](./flow-spawn.md) remain unchanged. Proposed examples below are
not supported by today's CLI.

## Verified Current Behavior

Flow dispatch awaits statements sequentially. Existing parallel Steps preserve
input order, limit lanes, and cancel/drain failed groups. Retry reconstructs locals
from committed outputs and walks repeat descendants. Async/await blocks and
operation handles are not implemented; restoration must not leak branch bindings.

## Syntax and Binding

```too
let h = async:
  run review_accuracy
  run review_risks
  generate 3 using suggest_improvements
run draft
let reviews = await h
```

To start the same group and wait immediately:

```too
let reviews = await:
  run review_accuracy
  run review_risks
  generate 3 using suggest_improvements
```

Both produce `[accuracy_result, risk_result, [idea1, idea2, idea3]]` when successful.
The result order follows branch declarations. Async captures entry inputs before
returning; subsequent work cannot alter those inputs.

| Form | Binding and completion |
| --- | --- |
| `let h = async [in P lanes]: ...` | Bind one operation handle after admission |
| `async [in P lanes]: ...` / `let async [in P lanes]: ...` | Launch, discard handle, preserve locals |
| `await [in P lanes]: ...` | Wait for the group; bind its array to `_` |
| `let results = await [in P lanes]: ...` | Wait; bind only `results` |
| `let await [in P lanes]: ...` | Wait and discard the result |

`async:` starts the work without requiring a later await. Its result uses the
internal type `_Awaitable`, with the complete group result contract stored separately
(`Awaitable<Results>` in design notation), not an array of Run handles. `await:` is
the immediate-wait form of the same operation; it does not introduce a second
scheduler. Awaiting its retained handle follows #685, including repeated waits
and result bindings.
No `all` qualifier, implicit flattening, or `async await:` form is introduced.

## Branches and Results

- Require at least one immediate branch. Allow unbound run, generate, map, reduce,
  keep, drop, sort, nested await blocks, and single/multiple handle-await statements.
  Reject direct let bindings, async launches/blocks, repeat, exec, and spawn.
  Multi-step sequences and repeat belong in helper flows called by run.
- Every branch receives an isolated snapshot of the same entry locals, history,
  and context. A sibling never supplies another sibling's input, even with one
  lane. This is a parallel group, not a sequential background flow. Preflight
  known input/target/output contracts before starting new child work.
- Each branch contributes one complete result. Map/generate retain their arrays;
  nested blocks and multiple-target awaits each contribute one array item without
  flattening. Empty arrays and null are valid results; absent output fails collection.
- Infer results from contracts: homogeneous `T` gives `T[]`; differing/unknown
  contracts give `Json[]`, preserving typed item references and provenance. Do not
  infer types from completion order or add tuple/union types.
- Await branches observe captured Run or operation handles and preserve the outer
  handles. They neither relaunch their targets nor adopt ownership of them.

## Scheduling and Ownership

Use explicit `P` or the enclosing flow's effective lanes for active direct branches.
Nested collection operations/blocks retain their own limits; this is not a global
leaf-task cap. Admit ready branches in source order without promising completion
order. A group identity exists before all child Runs exist, including while branches
are queued. Do not hold a shared lane permit across nested work that needs it.

The launching Run owns an async group; the group owns work it starts. Reuse #685's
parent return/failure/cancel/exec cleanup. Discarding a handle does not detach work.
On branch failure, cancel and drain unfinished owned branches, retain their records,
and fail the group. Publish no partial result; external effects are not rolled back.
Background group failure is recorded immediately and surfaces to the caller at await.

Waiting on existing handles is different: `await ha, hb` does not create ownership.
Canceling a wait-only branch stops its waiter, not the referenced computation.
The original owner's lifecycle still applies. Failure in another branch must not
cancel an independently spawned root just because this group awaited it.

## Records, Runtime, and Recovery

Use one durable operation identity for the group's lifecycle, result contract, and
ordered output references. Reuse par Step machinery for branch execution and
source-ordered child paths inside the group's execution scope. The caller remains
linear: `async:` is one launch Step that ends when the group is started; an optional
later `await h` is one blocking Step. `await:` is one blocking caller Step for starting
and waiting on the group, without an additional visible launch Step.

Follow #685's scope separation. Background branch events belong to the group, not
live children displayed under the completed launch Step. Preserve provenance and
ownership outside live presentation state. Waiting neither replays branch events
nor relocates them under the wait Step, and costs are counted once. Use #685's
`_Awaitable` type for the group target; the payload codec and background execution
records remain to be settled, not the common internal type identifier.
Follow #685's invocation presentation conventions for action, target, status, and
secondary handle details. Label the operation from its syntax, not its handle type:
an async block is not displayed as spawn merely because both return `_Awaitable`.

AsyncBlockStmt and AwaitBlockStmt may lower to the same group operation with
different delivery modes; handle AwaitStmt only waits for existing work. Flow and
agic use the same target resolver/wait service. `_toolang/await` must accept an
operation target, not only a Run ID. Agic may invoke a flow containing a block;
this plan adds no generic block-evaluation or operator-specific runtime tool.

Retry restores a committed async launch from its handle and a successful group
from its outer output/binding. Never replay branch bindings into the enclosing
locals, including inside repeat. Failed groups retain ordinary retry policy; no
partial-success cache or selective branch-retry feature is added. Unavailable
handles follow #685's explicit failure rules rather than relaunching work.

## Implementation Touchpoints and Acceptance

- Grammar and `src/toolang/lang/{ast,lower,contracts,flow_validation,format,
  description}.py`: both block forms, branch restrictions, lanes, and result contracts.
- `src/toolang/execution/executor/stmts/`, `steps/par.py`, `runs/flow.py`,
  `executor.py`, and `iteration.py`: group scheduling, snapshots, cleanup, restoration.
- `src/toolang/execution/{types,records,events,schemas}.py`: operation identity,
  launch/completion records, output provenance, inspection, and shared await adapter.
- Language, execution, retry, and historical-record tests; current flow syntax docs
  and examples. Publish/pin matching grammar before implementation release.

Acceptance scenarios:

1. Parse/check/format both forms, bindings, lanes, nesting, and repeat-contained
   blocks. Reject empty blocks, prohibited branches, and known invalid inputs.
2. Gates prove async returns before completion, await blocks wait, both schedules
   overlap branches, and retained handles can be awaited repeatedly without relaunch.
3. Branches read entry snapshots even with one lane; parent/sibling mutation cannot
   change their inputs. Results follow source order across scalar, null, empty/nested
   array, struct, and Part values; missing output fails without a partial binding.
4. Lane limits and nested groups avoid deadlock. Groups do not require all child IDs
   at launch. Nested map/block/collection-await results each remain one complete item.
5. Failure/cancel/parent exit/exec drains owned work while observer cancellation
   preserves independently owned targets. Discarded handles do not leak tasks.
6. Round-trip admission, operation, and `_Awaitable` output records; the group result
   contract stays separate from the handle type. Retry restores only outer bindings,
   including in repeat. Cover missing/live/terminal targets and provenance.
7. With or without a later await, caller Steps remain linear and the async launch
   row ends at startup. Background branch events stay in the group scope; `await:`
   presents one blocking Step. No reopened launch rows or replayed progress/cost.
8. Validate links/examples and run all default checks for implementation, keeping
   concurrency tests offline and deterministic. Generate its changelog via the runnable.

## Open Decisions and Risks

The remaining blocker is #685's awaitable payload and background execution records;
the `_Awaitable` identifier and linear caller Step lifecycle are fixed.
This plan must not assume Run-only identities or independent branch Run handles.
Approval must cover both launch modes and their shared contract before implementation.

Risks are treating this block as a sequential flow, flattening results, leaking
branch locals, nested-lane deadlocks, and cancellation crossing ownership boundaries.
Out of scope: named result objects, destructuring, inline multi-statement branches,
direct async repeat, and first-success/partial results. This definition changes no
product behavior and requires no changelog entry.
