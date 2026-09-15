# Model Call Resource Controls

## Status

Draft for approval. This document defines behavior only; implementation is a
separate change. It supersedes the earlier draft `model-output-budget.md`, which
proposed a run-level `output` limit and a fixed default ceiling.

## Goal

Give every model call two cross-provider resource controls:

```text
effort     = auto | LEVEL | TOKENS
max_output = auto | TOKENS
```

`auto` always means "do not impose an extra Toolang restriction; use the
model's native behavior or allowance".

## Vocabulary

`LEVEL` is one provider reasoning-effort name:

```text
none | minimal | low | medium | high | xhigh | max
```

`TOKENS` is an explicit token count. Percentages are not supported: a fraction
of an unknown ceiling has no concrete workload meaning.

Configuration is a three-way choice:

| Authored | Meaning |
| --- | --- |
| omitted | inherit the value in effect |
| `auto` | explicitly restore native/automatic behavior, cancelling an inherited value |
| value | explicitly constrain this call |

## effort

- `auto`: send no explicit reasoning constraint. Toolang must not substitute a
  level such as `medium`.
- `LEVEL`: pass the provider-native effort name through.
- `TOKENS`: a reasoning token budget. It is valid only when the model
  advertises reasoning-budget support.
- `none` disables reasoning. For a model that only advertises a reasoning
  toggle, `none` maps to that toggle. There is no separate `enabled` control.
- An unsupported level or an unsupported token-budget mode is an explicit
  error. There is no silent downgrade to a neighbouring level or budget.

## max_output

- `auto` (the default): use the model's native maximum output allowance.
  Resolution is `model.max_output`. If the provider treats an omitted output
  parameter as that same allowance, the adapter omits it; otherwise the adapter
  sends `model.max_output` explicitly.
- `TOKENS`: a hard cap on this call's output.
- An explicit `max_output` must leave room for an explicit reasoning budget.
  A contradictory pair is an error, never a silent clamp.
- Toolang does not pre-compute `min(model.max_output, remaining_context)`. A
  call's output control never depends on the current prompt size.

## Context Handling

Token counts are derived runtime facts, not part of an authored call. A
`ModelCall` carries only intent: the model, the input, `effort`, and
`max_output`.

Runtime resolution turns intent into a resolved call that also carries derived
facts such as the input token count:

```text
ModelCall            intent only
    -> ResolvedModelCall   intent + runtime facts (input_tokens, max_output)
        -> Adapter         provider mapping and provider-specific rules
```

- When the runtime can compute the input token count reliably, it clips
  `max_output` to satisfy the provider's context constraint, or fails when the
  input alone exceeds capacity.
- When it cannot, it does not invent a character-based estimate. The request
  goes to the provider, which enforces its own context limit.
- Adapters never implement a token-estimation heuristic.

## Provider Abstraction

Toolang exposes only `effort` and `max_output`. Adapters map them to their own
fields, for example `reasoning_effort`, `thinking_level`, `thinking_budget`,
`max_output_tokens`, `max_tokens`, and `generationConfig.maxOutputTokens`. No
provider-specific parameter becomes a core language abstraction.

## Metadata

Model capability data provides at least:

```text
max_output
reasoning modes          toggle | effort levels | token budget
supported effort levels
reasoning token budget support
reasoning token min/max, when known
```

Catalog values are evidence, not absolute authority, unless explicitly marked
exhaustive:

- a reasoning option may declare that its enumeration is exhaustive;
- when it is exhaustive, an unlisted effort level is rejected locally;
- when it is not, an unlisted level is passed through and the provider decides;
- `budget_tokens` `min`/`max` are enforced when present;
- `toggle` is represented canonically as `effort=none`.

## Configuration Inheritance

Upper-level configuration:

```text
effort = high
```

A call may then:

```text
effort = auto        cancel the inherited level, use native behavior
effort = high        restate the level
effort = 8192        replace it with a token budget
(omitted)            keep `high`
```

## Run-Level Limits

