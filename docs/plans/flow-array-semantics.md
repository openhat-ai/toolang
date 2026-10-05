# Simplify Flow Calls and Array Operators

Status: Proposed; feature definition, group 1 of 4. The human confirmed this
group includes removing shape/dim, the final generate/reduce names, and the
using rule. The complete definition remains subject to human approval.

## Goal and Scope

Use one ordinary call statement, `run`, and make collection behavior depend on
the outermost array value. Remove scatter/gather, rename storm to generate and
settle to reduce, and give generate/map/reduce one runnable-target syntax.

Success means arrays from parameters, run results, helper Flows, and restored
locals behave identically; nested arrays stay nested. This group owns the value
model and source migration, with no async launches, futures, await forms, or
spawn implementation. Groups 2-4 have separate definitions and PRs.

This supersedes the affected rules in [Flow Usability](flow-usability.md) and
[Flat Call Input](flat-call-input.md); unrelated behavior remains unchanged.

## Verified Current Behavior

- Run and scatter use the same child-run executor. Scatter requires an array
  output and marks it as list shape; run marks even an array as item shape.
  Gather calls once, requires nonempty primary input, and marks its result item.
- Runtime locals carry shape and sometimes store an element type instead of the
  complete type. Durable/public locals have a separate dim flag. Current source
  checks reject mapping over a run-returned array or a Flow array parameter.
- Map and storm already preserve array-valued child results. Settle rejects
  empty input even with an initializer.
- Grammar 0.3.4 requires using for all named collection targets. Inline scatter
  and settle permit omission; storm, gather, and map require using. Run, exec,
  and seek do not accept using. Offline probes of 84 combinations confirmed that
  let wrappers, lanes, and output annotations do not change those rules.
- Source formatting preserves optional using, while statement-head summaries
  insert it for collection operations. Both surfaces need the new target rule.

## Calls and Array Semantics

| Operation | Calls and input | Result |
| --- | --- | --- |
| `run R` | One call with the whole input when R consumes it | R's complete output |
| `generate N using R` | N calls with the same entry input | N complete results in an outer array |
| `map using R` | One call per outer item | Complete results in input order |
| `reduce using R` | Sequential reduction | The reducer's complete output |
| `keep` / `drop` | Select outer items | Selected items in original order |
| `sort` | Score outer items | Stable ordering of the same items |

1. Run accepts scalar and array contracts, including empty arrays, without
   iteration, wrapping, unwrapping, or flattening. Input-free calls remain valid.
   Inline run defaults to Text; an array output needs an explicit type.
2. Map/reduce/keep/drop/sort require an actual outer array. T[] supplies T items;
   T[][] supplies T[] items. Open Json arrays also work, with Json elements.
   Strings, objects, null, and missing input are not arrays; no implicit JSON
   text parsing or traversal of object fields.
3. Generate and map with child output U return U[], including for zero calls.
   Array-valued output therefore stays nested. Selection and sorting preserve
   the input's complete type, inner values, order contracts, and provenance.
4. Empty map/keep/drop/sort and generate 0 make zero child calls and return
   correctly typed empty arrays; normal target/contract preflight still applies.
   Run passes an empty array and calls once. Reduce retains settle's nonempty
   input requirement, including with from.
5. Without from, reduce seeds from the first element, requires the reducer output
   to equal the element type, and makes N-1 calls. With from, coerce the initializer
   to the reducer output type and make N calls. Preserve iteration history and
   treat an array accumulator as one complete value.
6. Part[] follows the same array rule. Ordinary calls and prompt/model transport
   still render the complete content value using its type, without shape flags.
7. Preserve bindings, positional filtering, predicate/scorer contracts, lane
   ordering, error boundaries, cancellation, and history. Recursive mapping or
   flattening requires explicit helper Flows.

Proposed source:

```too
flow summarize(_: Text) -> Text:
  run -> Text[]: Split {{_}} into topics.
  map -> Text: Explain {{_}}.
  keep first 3
  run: Summarize {{_}}.
```

For [["a", "b"], ["c"]], map receives two arrays; keep first 1 returns
[["a", "b"]]. Calling a helper Flow does not change these rules.

## Runnable-Target Syntax

For generate/map/reduce, named targets require using; inline targets must omit
it. This is based on the target kind, not merely the presence of a colon.

```text
generate N [in P lanes] using R
generate N [in P lanes] [-> T]: BODY
map [in P lanes] using R
map [in P lanes] [-> T]: BODY
reduce using R
reduce [-> T]: BODY

reduce using R:
  from: BODY

reduce [-> T]:
  BODY
  [from: BODY]
```

Run/exec retain their direct named or inline target forms, without using.
Seek is unchanged. Keep/drop predicates still require if; sort still requires
its direction and by clause. Preserve clause order: count/direction, lanes,
then the named target or inline target. Let binding/discard forms do not change
these rules. Inline defaults remain Text for generate/map/reduce.

Reject named forms without using and inline forms with using. Recognize old
forms sufficiently to report a migration diagnostic, never execute them as
implicit prompt text. Do not add produce/gen aliases or optional inline using.
The formatter and statement-head descriptions must agree on the target rule.

## Value Model, Persistence, and Migration

