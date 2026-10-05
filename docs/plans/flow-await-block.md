# Define Await Blocks

Status: Proposed; feature definition, group 3 of 4. The human selected await:
without all, with one complete child output per ordered array item. The complete
definition remains subject to human approval. This PR contains no implementation.

## Goal, Scope, and Dependencies

Start independent child operations, wait for their results, and return one array
without flattening. Ordinary Flow and repeat bodies remain sequential. The await
block is a value operator; it does not add par/seq control constructs.

Implement after [group 1](https://github.com/openhat-ai/toolang/pull/684)'s
array/target rules and [group 2](https://github.com/openhat-ai/toolang/pull/685)'s
handle-await grammar.
The block scheduler itself uses the existing parallel Step machinery, without
creating user-visible futures for its branches. Group 2 supplies the existing
handle form when a child explicitly awaits previously started work. Spawn is
not required. These are separate documentation and implementation changes.

## Verified Current Behavior

- Flow dispatch awaits each statement in order. Map/storm already use an internal
  par Step and preserve input order despite concurrent completion.
- Parallel child execution captures inputs, limits lanes, cancels/drains failing
  groups, and preserves complete child output types and references.
- Current grammar 0.3.4 rejects bare, named, and discarded await blocks.
- Retry restores top-level statement outputs, but repeat restoration walks
  descendants. A new block must not leak branch-local bindings during restoration.

## Syntax and Result Binding

```too
flow review(_: Text) -> Text:
  let reviews = await:
    run review_accuracy
    run review_risks
    generate 3 using suggest_improvements
  run: Summarize {{reviews}} for {{_}}.
```

The reviews array is [accuracy_result, risk_result, [idea1, idea2, idea3]].
The original primary input remains in _ because the block has a named binding.

```text
await [in P lanes]:
  VALUE_STMTS

let results = await [in P lanes]:
  VALUE_STMTS

let await [in P lanes]:
  VALUE_STMTS
```

Bare await: writes the complete array to _. Named let writes only that local.
Discarded let waits and drops the array. Publish a binding only after the whole
block succeeds. No all qualifier, spread/collect alias, or implicit flattening.

Block await starts child statements and joins them. In contrast, await handle
waits for existing work and replaces its operand local according to group 2.
The parser must distinguish these forms; block result binding follows ordinary
value-statement rules, while handle-await has its own default destination.

## Children, Inputs, and Types

- Require at least one immediate child. Allow unbound run, generate, map, reduce,
  keep, drop, sort, nested await blocks, and group 2's await-handle statement.
  Each contributes its complete result, irrespective of its local binding inside
  the branch. Reject direct let bindings, async launches, repeat, exec, and spawn.
  Multi-step sequences/control flow belong in helper Flows called by run.
- Children use isolated copies of the same entry locals, history, and context.
  A sibling result never becomes another sibling's input, even with one lane.
  Preflight known target/input/output contracts before starting new child work.
  Unknown external contracts retain checks at their ordinary call boundaries.
  The block itself needs no primary input; its children determine requirements.
- Calls in the block need no async prefix. A run child makes one ordinary call;
  generate/map retain their own calls and arrays. Nested blocks are one child
  result each. An exec inside a called Flow affects only that called Run.
- A direct await handle child resolves the captured future in its private local
  copy and contributes the completed value. The outer handle remains a future.
  It does not launch the target again or adopt ownership of that existing run.
- Collect outputs in declaration order, not completion order. Arrays stay nested;
  an empty child array contributes one empty-array item. Null is an item, not
  missing output. The outer length equals the immediate child count.
- Determine output from contracts: homogeneous child type T gives T[]; differing
  or unknown types give Json[], retaining typed values and provenance. Do not
  add tuple/union types or infer a narrower type from observed runtime values.
  A handle-await child contributes its future's result type T.

## Scheduling, Failure, and Persistence

Use explicit P or the enclosing Flow's effective lanes limit for active direct
children. Nested map/generate/await operations retain separate lane limits;
this is not a global leaf-task cap. Start ready branches in source order without
promising completion order. Avoid deadlock through nested shared semaphores.

All children must succeed before publishing the array. On failure, cancel and
drain unfinished branch tasks and newly launched runs owned by the block; retain
completed/failed records and publish no partial binding. Preserve ordinary parent
cancellation and limit behavior. File/tool side effects are not rolled back.

Canceling a branch that merely awaits a preexisting future stops that wait; it
does not cancel the referenced run just because another branch failed. The
original owner's lifecycle still applies: group 2 cancels owned async children
when their parent ends, while group 4's independent roots remain independent.

Persist one par-kind Step with source-ordered child paths. Keep AwaitBlockStmt
separate from AwaitStmt. The block output is a complete array of typed output
references; child Steps retain their own results and private bindings.

Restore a successful block only from its outer output and binding, including
inside repeat. Never replay child bindings into enclosing locals. Failed blocks
retain ordinary retry policy; there is no partial-success cache or new selective
branch retry mode. Preserve old record kinds and historical decoding.

## Implementation Touchpoints

- Upstream grammar plus `src/toolang/lang/{ast,lower,contracts,flow_validation,
  format,description}.py`: AwaitBlockStmt, child restrictions, lane clauses,
  output contract inference, distinct handle/block binding rules, and diagnostics.
- `src/toolang/execution/executor/stmts/await_block.py`, `steps/par.py`,
  `runs/flow.py`, `executor.py`, and `iteration.py`: isolated branch locals,
  concurrency, ownership-sensitive cleanup, ordered result collection, and retry.
- `src/toolang/execution/{types,records,schemas}.py`: statement decoding and
  par-kind compatibility, whole-array references, inspection/progress output.
- Language/format/highlight and execution/retry/record tests; current Flow syntax
  and examples. Publish/pin the grammar and invalidate affected prepared caches.

Generate the implementation's user-facing changelog entry through the repository
runnable. This documentation PR requires no release entry or product changes.

## Acceptance Tests

1. Parse/check/format bare, named, discarded, typed-child, lane-limited, nested,
   and repeat-contained blocks. Distinguish await handle; reject empty blocks,
   unsupported child forms, aliases, and missing known inputs before new calls.
2. Use deterministic gates to show overlap and source-order results even when
   completion order differs. Verify lane counts, nested limits, and no deadlock.
3. Every child reads the entry snapshot, including with one lane. Named/discarded
   outer results preserve _. No child local mutation escapes.
4. Preserve one item per child across scalar, null, empty/nested arrays, structs,
   Parts, homogeneous and heterogeneous types, and unknown contracts. Feed the
   final array directly into run/map/reduce.
5. Mix new calls with awaits of existing futures. Collect completed values,
   retain the outer futures, and never duplicate their launches. On block failure
   cancel block-owned work but preserve independently owned future targets.
6. Failures and parent cancellation drain branch tasks, retain inspection facts,
   avoid partial binding, and leave already-performed side effects observable.
7. Round-trip output refs and statement records. Retry a successful prefix and
   repeat-contained blocks, restoring only their outer bindings; failed blocks
   never leak partial arrays or branch-local bindings.
8. Check examples/links and run all default repository checks for implementation;
   default concurrency tests remain offline and deterministic.

## Risks and Approval

Risks are confusing handle-await with block-await, flattening child arrays,
branch-local bindings escaping, nested-lane deadlocks, and cancellation crossing
ownership boundaries. The syntax and acceptance cases specify these boundaries.

Out of scope: authored par/seq, named result objects, destructuring, inline
multi-statement branches, async modifiers on blocks, and first-success/partial
results. No unresolved product decision is needed for this group's implementation.
Approval of the complete definition and a published matching grammar are required.
