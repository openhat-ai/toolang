# Unify Flow Calls and Array Operators

Status: Proposed; definition only. The human confirmed an operator that collects
one complete child output per array item, and distinguished it from control
constructs such as repeat, par, and a possible seq. Use `spread` for this
operator and `fut` for the related asynchronous-binding direction discussed
below. Approval of these spellings does not approve the complete definition.

## Goal and Success Criteria

Use `run` for every ordinary single-runnable call. Remove authored `scatter` and
`gather`, and remove the independent Flow `item`/`list` shape and persisted
`Local.dim`. Collection operations inspect only the outermost array. Rename
`storm` to `produce` and `settle` to `reduce`; add a multi-child collection
operator without changing ordinary local-binding rules.

Success means array inputs and array results work identically regardless of
which statement or runnable produced them. Nested arrays remain nested, and
calling a helper Flow does not prevent subsequent collection operations.
Different runnable results form ordered arrays that can feed subsequent
run/map/reduce operations without repackaging through model calls.

This definition supersedes the affected shape and scatter/gather rules in
[Flow Usability](flow-usability.md) and [Flat Call Input](flat-call-input.md).
Their unrelated decisions remain unchanged.

## Verified Current Behavior

- `stmts/run.py` and `stmts/scatter.py` invoke the same Run Step executor.
  `operation_transform` marks scatter output as a list and run output as an
  item; scatter additionally requires an array output contract.
- Gather invokes that same executor, requires primary input, and rejects empty
  lists before invoking its child. Its result is marked as an item.
- Source checks reject both `run -> Text[]: ...` followed by `map` and a
  `flow(_: Text[])` beginning with `map`: both arrays have item shape.
- Runtime locals store an element type for list shape and a full value type
  for item shape. Durable and public locals carry a separate `dim` flag.
- Map and storm already preserve array-valued child results as nested arrays.
  Settle rejects empty input both with and without `from`.
- Flow statements execute sequentially. Named result bindings preserve `_`;
  named runnable parameters bind from same-named locals.
- The installed grammar is 0.3.4: `spread:` is rejected, while unrecognized
  produce/generate/reduce-like text can be lowered as an implicit run. New
  keywords require a grammar release, not executor-only dispatch changes.

These facts were checked against the implementation and offline parser/result
transformation probes; no model or live-provider calls were used.

## Semantics

| Operation | Calls and input | Result |
| --- | --- | --- |
| `run R` | One call; pass the complete current value if R consumes `_` | R's declared output, unchanged by the statement |
| `produce N using R` | N calls with the same complete input | One outer array of N results |
| `map using R` | One call per outer element, bound to `_` | One outer array of results, in input order |
| `keep` / `drop` | Select outer elements by position or Boolean predicate | Selected elements in original order |
| `sort` | One Number score per outer element | A stable ordering of those elements |
| `reduce` | Sequential reduction of outer elements | The reducer's declared output |
| `spread` | Independent child statements read one entry snapshot | One outer array, one item per child in source order |

1. Run accepts scalar or array inputs and outputs according to the existing
   runnable contract. It neither iterates nor wraps, unwraps, or flattens values.
   A runnable without primary input remains callable. Inline run still defaults
   to `Text`; array production requires an explicit output such as `Text[]`.
2. Map/keep/drop/sort/reduce require an actual outer array, independently of its
   producer. Typed `T[]` supplies elements of type `T`; `T[][]` supplies `T[]`.
   JSON arrays are also accepted: an untyped JSON array supplies `Json` elements.
   Strings, objects, structs, null, and absent input are not arrays. Do not parse
   JSON-looking text or traverse an object's array fields implicitly.
3. If a map or produce child returns `U`, the result is `U[]`. Thus a child
   returning `Text[]` produces `Text[][]`, including for zero calls. Selection
   and sorting preserve the source type (`T[]`, or `Json` for an open JSON
   array), nested values, and provenance. Scalar-to-array coercion is not added.
4. Empty map/keep/drop/sort inputs produce empty arrays with zero child calls;
   ordinary preflight checks remain. `produce 0` also makes zero calls. Run may
   pass an empty array to its child and still calls once. Reduce keeps settle's
   current nonempty-input requirement, including when `from` is supplied.
5. Reduce retains settle's seed and history contracts: no `from` means the first
   element is the seed, reducer output equals the element type, and N elements
   need N-1 calls. With `from`, the seed is coerced to reducer output type and
   N elements need N calls. An array accumulator remains a complete value.
6. `Part[]` follows the same array rule. A collection operation visits individual
   Parts; ordinary run passes the whole content value. Keep content rendering
   and multimodal transport based on value/type, without an item-shape exception.
