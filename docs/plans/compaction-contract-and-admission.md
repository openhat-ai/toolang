# Compaction contract and model-call admission

Status: implemented. Producer details are superseded by
[Text-only compaction algorithms](compact-summary-algorithm.md).

## Scope

Define a fixed framework result and safe model-call assembly/admission for
CLI and automatic compaction. The original scope left `compact.too` unchanged.
The implemented admission path reserves output and validates rebuilt calls.

## Result contract

```text
CompactionResult(thread: str, begin: str, end: str, summary: str)
```

- Every field is required and non-null; summary must contain non-whitespace text.
  Coverage is `[begin, end)` over the target thread's roots. Require a nonempty
  range and a retained terminal root. Only `begin == first_root` is usable as far.
- The shared value lives in `base/types/compaction.py`; execution validates
  references and coverage. `CompactionOutput` carries the durable reference and
  value. Callers resolve bounds and previous summaries before execution.
- Algorithms return Text. Reconstruct the result from recorded
  `{thread, summary, start, begin, end}` input and successful output; publish the
  summary Run reference in thread.horizon. See the text-only algorithm plan for
  persistence and entry-point ownership. No old-format compatibility is supported.
- Invalid explicit horizons fail. Discovery validates the published thread
  horizon without scanning or falling back to older producers. The authored
  output type cannot change the framework result contract.

## Model-call rules

1. Resolve output allowance `O` from explicit controls or the host automatic
   policy (route output limit or 32768 fallback, context-quarter preference,
   and explicit reasoning headroom), clamped to the catalog route output limit.
   Explicit reasoning can exceed the context preference; admission still applies.
   Adapters normalize authored options before admission and send the admitted
   allowance unchanged.
2. `O` includes reasoning. Explicit reasoning budget `R` requires `O > R`;
   do not double-count reasoning, translate effort labels into token counts,
   or silently shrink the selected output/reasoning allowance.
3. For joint context `C`, independent input cap `L`, and margin
   `M = max(1024, ceil(min(known C, L) / 20))`, require:

   ```text
   estimated_input + O + M <= C
   estimated_input + M <= L
   ```

   Apply known limits only. Known context without a resolvable output ceiling
   fails locally; unknown context retains other known checks without claiming
   joint admission. No known input limit means no automatic threshold compaction.
4. Count the complete request, including tools, schema, media, and adapter-added
   content. Distinguish heuristic estimates from provider measurements. Reuse
   calibration only for an unchanged binding, fixed inputs, and message prefix;
   first and rebuilt calls still undergo admission.
5. On overflow, compact eligible history while retaining the required last root.
   Rebuild summary, near, and current exchanges once, preserving declarations
   and tool pairs. Invalidate incompatible estimates when history changes. Preserve same-binding
   continuation needed to replay tool/reasoning exchanges; adapters validate
   reusable server context. Recheck the complete request.
6. Require compaction to advance; fail locally if required content still cannot
   fit. Compact runs obey admission but cannot recursively compact themselves.
   Persist the exact admitted call for streaming, non-streaming, and replay.

## Touchpoints and acceptance

Types/validation: `base/types/compaction.py`, `execution/schemas.py`, and execution
compaction validation. Consumers: compact tool, inspection, CLI, and assembly.
Budgeting: `plugin/models/budget.py`, executor budget/frame/model steps/agic, and
only affected adapters. Extend existing tests for these paths.

Offline acceptance:

- Concrete CLI/automatic bounds; malformed and cross-thread results rejected;
  published/incremental results survive restart; intervals never become far history.
- Text-only `compact.too` uses framework-owned coverage; stale or forged coverage
  cannot be adopted. Assembly contains no duplicated history; replay is exact.
- `665128 + 384000 > 1048576` triggers compaction/error before dispatch, including
  first calls and horizon/model changes. Exact-budget requests fit; +1 fails.
- Cover reasoning, independent/missing limits, missing usage, adapter overhead,
  and output defaults. Both adapter modes preserve the admitted output allowance.
- Oversized summaries/fixed content and non-advancing ranges fail without loops.

Before implementation commits: `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check`, and `uv run pytest`.

## Tradeoffs and approval

Reserving output triggers earlier compaction. The existing 5% estimation margin
cannot guarantee provider token counts; automatic context-error retries are out
of scope. Concrete CLI bounds supersede the null convention in `compact-command.md`.
The reported run has not been inspected; its numbers are a regression fixture.

Contract and budget policy approved by the human for implementation.
