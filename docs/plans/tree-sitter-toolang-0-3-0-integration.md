# Adopt Toolang Grammar 0.3.0

## Status and Goal

Scope approved on 2026-09-06, including source and persisted-data compatibility
breaks. Implementation uses the released 0.3.1 patch, which supplies the approved
[shared block model](https://github.com/openhat-ai/tree-sitter-toolang/pull/34)
and strict keyword recognition on every implicit prose line. That follow-up
supersedes the original permissive-continuation behavior in this definition.

Implementation: [Toolang #482](https://github.com/openhat-ai/toolang/pull/482).

Adopt the released `tree-sitter-toolang` 0.3.1 grammar throughout parsing,
semantic validation, formatting, execution, and inspection. Toolang's own
package version is already `0.3.0`; this is a grammar integration, not a package
release or a new language design.

Success means every supported 0.3.0 Flow form lowers and executes correctly,
repository-owned sources use that syntax, removed forms fail clearly, and the
default offline verification suite passes.

## Verified Baseline

Checked against Toolang `aad7fdf9` and upstream tag `v0.3.0`
(`4a4c4dbf6af2963d422a34acbbe774ea38503883`). The dependency declaration is
`tree-sitter-toolang>=0.2.2`; the lock resolves 0.2.2 and Tree-sitter 0.25.2.

Running the existing language tests with only a temporary 0.3.0 dependency
override produced **13 failed, 147 passed**. This is an upgrade probe, not a
failure of the unchanged project's verification:

```sh
uv run --frozen --isolated --with tree-sitter-toolang==0.3.0 pytest tests/unit/lang -q --tb=line
```

Additional probes confirmed that `map in 2 lanes using worker` silently lowers
with `lanes=None`, an inline runnable is mistaken for a name, positional
selection raises `Missing runnable`, repeat raises `Missing repeat statements`,
and sort is unsupported. Existing lowering expects the old CST fields.

Upstream sources: [grammar](https://github.com/openhat-ai/tree-sitter-toolang/blob/v0.3.0/GRAMMAR.md),
[CST fields](https://github.com/openhat-ai/tree-sitter-toolang/blob/v0.3.0/src/node-types.json),
[acceptance tests](https://github.com/openhat-ai/tree-sitter-toolang/blob/v0.3.0/tests/test_flow_syntax.py),
and [published package](https://pypi.org/project/tree-sitter-toolang/0.3.0/).

## Scope

In scope: dependency/lock update, changed Flow CST lowering, `SortStmt` and its
execution semantics, source and compact-head formatting, relevant validation,
AST persistence boundaries, progress/inspection wording, tracked source and
current-documentation migration, and deterministic acceptance tests.

Out of scope: upstream grammar edits, dynamic count interpolation, top-level
`value`, typed `let`, new statement/context variable namespaces, module import
changes, new CLI flags, recall/history redesign, and unrelated execution
refactoring. These broader proposals are not present in the released grammar.
Direct Content `let` remains an item with type `Part[]`; existing runnable type
defaults and reserved `far`, `near`, and `line` names remain unchanged.

## Decisions

### Dependency and Source Compatibility

- Require `tree-sitter-toolang>=0.3.1,<0.4` and lock exactly 0.3.1 for this
  integration. Keep the existing Tree-sitter range and locked 0.25.2 runtime.
  No parser artifacts are vendored. The upper bound prevents an unreviewed
  future minor grammar from entering a fresh installation.
- Accept only the released grammar. Do not add aliases, a legacy parser,
  automatic source rewriting, or runtime fallback for removed forms.
- Preserve `run`, `seek`, `ask`, bare-text runs, result bindings, Content
  bindings, and existing declaration/cap behavior wherever upstream retains
  them. Source migration must respect text boundaries instead of globally
  replacing words inside prompts.

### Authored Flow Surface

| Existing form | 0.3.0 form |
| --- | --- |
| `scatter 4 expand` | `scatter 4 using expand` |
| `storm 4 worker par 2` | `storm 4 in 2 lanes using worker` |
| `gather merge` / `settle reduce` | `gather using merge` / `settle using reduce` |
| `map worker par 1` | `map in 1 lane using worker` |
| `keep predicate par 2` | `keep in 2 lanes if predicate` |
| `drop predicate par 2` | `drop in 2 lanes if predicate` |
| `rank score` | `sort descending by score` |
| `rank score top 3` | `sort descending by score`, then `keep first 3` |
| `rank score bottom 3` | `sort descending by score`, then `keep last 3` |
| `repeat 2:` / `repeat 1:` | `repeat 2 times:` / `repeat 1 time:` |

For affected inline operations, keep the connector: `map using -> Text: BODY`,
`keep if: BODY`, and `sort ascending by: BODY`. Direct colon shorthand for
`scatter`, `storm`, `gather`, `settle`, `map`, predicate `keep/drop`, and sort is
not supported. `run:`, `seek AGENT:`, `ask:`, and `until:` retain their forms.

Counts and sort direction immediately follow their verb. Named runnable and
lane clauses may exchange order; an inline runnable is final. A direction is
required for sort. Counts remain integer literals. Numeric value 1, including
`01`, requires `lane` or `time`; other values require the plural. Lane limits
must be positive; counts and positional selection sizes may be zero. Omitted
lanes retain the executor's existing behavior; no new concurrency default is
introduced. Only storm, map, predicate keep/drop, and sort accept lanes.

### Lowering and Validation

Use the public CST fields, not token-position or regex-based parsing:

| CST change | Required adaptation |
| --- | --- |
| Affected operations expose `runnable` as a named reference or `inline_agic` | Branch on node type and lower inline agics; retain the separate `agic` field for inline run/seek. |
| Parallel operations expose `lanes` directly | Read the integer field; remove the old `par_clause` dependency. |
| Keep/drop expose `selection: position` | Read its `side` and `count` fields. |
| Sort exposes `order`, `runnable`, optional `lanes` | Create `SortStmt` with explicit direction. |
| Repeat exposes `body: statements` and optional `until: inline_agic_body` | Lower the body directly and generate the final Boolean evaluator. |

Replace `RankStmt` with `SortStmt(kind="sort", order, runnable, lanes, binding,
span, doc)`, where `order` is `ascending | descending`. Remove rank's selection
and limit fields and its unused vocabulary. Preserve the narrow language
facade and update AST discriminators/serialization consistently.

Inline `if` defaults to Boolean and inline `by` to Number. Reject an explicit
incompatible return type instead of silently replacing it. Named predicates
and scorers must declare Boolean and Number respectively; reject mismatches
during semantic validation and retain runtime result checks. Inline evaluator
agics continue to disable recall and tools; named agics retain their policy.
Scatter's inline `-> T` continues to describe the element type and lowers to
`T[]` (default `Part[][]`); this upgrade does not enforce its requested count
against the returned array length.

Repeat remains control flow without a result. Reject both named and discarded
`let` wrappers around repeat, even though the CST permits a flow operation
there. Its body updates the working locals; `until` runs after each completed
iteration, uses the latest locals, and propagates evaluator failures.

Recognize Tree-sitter errors, missing nodes, and `invalid_*` nodes before
lowering. Report malformed or legacy Flow headers with their line and useful
syntax context instead of reporting every indented error as bad indentation.
Keep ordinary prompt prose intact. Every implicit continuation line checks its
first complete token for lowercase keywords; malformed keyword-led text is an
error. Capitalization or explicit text blocks permit keyword-led prose.

### Execution and Observability

Sort scores every input once, preserves original item types and provenance,
and orders numerically in the specified direction. Equal scores preserve input
order in both directions; reversing the entire ascending result is therefore
incorrect. Empty input returns an empty list without scorer calls. Scoring
failure fails the statement before its result binding commits.

Sorting and selection are two independent statements, not one fused operation:

```too
sort descending by score
keep first 3
```

The old `rank score top 3` committed the selected result once, in one `par`
Step. The new sort commits the complete sorted collection to `_` in a `par`
Step; keep then commits the selected collection to `_` in a `value` Step.
Keep performs local positional selection with no model call or child Run.
Scoring-call counts are unchanged. Inspection exposes both Steps, and retry
after a committed sort must reuse its result instead of scoring again.
Cancellation between them can leave the sorted collection committed. This is
an intentional change to the statement boundary, not an atomic migration.

For unbound statements, the final value and order match the previous result.
Use `keep last N` after descending sort for bottom-N; ascending plus first-N
would change the output order. The tracked migration sites currently use
unbound rank-with-selection and become two statements. Step paths and retry
prefixes are not preserved across source rewrites.

Named/discarded legacy forms have no general semantics-preserving rewrite in
this integration. For example, `sort ...` followed by `let best = keep first 3`
leaves `_` holding the complete sorted collection, whereas the old
`let best = rank score top 3` left `_` unchanged. Discarding keep likewise
does not undo the sort binding. Do not recommend a generic helper Flow as an
equivalent substitute: current runnable inputs begin as items and `run` binds
an item result, so a typed array does not preserve the collection shape of
rank. Users of these old forms must explicitly re-author their workflow and
accept its new binding boundaries. Collection-preserving composition, block
bindings, new input-selection syntax, and automatic operation fusion require
separate definitions and are outside this upgrade.

Update dispatch, sort result transformation, and progress summaries together.
Keep the execution Step kind `par`: it describes parallel execution and is
independent of the removed authored `par` keyword. New stored statement data
uses `kind="sort"`; CLI inspection and progress display sort direction and
new clause wording. Compact heads may abbreviate inline bodies and must keep
generated `<agic:...>` names hidden.

### Formatting, State, and Rollout

Source formatting preserves valid authored clause order while normalizing
spacing and indentation. It preserves Content, comments, documentation
attachment, implicit-run boundaries, nested repeat bodies, and final until
placement. Formatting twice produces the same output; reparsing preserves
semantic AST content. Compact AST-derived heads use verb, count/direction,
lanes, then runnable as their conventional order.

The formatter derives line roles, text ownership, and control-block ranges from
the CST before rendering. Ordering and spacing retain those annotations instead
of parsing the rendered text again. Lowering and formatting share physical-line
splitting and text-margin handling; each text margin is computed once per block.

Program caches and durable Step `given` payloads contain language AST data;
renaming the statement discriminator is a storage compatibility change.
Follow the repository's current-version-only store policy: increment the
State layer schema and execution store schema (baseline 4 and 37), reject old
stores with the existing schema error, and rebuild derived State from migrated
source. Do not silently reinterpret old rank records, rewrite history, or
delete databases. Old execution history and retries require the previous
runtime; a new runtime uses a separate compatible store. This break applies
to the old store schema even when a particular history contains no rank.

Document that boundary in release notes before shipping. Upgrade source,
dependency, consumers, and schema handling together in one implementation PR;
do not ship a dependency-only intermediate state. Historical design plans
retain their historical examples; update current language/execution docs and
tracked executable sources. Leave untracked user sources untouched. Rollback
requires the previous runtime, source syntax, and matching store; no downgrade
conversion is included.

## Implementation Touchpoints

| Area | Likely files |
| --- | --- |
| Dependency and language | `pyproject.toml`, `uv.lock`, `src/toolang/lang/{ast,lower,validate,format}.py` |
| Execution | `src/toolang/execution/executor/common.py`, `executor/stmts/__init__.py`, rename `executor/stmts/rank.py` to `sort.py`, `src/toolang/execution/types.py` |
| Persistence | `src/toolang/state/cache.py`, `src/toolang/execution/store.py`; verify existing AST consumers in State preparation, execution records, and schemas |
| Presentation | `src/toolang/cli/common/execution_progress/{headers,step_projection,projector}.py`; verify `src/toolang/execution/inspection.py` |
| Migration | Affected tracked `examples/*.too`, `tests/fixtures/*.too`, embedded test sources; verify the catalog agent template; `docs/{flow-syntax,program,execution-presentation}.md`, `RELEASE_NOTES.md`, `CHANGELOG.md` and other current docs containing executable Flow examples |
| Acceptance | `tests/unit/lang/`, `tests/integration/execution/test_flow_scenarios.py`, `test_flow_execution.py`, `tests/unit/state/test_cache.py`, execution record/store tests, CLI progress and inspection tests |

These are bounded touchpoints, not a mandate to edit every listed file.
Parsing remains in `toolang.lang`; execution consumes the semantic AST.

## Acceptance Tests

1. Parse and round-trip all named/inline operations, both allowed clause orders,
   positional selection, count-only/until-only/combined and nested repeat,
   Content/result/discard bindings, comments, and unnamed runnable defaults.
   Verify flattened CST fields do not lose lanes, direction, source lines,
   docs, output types, or inline captures.
2. Reject old connectors and headers, rank/par/top/bottom at statement
   boundaries, missing direction, duplicate/conflicting clauses, invalid
   singular/plural agreement, zero lanes, punctuation, wrong evaluator return
   types, and repeat bindings. Test valid `01 lane`/`01 time`, zero counts,
   and capitalized or explicit keyword-led prose separately.
3. Using deterministic fake runnables, verify storm/map/filter/sort concurrency
   limits and stable input order despite out-of-order completion. Preserve
   scatter's one-child/advisory-count and settle's sequential accumulator
   behavior. No live model or Docker is required.
4. Sort ascending/descending over mixed positive, negative, and equal numeric
   scores; cover empty input, scorer failure, nonnumeric result, named/discard
   bindings, item types, and provenance. Verify sort followed by first/last
   selection, including N=0 and N larger than the input. Assert two committed
   Steps (`par`, then `value`), no extra model calls for positional selection,
   and the distinct `_`/named-local outcomes when selection is bound or
   discarded. Do not claim equivalence with old bound rank-with-selection.
5. Verify zero-repeat leaves locals unchanged, final until sees updated locals,
   early stopping works, and evaluator failure fails the loop. New-version
   retry restores committed loop/selection locals without rescoring committed
   work; old-version retry is rejected at the compatibility boundary.
6. Check formatting idempotence and semantic equivalence, including nested
   repeat/until indentation, inline return annotations, preserved clause order,
   implicit-run blank-line boundaries, and every tracked valid `.too` source.
   Compact heads and progress show sort direction without legacy syntax or
   generated inline names.
7. Round-trip Program, SortStmt, nested repeat, State cache, Step records and
   API serialization. Verify the Step kind remains `par`, direction survives
   reload/inspection, stale caches rebuild from migrated source, and old stores
   fail clearly without mutation. Keep rejected legacy-source fixtures only
   in explicit negative tests.
8. Run the required default checks with the final dependency and migrated
   sources; all must pass before implementation is committed:

   ```sh
   uv run ruff check .
   uv run ruff format --check .
   uv run ty check
   uv run pytest
   ```

## Action Items

- [x] Approve the released-grammar scope and explicit compatibility breaks.
- [x] Update the dependency, AST, lowering, and validation as one integration.
- [x] Implement directional stable sorting and adapt persistence consumers.
- [x] Update formatting, progress, inspection, and schema boundaries.
- [x] Migrate tracked sources and current docs with binding-aware rank rewrites.
- [x] Add the acceptance cases above and pass the complete default suite.
- [x] Fetch/rebase onto current `origin/main`, rerun verification, and open a
      ready implementation PR with migration and rollback notes.

## Risks and Open Questions

The main risks are silently lost lane limits, inline CST nodes being read as
names, accidental changes to prompt boundaries, unstable descending ties, and
rank migration changing the caller's `_` or retry boundaries. The tests above
target each risk directly. Persisted-store incompatibility is intentional and
requires explicit approval with the rest of this definition.

No unresolved technical choices remain in this approved scope. Broader syntax
proposals require separate definitions.