7. Existing bindings, discarded results, lane limits, cancellation, error
   boundaries, predicate/scorer contracts, and iteration history keep their rules.
   A nested array can be processed further by explicitly mapping inside a child
   Flow; there is no automatic recursive map or flatten operation.

For example, the proposed semantics permit this existing syntax:

```too
flow summarize(_: Text) -> Text:
  run -> Text[]: Split {{_}} into topics.
  map using -> Text: Explain {{_}}.
  keep first 3
  run: Summarize {{_}}.
```

For `[["a", "b"], ["c"]]`, map invokes its child twice with `["a", "b"]`
and `["c"]`; `keep first 1` returns `[["a", "b"]]`.

## Multi-child Collection Operator

Flow and repeat bodies remain sequential by default. Existing repeat and possible
par/seq control constructs describe execution organization; value operators
such as produce, map, reduce, and the proposed spread describe value production
or transformation. A statement block can supply an operator's children without
making that operator a control construct. New par/seq control syntax is outside
this definition.

A named Flow is already a runnable, so `produce N using pipeline` and
`map using pipeline` repeat a multi-step sequence without extending `using` to
accept multiple targets. Use `spread` for independent children with different
runnables, counts, and output types:

```too
flow review(_: Text) -> Text:
  spread:
    run review_accuracy
    run review_risks
  run summarize_reviews
```

Both reviewers consume the original `Text` and return `Text`.
`summarize_reviews(_: Text[]) -> Text` receives their results as one array.
Use `let reviews = spread:` when the next runnable needs both the original `_`
and a named `reviews: Text[]` parameter. Spread does not export child locals or
assemble named fields into an object.

- Syntax is `spread [in P lanes]: STMTS`, with at least one child. Each immediate
  statement contributes exactly one output. First-version children are unbound
  value statements, including produce/map/reduce and nested spread. Reject direct
  `let`, `repeat`, and `exec` children: named/discarded child bindings would hide
  the one-item-per-child contract, and control statements have no return value.
  Put multi-step sequences or repeats in helper Flows and call them with `run`.
  Exec inside a called child Flow still affects only that child Run.
- All children read the same entry locals and iteration history, including `_`,
  using isolated local tables. A sibling's result never becomes another child's
  input, even with one lane. Collect itself does not require `_`; each child keeps
  its own input contract. Preflight every known input and operation contract
  against that snapshot before children start; unknown remote contracts retain
  their existing checks at the child boundary.
- Collect one complete output per child in source order, never completion order.
  Preserve arrays as individual items: two children returning `Text[]` produce
  `Text[][]`; an empty child array still contributes one empty-array item. A
  child returning null contributes null, not a missing result.
- Determine the array type from output contracts before execution. If all child
  types are the same T, the result is `T[]`; otherwise use `Json[]`, preserving
  each child's typed value and provenance. An unknown child output also selects
  `Json[]`. Do not infer a different result type from observed values or add
  tuple/union types. The outer count always equals the number of children.
- Spread is a value statement: bare spread writes `_`; `let results = spread:`
  writes only `results`; `let spread:` discards the whole array. Bind only after
  all children succeed. No child variable is exported to the enclosing Flow.
- Limit active direct children with P, or inherit the Flow's `lanes`. Nested
  spread/produce/map operations retain their own lane limits; this is not a global
  leaf-call limit. Start ready children in source order, but do not promise
  completion order. Use separate scheduling scopes to avoid nested-lane deadlock.
- A failure cancels unfinished siblings and awaits their cleanup; fail the spread
  Step without binding a partial array. Parent cancellation cancels the whole
  block. Completed child records remain inspectable. Failed result collection
  does not roll back file or tool side effects.
- Persist one existing-kind `par` Step with child statement paths assigned in
  source order. This internal execution category already serves map and storm;
  it does not introduce an authored par control construct. Its output is an array
  of typed refs to child outputs using existing array codecs; child Steps retain
  their outputs and local bindings.
  Restore a committed spread prefix from its whole-array output and outer binding,
  never by applying child bindings to enclosing locals. Apply the same rule
  inside repeat. Failed blocks retain the existing retry policy; add no
  partial-success retry mode.

## Value Model and Validation

- Remove runtime/static `shape` and durable/public `dim`; do not replace them
  with another independently writable collection flag. Runtime `type_name`
  always describes the complete value. Derive element types only when selecting
  an outer array element.
- Represent missing locals and statement-without-output separately from valid
  values. Use absence or a private missing-value sentinel, never JSON null.
  Repeat remains a control statement with no result; spread produces an array.
- Static checks accept known array types and reject known non-arrays or missing
  input. Unknown types and open `Json` values defer array checks to execution.
  Repeat joins widen differing type/length facts without a shape lattice.
- Check dynamic array requirements within the operation's Step before any child
  call. Diagnostics name the operation and expected array, not shape state.