`RunLimits` continues to bound the complete recursive root run tree with
`agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, and `time`. There is no
run-level output limit: `max_output` is per model call. This removes the
run-level `output` limit introduced by the superseded draft.

## Empty Output

A call can still end without visible content, for example when an explicit
`max_output` is consumed by reasoning. The model step that ends an agic fails
with `EmptyModelOutput` instead of recording an empty success. The earlier
automatic reasoning-headroom raise is replaced by the explicit
`max_output`/reasoning-budget consistency check above.

## Scope

Included:

- the `effort` and `max_output` canonical controls end to end: authored input,
  session defaults, run overrides, HTTP payloads, and persistence;
- runtime resolution of the resolved call, including reliable input token
  counts and `max_output` clipping;
- adapter mapping and omission rules for each built-in adapter;
- catalog metadata interpretation, including the exhaustive marker;
- removal of the superseded run-level `output` limit;
- documentation and offline tests.

Excluded:

- new providers or adapters beyond the built-in set;
- token-budget percentage forms;
- compaction policy changes beyond decoupling the admission budget from
  `max_output`;
- migration or change of persisted record shapes.

## Design Touchpoints

- `src/toolang/base/types/model.py`: canonical resource controls on
  `ModelParameters` and `ModelOverride`; drop the `default` level.
- `src/toolang/base/model_settings.py`: parse, format, apply, and compose
  `effort` and `max_output` independently.
- `src/toolang/plugin/models/resolution.py`: effort evidence rule, budget
  min/max, `toggle` to `none`, and resolved control construction.
- `src/toolang/plugin/models/budget.py`: `max_output` resolution and the
  explicit-consistency check; the input admission budget stops depending on
  `max_output`.
- `src/toolang/base/types/run.py` and `src/toolang/execution/executor/frame.py`
  and `.../steps/model.py`: keep `ModelCall` as intent and carry derived facts
  through the resolved call.
- `src/toolang/plugin/models/adapters/*`: consume resolved facts, choose omit
  versus explicit, and drop the hidden `max_tokens = 4096` fallback.
- Revert the run-level `output` limit: `src/toolang/base/types/policy.py`,
  `src/toolang/execution/types.py`, `src/toolang/setup/config.py`,
  `src/toolang/cli/common/policy.py`, `src/toolang/api/schemas.py`,
  `src/toolang/api/routers/runs.py`, and the chat `/limit` help.
- `docs/models.md`, `docs/executor.md`, `docs/api.md`,
  `docs/input-syntax.md`.

## Acceptance Tests

1. `effort = auto` sends no reasoning constraint and is not mapped to a level.
2. An inherited `effort = high` is cancelled by a call-level `effort = auto`.
3. `effort = 8192` on a model without budget support fails explicitly.
4. An effort level absent from a non-exhaustive catalog enumeration reaches the
   adapter unchanged; the same level is rejected when the enumeration is
   exhaustive.
5. `effort = none` disables reasoning for both effort and toggle models.
6. `max_output = auto` resolves to `model.max_output` or is omitted when the
   provider treats omission as equivalent.
7. `max_output = 8192` caps the call; a value below the explicit reasoning
   budget fails.
8. When the runtime has `input_tokens`, it clips `max_output` to the context
   constraint; when it does not, the request is sent unchanged.
9. No run-level output limit exists in `RunLimits`, CLI, HTTP, or setup config.
10. A terminal model step with no visible content fails with
    `EmptyModelOutput`.
11. The default offline suite passes.

## Risks

- Passing an unlisted effort level through moves the failure to the provider.
  That is the intended trade-off; providers whose enumeration is complete
  declare it exhaustive and keep local rejection.
- Omission semantics differ per provider. Each adapter must state which form it
  sends, and tests must pin it.
- Removing the run-level `output` limit changes the CLI, HTTP, and setup limit
  surfaces. The limit was never released, so no compatibility path is needed.
- Reliable input token counts are not always available before the first call;
  adapters then depend on provider-side enforcement.

## Resolved Decisions

1. `max_output` is per model call. Run-tree totals stay with `tokens`; there is
   no run-level output limit.
2. Token estimation belongs to runtime resolution, not `ModelCall`. Estimate
   only when reliable; otherwise let the provider enforce the limit.
3. Catalog effort enumerations are evidence, authoritative only when marked
   exhaustive.
4. The reasoning toggle merges into `effort = none`.
