# Compaction contract and model-call admission

Status: implemented. Producer details are superseded by
[Text-only compaction algorithms](compact-summary-algorithm.md).

## Scope

Define a fixed framework result and safe model-call assembly/admission for
CLI and automatic compaction. The original scope left `compact.too` unchanged.
Current consumers independently read dictionaries; input admission does not
reserve output, and uncalibrated calls skip output clipping.

## Result contract

```text
CompactionResult(thread: ThreadRef, begin: RunRef, end: RunRef, summary: str)
```

- Every field is required and non-null; summary must contain non-whitespace text.
  Coverage is `[begin, end)` over the target thread's roots. Require a nonempty
  range and a retained terminal root. Only `begin == first_root` is usable as far.
- One execution-owned decoder validates results for CLI, discovery, and assembly.
  `CompactionOutput` carries its durable reference and this concrete result.
  Resolve omitted CLI bounds at the entry point; freeze coverage and previous
  reference, then recheck after lock acquisition and completion.
- Adapt existing script `begin=null` only when recorded inputs and validated
  previous coverage prove a full prefix. Incremental coverage must be contiguous.
  Reject missing fields and contradictory bounds. Preserve raw Run outputs and
  summary references; framework results, including CLI `output`, are concrete.
  Keep the existing CLI envelope and its optional `horizon`.
- Invalid explicit horizons fail; discovery skips invalid or interval results
  and may use an older valid result. The authored type cannot change this contract.

## Model-call rules

1. Resolve output allowance `O` from target max output, otherwise model maximum,
   clamped to the advertised maximum. Resolve any provider-option fallback before
   admission; adapters must send the admitted allowance unchanged.
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
   and tool pairs. Clear incompatible estimates and continuation when history
   or model binding changes, then recheck the complete request.
6. Require compaction to advance; fail locally if required content still cannot
   fit. Compact runs obey admission but cannot recursively compact themselves.
   Persist the exact admitted call for streaming, non-streaming, and replay.

## Touchpoints and acceptance

Types/validation: `execution/types.py`, `execution/schemas.py` and a shared decoder.
Consumers: executor compact, inspection history, CLI compact, and assembly.
Budgeting: `plugin/models/budget.py`, executor budget/frame/model steps/agic, and
only affected adapters. Extend existing tests for these paths.

Offline acceptance:

- Concrete CLI/automatic bounds; malformed and cross-thread results rejected;
  legacy/incremental results survive restart; intervals never become far history.
- Unchanged `compact.too` works through the decoder; stale or forged coverage
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

Contract, compatibility, and budget policy approved by the human for implementation.
