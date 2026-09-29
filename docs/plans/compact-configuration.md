# Configure automatic compaction policy

Status: Proposed; field names and defaults await approval. Extends
[internal compaction runs](persist-batched-compaction-run.md).

## Goal and scope

Expose the user-facing compaction policy in root and agent `config.toml` files.
Omitting the new fields preserves the tested behavior. Keep one `[compact]`
table, normal model-setting syntax, and the existing internal execution path.

## Fields and defaults

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | Automatic selection | Existing exact model specification, including optional `effort` and `max_output`; `unset` disables compaction. Automatic selection remains the first allowed, ready model supporting tool calls. |
| `summary_tokens` | `4096` | Positive integer soft target for the cumulative summary, not a hard output limit. |
| `trigger_ratio` | `1.0` | Start compaction when the caller's estimated input exceeds this fraction of its usable input budget. |
| `retain_ratio` | `0.5` | Target maximum fraction of the caller's usable input budget retained as recent complete root exchanges; always retain the latest historical root even when it exceeds this target. |
| `batch_ratio` | `0.8` | Maximum calibrated estimate for a compact batch as a fraction of the compact model's usable input budget. |

Ratios must be finite numbers, excluding booleans. Require
`0 < trigger_ratio <= 1`, `0 <= retain_ratio < trigger_ratio`, and
`0 < batch_ratio <= 1`. Reject unknown fields, invalid types, and nonpositive
`summary_tokens` during Setup loading.

```toml
[compact]
model = "deepseek/deepseek-flash"
summary_tokens = 4096
trigger_ratio = 1.0
retain_ratio = 0.5
batch_ratio = 0.8
```

The example model is optional, not a provider-specific default. To set a hard
output allowance, use the existing syntax such as
`model = "deepseek/deepseek-flash max_output=8192"`. Without an explicit allowance,
derive `max(2 * summary_tokens, summary_tokens + 1024)` and clamp to the model's
output limit, preserving the current default of 8192 at a target of 4096.
Do not introduce separate `enabled`, `effort`, or `max_output` keys.

## Resolution and runtime behavior

- Resolve each numeric field independently: agent config overrides root config,
  which overrides built-in defaults. Preserve existing model precedence:
  CLI, environment, agent config, root config, automatic selection. Do not add
  CLI flags or environment variables for the numeric fields in this change.
- Resolve and validate one immutable policy through Setup. Include it in Setup
  revision identity and use the existing Setup refresh lifecycle. Execution
  receives concrete values; it does not parse TOML or consult environment values.
- Both caller ratios use its usable input budget after output reservation and
  the existing model safety margin. `batch_ratio` instead uses the compact
  model's usable budget. Token-estimator corrections, the generic 5% safety
  margin, context-error classification, and retry splitting remain internal.
- A lower trigger ratio is a soft threshold. If no historical prefix can be
  compacted, allow a request that still fits the hard budget. Above the hard
  budget, preserve the existing failure. Never repeatedly compact an unchanged
  prefix to satisfy a soft threshold; rebuild and enforce hard admission after
  adopting a result.
- Freeze the effective generation policy in the compact child's existing
  `policy` input. Include `summary_tokens` through the existing `size` field and
  add `batch_ratio`; caller trigger/retention decisions are represented by the
  chosen prefix. Legacy policy records without `batch_ratio` mean `0.8`.
  Pending checkpoints resume only under compatible policy. Already successful,
  valid summaries remain reusable without regeneration, as today.
- Keep existing root-tree limits and child model-call limits; add no separate
  compact timeout, retry limit, storage format, or public execution entry point.

## Implementation touchpoints

- `src/toolang/base/types/compaction.py`: immutable policy vocabulary and defaults.
- `src/toolang/setup/config.py`, `types.py`, `watcher.py`: parsing, field-wise
  resolution, validation, and Setup snapshot identity.
- `src/toolang/execution/compaction.py`: concrete batch policy and checkpoint
  compatibility; preserve additive token-count caching.
- `src/toolang/execution/executor/steps/model.py`, `runs/compact.py`: caller
  thresholds, retained boundary, and generation-policy handoff.
- `docs/models.md`: supported configuration, precedence, and examples.
- Existing Setup and compaction unit/integration tests: acceptance checks below.

## Acceptance checks and risks

- Empty configuration reproduces current thresholds, batch boundaries, summary
  target, and output allowance. Existing `compact.model` examples still work.
- Root/agent partial overrides merge per field; CLI model overrides preserve
  numeric policy. Invalid fields, booleans, NaN/infinity, and ranges fail early.
- Non-default trigger and retention ratios change the eligible prefix while
  preserving whole exchanges and the required last root. Soft thresholds never
  fail a request that fits the hard budget solely because no prefix is eligible.
- Non-default summary size and batch ratio affect actual persisted model calls
  and batch boundaries. Explicit model output allowance remains independent.
- Setup revisions change with effective policy; accepted compact Runs keep their
  captured policy. Legacy checkpoints remain readable; incompatible pending
  checkpoints are not resumed under changed generation settings.
- Lower thresholds can increase call volume; larger summaries and reasoning
  compete for output capacity. Document these consequences without claiming a
  soft summary target guarantees its output length.

Open decision: approve this field set and behavior-preserving defaults before
implementation. Run the repository's default verification for implementation.