- Preserve full array types and selected-element pointers through inline
  captures, named calls, Flow inputs/outputs, exec, seek results, and resumed
  execution. Preserve existing typed-boundary coercion rules.
- Collection progress counts outer elements. Output presentation derives array
  summaries from value/type; pointer-backed values may report unknown length.
  `Part[]` continues to render as content where the surface renders content.

## Syntax, Records, and Migration

Use `produce` and `reduce` as canonical keywords; do not add generate/gen aliases.
Produce retains storm's count, lane clauses, inline output default, and runtime
behavior; reduce retains settle's clauses and reducer contract.

Remove executable scatter/gather/storm/settle forms with migration diagnostics,
without deprecated execution aliases. Adopt a published Tree-sitter release
that defines produce, reduce, and spread statement nodes. Preserve recognizable
legacy CST forms only for precise rejection, never as implicit prompt text.
Source checking, formatting, help, highlighting, and execution must agree,
including nested and bound forms. Literal prose with a newly reserved statement
header must use explicit `run:`. Pin the released grammar and lock its artifacts
when available; upstream grammar publication is an implementation prerequisite.

| Old source | Replacement |
| --- | --- |
| `scatter using expand` | `run expand` |
| `scatter: BODY` / `scatter using: BODY` | `run -> Text[]: BODY` |
| `scatter [using] -> T[]: BODY` | `run -> T[]: BODY` |
| `gather using merge` | `run merge` |
| `gather using [-> T]: BODY` | `run [-> T]: BODY` |
| `storm N ...` | `produce N ...` with the same clauses |
| `settle ...` | `reduce ...` with the same `from` and runnable clauses |

Keep named/discarded bindings and explicit output types when rewriting. An
inline scatter's implicit `Text[]` must become explicit. A former gather may
now call its child on `[]`; migrate any application-specific nonempty check into
its runnable. There is no separate nonempty constraint on ordinary run.

New public locals contain `type` and `value`; new stored locals contain the
existing self-describing `value` only. Output retains its `local` and `binding`
wrapper. Existing value codecs, refs, and array nesting do not change.

- Read legacy stored locals with valid `dim=0/1`, validate their old structural
  invariants, and ignore dim for current value semantics. Write only the new
  representation. Do not bulk-rewrite historical records.
- Keep legacy scatter/gather/storm/settle Step descriptions decodable for
  inspection, including the already-supported historical scatter count field.
  Compatibility decoding must not make these statements executable. Keep
  parsing in `lang` and record compatibility in execution-owned codecs.
- Invalidate prepared-program caches affected by the language change. Reject
  execution/retry/rerun of snapshots containing removed statements before child
  calls, with an instruction to migrate and submit a new run. Historical
  inspection and resolution of existing value pointers remain available.
- Public API consumers must stop requiring or sending `dim`; queries or refs
  to `Local.dim` are removed. Old clients must upgrade alongside the runtime.

The implementation PR updates current docs and tracked examples, and generates
the breaking-change/migration entry through `too aide.too update_changelog`.
This definition PR changes no behavior and needs no release entry.

## Related Direction: Asynchronous Bindings

Use `fut` as the short asynchronous-binding keyword paired with `let`.
The following syntax records the design direction; root spawning, future
values, asynchronous scheduling, and awaiting are outside this array-operator
implementation scope and require a separate complete definition.

```too
fut research_result = spawn research
fut ideas_result = run brainstorm

run prepare_outline

let research = await research_result
let ideas = await ideas_result

run write_article
```

- `fut result = run R` starts a child operation immediately and binds its future.
  `let result = run R` waits and binds the completed result.
- `spawn R` starts an independent root and returns its handle immediately.
  `fut result = spawn R` binds that completion handle without nesting futures.
- Capture inputs at launch. Waiting is explicit: `await result` writes the
  completed output to `_`; `let value = await result` binds only `value`.
  The original future remains reusable; waiting again does not execute again.
- The same binding syntax may extend to whole produce/map/spread operations
  while preserving their result and internal scheduling contracts.

The separate definition must resolve unawaited child lifetimes, cancellation
and failure propagation, durable handles and retry, root context and limits,
and whether to accept the long `future` spelling as an alias. This section
does not add those features to this plan's acceptance tests.

## Implementation Touchpoints

- `src/toolang/lang/{ast,lower,contracts,flow_validation,format,description}.py`:
  renamed and new statements, spread output contracts, legacy decoding, type checks.
- The upstream grammar's statement nodes/queries/corpus, followed by this
  repository's `pyproject.toml`, `uv.lock`, CST/formatter/highlighter integration:
  published syntax support; no alternate handwritten parser.
