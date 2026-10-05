# Unify Flow Calls and Array Operations

Status: Proposed; awaiting explicit human approval. Definition only.

## Goal and Success Criteria

Use `run` for every ordinary single-runnable call. Remove authored `scatter` and
`gather`, and remove the independent Flow `item`/`list` shape and persisted
`Local.dim`. Collection operations inspect only the outermost array.

Success means array inputs and array results work identically regardless of
which statement or runnable produced them. Nested arrays remain nested, and
calling a helper Flow does not prevent subsequent collection operations.

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

These facts were checked against the implementation and offline parser/result
transformation probes; no model or live-provider calls were used.

## Semantics

| Operation | Calls and input | Result |
| --- | --- | --- |
| `run R` | One call; pass the complete current value if R consumes `_` | R's declared output, unchanged by the statement |
| `storm N using R` | N calls with the same complete input | One outer array of N results |
| `map using R` | One call per outer element, bound to `_` | One outer array of results, in input order |
| `keep` / `drop` | Select outer elements by position or Boolean predicate | Selected elements in original order |
| `sort` | One Number score per outer element | A stable ordering of those elements |
| `settle` | Sequential reduction of outer elements | The reducer's declared output |

1. Run accepts scalar or array inputs and outputs according to the existing
   runnable contract. It neither iterates nor wraps, unwraps, or flattens values.
   A runnable without primary input remains callable. Inline run still defaults
   to `Text`; array production requires an explicit output such as `Text[]`.
2. Map/keep/drop/sort/settle require an actual outer array, independently of its
   producer. Typed `T[]` supplies elements of type `T`; `T[][]` supplies `T[]`.
   JSON arrays are also accepted: an untyped JSON array supplies `Json` elements.
   Strings, objects, structs, null, and absent input are not arrays. Do not parse
   JSON-looking text or traverse an object's array fields implicitly.
3. If a map or storm child returns `U`, the result is `U[]`. Thus a child
   returning `Text[]` produces `Text[][]`, including for zero calls. Selection
   and sorting preserve the source type (`T[]`, or `Json` for an open JSON
   array), nested values, and provenance. Scalar-to-array coercion is not added.
4. Empty map/keep/drop/sort inputs produce empty arrays with zero child calls;
   ordinary preflight checks remain. `storm 0` also makes zero calls. Run may
   pass an empty array to its child and still calls once. Settle keeps its
   current nonempty-input requirement, including when `from` is supplied.
5. Settle retains its seed and history contracts: no `from` means the first
   element is the seed, reducer output equals the element type, and N elements
   need N-1 calls. With `from`, the seed is coerced to reducer output type and
   N elements need N calls. An array accumulator remains a complete value.
6. `Part[]` follows the same array rule. A collection operation visits individual
   Parts; ordinary run passes the whole content value. Keep content rendering
   and multimodal transport based on value/type, without an item-shape exception.
7. Bindings, discarded results, lane limits, cancellation, error boundaries,
   predicate/scorer contracts, and iteration history keep their current rules.
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

## Value Model and Validation

- Remove runtime/static `shape` and durable/public `dim`; do not replace them
  with another independently writable collection flag. Runtime `type_name`
  always describes the complete value. Derive element types only when selecting
  an outer array element.
- Represent missing locals and statement-without-output separately from valid
  values. Use absence or a private missing-value sentinel, never JSON null.
  Repeat remains a control statement with no result.
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

Remove executable scatter/gather syntax directly, with migration diagnostics;
do not retain deprecated execution aliases. The currently pinned Tree-sitter
grammar may recognize their CST nodes so lowering can issue precise errors.
No external grammar release is required to reject these statements in Toolang.
Source checking, formatting, help, and execution must all reject removed forms,
including statements nested under `let` or `repeat`.

| Old source | Replacement |
| --- | --- |
| `scatter using expand` | `run expand` |
| `scatter: BODY` / `scatter using: BODY` | `run -> Text[]: BODY` |
| `scatter [using] -> T[]: BODY` | `run -> T[]: BODY` |
| `gather using merge` | `run merge` |
| `gather using [-> T]: BODY` | `run [-> T]: BODY` |

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
- Keep legacy scatter/gather Step descriptions decodable for historical
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

## Implementation Touchpoints

- `src/toolang/lang/{ast,lower,contracts,flow_validation,format,description}.py`:
  syntax rejection, active statement vocabulary, legacy decoding, type checks.
- `src/toolang/execution/executor/{common,executor,content,iteration}.py`,
  `runs/{flow,agic}.py`, and `stmts/`: complete-value locals, collection selection,
  call/result binding, content handling, and removal of scatter/gather handlers.
- `src/toolang/execution/{types,records,schemas,store}.py`: Local codecs,
  reference projections, historical records, and resolved outputs.
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
2. A `flow(_: Text[])` can begin with map/keep/drop/sort/settle. Array results
   from run, helper Flows, and seek have the same collection behavior; exec
   preserves arrays across replacement input/output boundaries.
3. Run forwards a complete scalar, empty array, and nested array in one call;
   validate exact child input, output type, and call count.
4. Exercise empty/singleton/multiple outer arrays for all collection operations.
   Assert order, stable sorting ties, types, and call counts. Retain settle's
   empty failure and N-1/N seed behavior.
5. Map over `Text[][]` passes one `Text[]` per call. Array-returning map/storm
   produces nested arrays, and subsequent keep/sort/map touches only the outer
   level. Include empty inner arrays and empty outer results.
6. Typed arrays, open JSON arrays, and `Part[]` are accepted. Scalars, JSON
   objects/null, JSON-looking strings, and missing input fail before child calls.
   JSON null remains distinct from missing output; Parts still render correctly.
7. Named/discarded results preserve `_`; repeat joins, inline captures, reducer
   history, lane ordering/cancellation, and errors retain their contracts.
8. New storage, public API, events, and schema projections omit dim. Round-trip
   nested/empty arrays and value pointers; restart/resume yields the same values.
   Legacy locals and scatter/gather history remain inspectable, while legacy
   executable snapshots and stale caches cannot bypass rejection.
9. Migrate and check tracked examples; validate current documentation links and
   syntax. Run all default repository checks for the implementation.

## Risks, Scope, and Open Questions

This is a breaking language and Local-protocol change. Main risks are lost array
levels or provenance during the type-model conversion, implicit inline output
defaults during migration, and confusing content arrays with absent output.
The acceptance cases above cover those boundaries.

Out of scope: new collection syntax, flattening, a new fold/settle empty policy,
automatic source rewriting, new concurrency controls, a legacy execution engine,
and changes to unrelated filesystem or presentation uses of the word shape/dim.

The proposed compatibility choice is direct source removal with historical
record reads retained. No implementation decisions remain open under that
choice; human approval of the definition is still required before implementation.
