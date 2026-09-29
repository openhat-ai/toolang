# Configure automatic compaction

Status: Approved for implementation. Extends
[internal compaction runs](persist-batched-compaction-run.md).

## Goal and configuration

Automatic compaction works without configuration. Root and agent `config.toml`
accept four optional fields:

```toml
[compact]
# model omitted: use the current thread model
summary = 4096
recent = "30%"
trigger = "80%"
```

- `model`: an exact model specification with optional `effort` and `max_output`.
  Omission follows the thread model identity, without inheriting its reasoning
  or output settings. Explicit choices must be ready, allowed, and support tool
  calls; never silently switch models. No disabling sentinel or enabled switch.
- `summary`: soft summary length target, default 4096 tokens.
- `recent`: soft budget for recent original historical roots, default 30%.
  Excludes summary, fixed instructions, and the current Run. Preserve whole roots,
  always retain the latest, and advance at least one root per compaction.
- `trigger`: input admission budget, default 80%. Preflight compacts when the
  estimated complete request exceeds this budget.

Each size accepts a positive integer token count or a percentage string in
`(0%, 100%]`, including decimal percentages. Reject booleans, floats, numeric
strings, unknown fields, and invalid model sentinels. Percentages all use the
current thread model's declared context window, never the compact model's window,
current history length, or another configured target. Resolve by flooring to an
integer with a minimum of one token.

The thread model is the current root Run's model binding; nested Runs use this
same reference. If a model-free root delegates to a model-bound child, that
child's model provides the reference. Resolve against the captured Setup.

## Budget and inheritance

Resolve each field independently: agent config, root config, built-in default.
Existing CLI/environment compact-model overrides take precedence over config for
`model` only. No new CLI flags, environment variables, colon overrides, or slash
commands. Reject `unset` in existing compact-model override surfaces as well.

The effective input budget is the smaller of the resolved trigger and the actual
calling model's safety budget (input limit, output reservation, estimation margin).
Use it directly in normal preflight. Resolve recent and summary independently
against the same thread context window. Require recent < trigger when both are
resolvable. Soft targets do not guarantee the final complete request fits;
recheck after adoption and fail when required content cannot fit.

When context metadata is unknown, percentage trigger cannot impose a numeric
ceiling: retain the known safety budget, if any. Never substitute an input limit
for context size. If compaction is needed and a percentage summary/recent remains
unresolvable, fail with an actionable request for context metadata or absolute
values. Integer settings still work without context metadata.

Keep compact batching at 80% of the compact model's own safety budget. Derive its
output allowance as `max(2 * summary, summary + 1024)` unless explicitly overridden,
clamped to the compact model's output limit. Default allowance is 8192 tokens.

## Implementation and durability

- `setup/types.py`, `config.py`, `models.py`, `watcher.py`: one immutable compact
  configuration, parsing, layering, model selection, and Setup revision identity.
- Executor frame/model preflight: resolve thread-relative targets and apply the
  effective input budget and recent-history target. Compact run driver: pass
  resolved summary size and thread model identity into the existing core.
- Existing compact policy persists the concrete summary size, model, and output
  budget; compatible pending checkpoints and valid successful results retain
  existing reuse semantics. No new persistence format or tokenizer pass.
- `cli/common/policy.py`: reject disabling compact-model sentinels.
- Focused model/config docs and Setup/executor tests cover the public contract.

## Acceptance and risks

- Empty config supplies all defaults; root/agent partial overrides and CLI model
  overrides preserve other fields. Invalid fields/values fail during Setup load.
- Different context windows produce proportional targets; explicit token values
  stay fixed. A different compact model does not affect the denominator. Nested
  Runs use the root model; safety limits of the actual caller still apply.
- Trigger changes actual admission; recent changes the retained whole-root
  boundary; summary reaches the prompt, output allowance, and persisted policy.
- Default compact selection follows the thread model, independent of catalog
  ordering and thread reasoning/output parameters; unavailable choices fail.
- Setup changes get new revisions; accepted Runs retain captured configuration.
  Incremental estimation, cancellation, events, and checkpoint reuse stay intact.
- Oversized indivisible roots may require an explicitly configured larger compact
  model. Large summaries and the mandatory latest root can exceed soft targets.

Run all default repository checks. No open design questions remain.
