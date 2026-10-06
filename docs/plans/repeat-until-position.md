# Flexible repeat conditions

Approved contract. See [Flow syntax](../flow-syntax.md) for the language reference.

## Goal and scope

Allow zero or one `until` anywhere in a repeat body, named Boolean conditions,
and unbounded `repeat:` without a count or condition. Source order must determine
execution order while preserving existing trailing-condition behavior.

This definition covers grammar integration, language consumers, runtime,
compatibility, presentation, and tests. Multiple conditions, new `break` or
`continue` statements, and other loop forms are excluded.

## Semantics

- Each repeat requires at least one ordinary Flow statement. Its optional
  `until RUNNABLE` or `until: BODY` may precede, separate, or follow statements.
  Nested repeats own their conditions. Reject condition-only bodies, duplicate
  conditions, conditions outside repeats, and result bindings on `until`.
- A regular variable name is valid iff it fully matches `[a-z][a-z0-9_]*`
  and is not in the grammar-owned keyword set, including reserved legacy words,
  using exact, case-sensitive comparison. Apply to locals and named parameters;
  `_` retains its special primary-input role. Existing invalid names must be
  renamed together with their references.
- At the condition's position, bind inputs from current locals. True exits only
  the owning repeat; false continues with the suffix. Keep committed prefix
  effects; skipped statements create no values or records. The Boolean result
  never replaces parent locals or Flow output. Each evaluation refreshes Run
  handle status, sharing one snapshot across its input checks and binding.
- Count and condition are independently optional. Neither means an unbounded
  loop, ending only through exec, cancellation, or failure. Preserve cooperative
  cancellation even for local-only bodies. `repeat 0 times` makes no calls;
  reaching N passes starts no extra condition check.
- Count a pass only after its whole body succeeds and any condition succeeds or
  skips for insufficient history. A true leading/middle condition does not
  count the partial pass; a true trailing condition does. Failure, cancellation,
  or exec before completion preserves the prior count, including failure or
  cancellation of a trailing condition after the body succeeded. Record
  `satisfied` for condition exit and `exhausted` for the count limit.
- Preserve [history](../../src/toolang/execution/executor/iteration.py): default
  window 3, positive `windowing`, immutable prior counted passes, and nested
  scope shadowing. History stays fixed throughout the pass; partial passes add
  no frame. Inline and named agics skip evaluation as false when their resolved
  templates, including inherited settings and guards, need unavailable history.
  Keep out-of-window preflight checks, including for zero-count loops.
- Named conditions accept only bare `snake_name` targets; kind- or module-qualified
  references are unsupported. Input binding follows `run`; targets require declared
  `Boolean` output and follow [live resolution](runtime-live-resolution.md).
  Preserve visibility, reentry checks, authority, one invocation State snapshot,
  and module-owned settings. Reject named colon bodies, argument lists, and
  `until using`. Named children use the normal tool permissions and Step lifecycle;
  inline evaluators disable tools. Executed tool effects are retained. Child exec
  stays within the child Run.
- Named Flows execute normally with the active history scope. Do not infer
  whole-Flow warm-up recursively: guarded reads work normally, and unguarded
  missing-frame reads fail without undoing earlier child work.
- Analyze condition inputs after the prefix and before the suffix; later
  assignments cannot satisfy first-pass inputs. Preserve finite widening and
  conservative joins, with runtime checks for conditionally available locals.
  An earlier possible condition exit preserves fallthrough past a suffix exec;
  unconditional unbounded loops and unconditional prefix exec have none.

Example:

```too
agic is_done -> Boolean:
  Return true if {{_}} is complete.

flow improve:
  repeat 5 times:
    run: Inspect {{_}}.
    until is_done
    run: Improve {{_}}.
```

## Design and implementation touchpoints

- Use `tree-sitter-toolang==0.4.0a3` in `pyproject.toml` and `uv.lock`. Preserve
  ordered CST ownership and literal prompt indentation; do not preprocess source.
- In `src/toolang/lang/{ast,lower,validate,flow_validation,format}.py`, retain
  `RepeatStmt.stmts` and `runnable`; add `until_index: int | None = None` counting
  preceding body statements. None preserves trailing behavior; an explicit
  index requires a condition and an integer in `0..len(stmts)`, excluding bool.
  Preserve condition source locations and idempotent formatting.
- In `src/toolang/execution/executor/`, execute prefix/condition/suffix through
  existing child-call helpers and the normal tool interface. Body statements
  remain same-Run child Steps with monotonic StepRefs; an evaluated condition is
  a child Run with an unbound result. Preserve zero-based pass indices and
  body/until phase labels; phases do not imply temporal order. Create no new event
  types or synthetic skipped Steps. Preserve existing retry commit boundaries:
  only succeeded containers restore their executed bindings.
- Extend AST codecs to read historical missing indices as trailing, and bump
  `src/toolang/state/cache.py`'s derived layer schema. Rebuild prepared caches
  without rewriting history; no execution database schema change is required.
- Update `src/toolang/lang/description.py` and its script-help/progress consumers
  to display `Repeat indefinitely` when both count and condition are absent.
  In `cli/common/execution_progress/projector.py`, keep condition markers on the
  owning loop through named Flow nesting. In `execution/inspection/trees.py`,
  order Human and JSON trees by pass and authored condition position, including
  Step-rooted inspection and historical trailing defaults. Update current docs
  and generate the changelog through the repository runnable.

## Acceptance and verification

1. Parse/format/AST round trips cover all positions, both target forms, optional
   count/condition, nesting, comments, literal text, tabs, LF/CRLF, and EOF;
   malformed ownership, targets, bindings, and indices produce diagnostics.
   Validate local/parameter names against regex boundaries and the complete
   keyword set; preserve the special `_` parameter.
2. Offline traces verify true/false order, retained prefix effects, nearest-loop
   exit, zero-body leading exit, N=0/N=1, and no extra check after exhaustion.
   Assert calls, final locals, counts, and termination. Condition failure,
   conversion failure, and cancellation never count an incomplete pass.
3. Unbounded loops continue, transfer through exec, and promptly cancel with a
   local-only body. Verify positional input visibility, suffix-exec fallthrough,
   unreachable code, history warm-up, window limits, and nested scope restoration.
4. Named agic/Flow cases reject qualified references and cover typed inputs,
   wrong output types, incompatible live updates, reentry, module/tool settings,
   child exec, handle-status refresh, and the difference in history warm-up.
5. Historical codecs, new-position serialization, cache rebuilds, retry, stored
   records, and model-call reconstruction preserve executed effects. Retry after
   a succeeded partial-pass exit restores only executed bindings. Compare Run-
   and Step-rooted Human/JSON tree order with sequential traces at every condition
   position across multiple passes. Verify nested condition markers and the
   unbounded-loop label in script help and progress.

Use existing language, execution, State, and CLI suites and all default checks.
Verify documentation links, examples, and `git diff --check`.

## Risks and approval

Risks: CST ownership, local visibility, partial-pass accounting, historical
records, named-call scope, and cancellation responsiveness. No open design
alternatives; humans retain definition approval, merge, and release decisions.