- Remove runtime/static shape and stored/public Local.dim. Do not replace them
  with another collection flag. Runtime types describe complete values; derive
  an element type only when selecting an outer item.
- Keep absent locals and statements without output distinct from JSON null.
  Known non-array/missing inputs fail statically where possible; open Json and
  unknown types get runtime checks inside the operation Step before child calls.
  Repeat joins widen type/length information without a shape lattice.
- Preserve full types, typed refs, selected-item pointers, multimodal Parts, and
  provenance through calls, exec, seek, inline capture, and retry restoration.
  Progress and presentation count outer items using value/type.
- Public locals contain type and value; stored locals contain the existing
  self-describing value. Preserve Output's local/binding wrapper and value codecs.
  Read legacy dim 0/1 after validating its old invariants, then use value semantics.
  Write only the new form; do not rewrite historical records.
- Keep legacy scatter/gather/storm/settle statement records readable, including
  historical scatter counts. Historical decoding does not enable old execution.
  Invalidate affected prepared caches and reject executable/retry/rerun snapshots
  containing removed or invalid old syntax before child calls, with migration
  guidance. Keep source parsing in lang and legacy record decoding in execution.

| Old source | Replacement |
| --- | --- |
| `scatter using expand` | `run expand` |
| `scatter: BODY` or `scatter using: BODY` | `run -> Text[]: BODY` |
| `scatter [using] -> T[]: BODY` | `run -> T[]: BODY` |
| `gather using merge` | `run merge` |
| `gather using [-> T]: BODY` | `run [-> T]: BODY` |
| `storm N [in P lanes] using R` | `generate N [in P lanes] using R` |
| `storm N [in P lanes] using [-> T]: BODY` | `generate N [in P lanes] [-> T]: BODY` |
| `settle using R` | `reduce using R` |
| `settle [using] [-> T]: BODY` | `reduce [-> T]: BODY` |
| `map [in P lanes] using [-> T]: BODY` | `map [in P lanes] [-> T]: BODY` |

Keep result bindings, explicit types, lane clauses, and from blocks when
migrating. Scatter's implicit Text[] must become explicit on run. A former
gather can now receive []; move any required nonempty check into its runnable.
Public clients must stop requiring/sending dim.

A published Tree-sitter release is required for generate/reduce and the strict
target syntax; pin that dependency and update its lock artifacts. No alternate
handwritten parser. Reserve later async/await/spawn syntax only in their own
feature groups. This PR changes definitions, not current product documentation.

## Implementation Touchpoints

- `src/toolang/lang/{ast,lower,contracts,flow_validation,format,description}.py`:
  statements, complete-value types, strict target syntax, diagnostics, and joins.
- Upstream grammar nodes/queries/corpus, then `pyproject.toml`, `uv.lock`, and
  local CST/highlighting/formatting integration.
- `src/toolang/execution/executor/{common,executor,content,iteration}.py`,
  `runs/{flow,agic}.py`, and `stmts/`: array checks and bindings; remove
  scatter/gather executors and rename storm/settle owners to generate/reduce.
- `src/toolang/execution/{types,records,schemas,store}.py`: Local codecs,
  legacy reads, provenance, and public projections; `src/toolang/state/cache.py`.
- CLI execution progress, language/execution/API/record tests, current Flow docs,
  and tracked examples. Generate the implementation's breaking-change entry
  through `too aide.too update_changelog` and obtain maintainer verification.

## Acceptance Tests

1. Parse/check/format named and inline forms with/without types, lanes, and let
   wrappers. Reject removed names, missing named using, and inline using with
   useful replacements. Preserve from blocks; do not accept invalid prose fallbacks.
2. Map/filter/sort/reduce arrays from parameters, run, helper Flows, and seek;
   exec preserves arrays across replacement. Run forwards scalar/empty/nested
   values in exactly one call and still allows no-input runnables.
3. Verify exact types, values, counts, ordering, stable sort ties, and provenance
   for empty/singleton/multiple arrays, Json arrays, Part[], nested arrays, and
   array-valued child results. Reject scalar/object/null/missing collection input.
4. Generate and reduce preserve storm/settle's count, seed, history, lane,
   cancellation, and error contracts. Test zero generation and both empty-reduce
   failures, N-1 unseeded calls, and N seeded calls.
5. Round-trip new locals without dim and legacy records with dim; preserve
   historical inspection. Retry restores complete values and bindings. Removed
   source snapshots and stale caches cannot bypass migration checks.
6. Check tracked examples and documentation links. During implementation run
   the default ruff, format, ty, and offline pytest checks before every commit.

## Risks and Approval

This is a breaking source and Local-protocol change. Main risks are lost array
levels/provenance, content-array regressions, changed gather empty-input behavior,
and inconsistent parser/formatter target rules. Acceptance tests cover these.

Out of scope: automatic source rewriting, flattening, new reduce-empty behavior,
new concurrency operators, futures, root spawning, and a legacy execution engine.
No unresolved design choice is needed for this group's implementation. The human
must approve the complete definition and the grammar release must exist before
implementation. Documentation-only validation is source inspection, offline
syntax/value probes, link checks, and git diff --check; no release entry is needed.