- `src/toolang/execution/executor/{common,executor,content,iteration}.py`,
  `runs/{flow,agic}.py`, and `stmts/`: complete-value locals, collection selection,
  call/result binding, content handling, and removal of scatter/gather handlers.
  Rename storm/settle owners to produce/reduce; add `stmts/spread.py` using the
  existing internal par Step boundary and child-local execution, collecting
  outputs in source order.
- `src/toolang/execution/{types,records,schemas,store}.py`: Local codecs,
  reference projections, historical records, resolved outputs, and SpreadStmt as
  an existing-kind par Step. Restore only spread's outer binding during Flow retry.
- `src/toolang/state/cache.py` and execution snapshot entry points: prevent stale
  validated programs from bypassing the new source rules.
- `src/toolang/cli/common/execution_progress/formatting.py`: summaries without
  dim. Update other consumers found through shape/dim and statement references.
- Language, execution, record/provenance, state, API, and presentation tests;
  `docs/{flow-syntax,program,concepts,run-step-records,api,execution-presentation}.md`,
  tracked `.too` examples, and `CHANGELOG.md` during implementation.

## Acceptance Tests

1. Parse/check/format valid run replacements. Reject every removed source form
   with a useful location and replacement, including bound/discarded/nested
   forms. Inline array producers retain their exact output type after migration.
2. A `flow(_: Text[])` can begin with map/keep/drop/sort/reduce. Array results
   from run, helper Flows, and seek have the same collection behavior; exec
   preserves arrays across replacement input/output boundaries.
3. Run forwards a complete scalar, empty array, and nested array in one call;
   validate exact child input, output type, and call count.
4. Exercise empty/singleton/multiple outer arrays for all collection operations.
   Assert order, stable sorting ties, types, and call counts. Retain reduce's
   empty failure and N-1/N seed behavior.
5. Map over `Text[][]` passes one `Text[]` per call. Array-returning map/produce
   produces nested arrays, and subsequent keep/sort/map touches only the outer
   level. Include empty inner arrays and empty outer results.
6. Typed arrays, open JSON arrays, and `Part[]` are accepted. Scalars, JSON
   objects/null, JSON-looking strings, and missing input fail before child calls.
   JSON null remains distinct from missing output; Parts still render correctly.
7. Named/discarded results preserve `_`; repeat joins, inline captures, reducer
   history, lane ordering/cancellation, and errors retain their contracts.
8. New storage, public API, events, and schema projections omit dim. Round-trip
   nested/empty arrays and value pointers; restart/resume yields the same values.
   Legacy locals and removed-statement history remain inspectable, while legacy
   executable snapshots and stale caches cannot bypass rejection.
9. Produce and reduce match the former storm and settle contracts, including
   inline defaults, lane clauses, reducer seeds/history, and call counts.
   New grammar nodes and formatter round trips must not become implicit runs.
10. Spread returns one array item per child in source order, including child empty
    arrays and nulls. Test homogeneous `Text[]`/`Text[][]`, heterogeneous
    `Json[]`, structs/Parts, unknown output contracts, and preserved refs. Exercise
    bare/named/discarded spread, nested spread, spread inside repeat, multi-step
    child Flows, and direct consumption of the resulting array by map/reduce/run.
11. Reject empty spread, child `let` bindings, unsupported direct control children,
    and statically known missing inputs before calls; allow input-free children
    without `_`. Prove entry snapshot reads with one lane and out-of-order
    completion; prove overlap and lane limits with deterministic gates, including
    nested-operation limits.
12. A failed/canceled child publishes no partial array, cancels and
    drains siblings, preserves child records, and prevents downstream execution.
    Restoring a successful spread prefix restores exactly its outer binding and
    refs without replaying child bindings; failed prefixes leak no partial result.
13. Migrate and check tracked examples; validate current documentation links and
    syntax. Run all default repository checks for the implementation.

## Risks, Scope, and Open Questions

This is a breaking language and Local-protocol change. Main risks are lost array
levels or provenance during the type-model conversion, implicit inline output
defaults during migration, confusing content arrays with absent output, and
child results escaping before the whole spread array succeeds. Nested lane limits
can multiply concurrent calls and must be documented and tested.
The acceptance cases above cover those boundaries.

Out of scope: authored `par`/`seq` control constructs, implicit result objects or
destructuring, inline multi-statement spread branches, flattening, a new reduce
empty policy, automatic
source rewriting, global concurrency controls, a legacy execution engine, and
unrelated filesystem or presentation uses of the word shape/dim.

The proposed compatibility choice is direct source removal with historical
record reads retained. The collection operator's array result and separation
from control constructs follow the human's explicit direction. Remaining
semantics are proposed for review, not inferred approval.
Implementation requires approval of the complete definition and publication of
the matching upstream grammar; this PR changes no product behavior.
