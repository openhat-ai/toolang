# Compaction configuration

## Goal and public contract

Automatic compaction works with defaults and allows independent control of its
model, summary size, retained history, and trigger. This configuration governs
[automatic history compaction](persist-batched-compaction-run.md).

Root and agent `config.toml` accept these optional fields:

```toml
[compact]
# Omit model to use the current thread model identity.
summary = 4096
recent = "30%"
trigger = "80%"
```

| Field | Meaning | Default |
| --- | --- | --- |
| `model` | Exact model specification, optionally including `effort` and `max_output` | Current thread model identity |
| `summary` | Soft cumulative summary length target | 4096 tokens |
| `recent` | Soft budget for retained historical Step units | 30% of thread context |
| `trigger` | Complete-request input threshold for preflight | 80% of thread context |

Model selection must be available, allowed, and support tool calls. The default
uses the thread model identity with the compact model's own reasoning/output
settings. The thread model is the current root Run's model binding; nested Runs
use that reference. A model-free root uses the calling child's model.

Each size accepts a positive integer token count or a percentage string in
`(0%, 100%]`, including decimals. All percentages use the current thread model's
declared context window. Resolve by flooring to an integer, with a minimum of
one token. For a 200,000-token window, `summary="2%"`, `recent=60000`, and
`trigger="80%"` resolve to 4,000, 60,000, and 160,000 tokens.

Validate field names and types during Setup loading. Require `recent < trigger`:
compare matching units during parsing and resolved token counts during frame
preparation. Reject invalid values with an error identifying the field.

## Inheritance and lifetime

Resolve each field independently in this order:

1. Agent configuration.
2. Root configuration.
3. Built-in default.

For `model`, runtime CLI `--compact-model` takes precedence over
`TOOLANG_COMPACT_MODEL`, followed by the configuration layers above. Both accept
the same exact model specification. See [model configuration](../models.md#automatic-compaction-configuration)
for invocation examples.

Resolve against the captured Setup. Configuration changes produce a new Setup
revision; accepted Runs keep their captured settings. Runtime startup overrides
apply when creating that runtime.

## Budget rules

The effective caller input budget is the smaller of the resolved `trigger` and
its model's safety input budget, including output reservation and estimation
margin. Estimate the complete request and recheck it after adopting a summary.

`recent` excludes the summary, fixed instructions, and current Run. Preserve at
least the latest complete history unit and paired tool messages, then add earlier
units while they fit. Coverage may end inside a root. An oversized mandatory
latest Step is shortened with omission markers to at most half the effective
caller input budget. Its original records remain unchanged.

`summary` is a prompt target. The compact model output allowance is
`max(2 * summary, summary + 1024)` unless its model specification supplies
`max_output`, clamped to that model's output limit. The default allowance is 8192
tokens and includes reasoning; an explicit reasoning budget must be smaller than
the resolved output allowance. Batch admission uses 80% of the compact model's
safety input capacity; its window controls batching independently of the thread
model's percentage denominator.

When context metadata is unknown, a percentage trigger leaves admission to the
known safety budget. If compaction is needed with unresolved percentage summary
or recent targets, report that context metadata or absolute token settings are
required. Integer targets can resolve without a context window. Compaction still
requires a known input or context limit for its own model.

The configured summary target is capped internally by remaining caller input
space and compact-model capacity. Validate that the result leaves space for the
next batch and fits the caller's rebuilt request before publication. Oversized
responses retry the same batch at most twice with a smaller target and advance no
checkpoint. Fixed/current content or minimum Step metadata can still exhaust
capacity; fail explicitly rather than publish an unusable result.

## Implementation changes

- Add immutable compact settings in `setup/types.py`; parse and merge fields in
  `setup/config.py` and include them in Setup revisions through `setup/watcher.py`.
- Resolve explicit and default models in `setup/models.py`; parse startup model
  overrides in `cli/common/policy.py`.
- Resolve thread-relative targets and caller admission in executor frames and
  model preflight. Pass concrete settings to `executor/runs/compact.py` and the
  core compaction state.
- Persist resolved model, Setup, summary target, and output policy in the compact
  child entry so checkpoint reuse is validated against its execution contract.

## Acceptance criteria

- Empty configuration supplies all defaults; partial layers and model-only
  overrides preserve independently configured fields.
- Percentages follow the thread context window across nested calls and model
  selection; absolute counts remain fixed. Missing metadata has explicit behavior.
- Trigger changes admission, recent changes the Step boundary, and summary
  changes the prompt target, output reservation, and captured policy.
- Invalid configuration and unavailable models fail clearly. Captured settings,
  checkpoint reuse, cancellation, and events remain consistent after Setup changes.
