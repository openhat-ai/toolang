# Configure automatic compaction

Status: Proposed. Simplified to model selection and summary length; implementation
awaits approval. Extends [internal compaction runs](persist-batched-compaction-run.md).

## Goal and user-facing configuration

Automatic compaction works without configuration. Expose only two optional fields
in root and agent `config.toml` files:

```toml
[compact]
model = "deepseek/deepseek-flash"
summary_tokens = 4096
```

- `model`: existing model specification. Omitted means the first allowed, ready
  model supporting tool calls; no provider is required by default. Preserve
  `model = "unset"` to disable automatic compaction.
- `summary_tokens`: positive integer soft summary target, default `4096`.
  Larger values retain more detail at the cost of more context and output tokens.
  It is not a guaranteed length or a hard response limit.

Normal usage needs only `model`; the summary target can usually be omitted.
Existing model-string `effort` and `max_output` options remain available for
advanced use, without separate compact fields. Derive the output allowance as
`max(2 * summary_tokens, summary_tokens + 1024)` unless explicitly overridden,
and clamp it to the model output limit. The default allowance remains `8192`.

## Internal policy and resolution

Keep the current tested internal policy: trigger above the caller's usable input
budget, retain recent whole roots up to half that budget while always retaining
the latest root, and admit compact batches up to 80% of the compact model's usable
input budget. Do not expose trigger, retention, batching, retry, tokenizer, or
safety-margin knobs. No new CLI flags or environment variables are needed.

Resolve `summary_tokens` per field: agent config, root config, then default.
Preserve model precedence: CLI, environment, agent config, root config, automatic
selection. A model override must not reset the resolved summary target. Reject
unknown fields and invalid summary targets, including booleans, during Setup load.

Carry the validated target in the immutable Setup snapshot and its revision.
Pass its concrete value to the existing compact driver and reducer; record it
through the existing compact `policy.size` field. Accepted Runs retain captured
settings; pending checkpoints require compatible settings. Already successful,
valid summaries remain reusable. No new persistence format is needed.

## Implementation touchpoints

- `src/toolang/setup/config.py`, `types.py`, `watcher.py`: configuration resolution,
  validation, immutable Setup value, and revision identity.
- `src/toolang/execution/executor/runs/compact.py`: replace the hard-coded summary
  target with the resolved Setup value. Reuse existing reducer and checkpoint code.
- `docs/models.md` and existing Setup/compaction tests: document and verify the
  two-field interface. No changes to model-preflight trigger logic are required.

## Acceptance checks and risks

- Empty configuration preserves existing behavior: target 4096, allowance 8192,
  unchanged thresholds and batches. Existing model-only configurations work.
- Root/agent partial overrides and CLI model overrides preserve field independence;
  invalid targets and unknown fields fail early.
- A non-default target reaches the actual prompt, derived output allowance, and
  persisted policy. An explicit model output allowance remains independent.
- Setup revision changes with the target; captured Runs and legacy checkpoints
  retain their existing semantics. Incremental token-count caching remains intact.
- Larger targets can reduce batch capacity; explicit output limits and reasoning
  can prevent reaching the soft target. Explain this without adding more knobs.

Run the repository's default verification for implementation.
