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
- `TOKENS`: an explicit reasoning budget; missing catalog support information
  permits an attempt when the adapter can encode it.
- `none` disables reasoning. For a model that only advertises a reasoning
  toggle, `none` maps to that toggle. There is no separate `enabled` control.
- An unsupported level or an unsupported token-budget mode is an explicit
  error. There is no silent downgrade to a neighbouring level or budget.

## max_output

- Explicit `TOKENS` or an authored provider output option selects the allowance,
  clamped to the known catalog route output limit.
- Otherwise start at 4096, cap at one quarter of known context, raise to explicit
  reasoning tokens + 1024, then clamp to the route output limit. These constants
  are host policy, not service defaults or catalog facts.
- Require positive output exceeding explicit reasoning tokens. Never reduce a
  reasoning budget or adjust output silently as the prompt grows.

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

- Reserve the resolved output allowance and existing estimation margin against
  known context/input limits. Compact and recheck input overflow.
- Without context/input metadata, omit local input admission and retain bounded
  output; the provider enforces its actual context.
- Adapters send the admitted output allowance unchanged.

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
3. Missing budget metadata permits explicit `effort = 8192`; known restrictions
   and unencodable controls fail explicitly.
4. An effort level absent from a non-exhaustive catalog enumeration reaches the
   adapter unchanged; the same level is rejected when the enumeration is
   exhaustive.
5. `effort = none` disables reasoning for both effort and toggle models.
6. `max_output = auto` resolves through the host policy and is sent explicitly.
7. `max_output = 8192` caps the call; a value below the explicit reasoning
   budget fails.
8. Known input overflow triggers compaction and recheck without changing output.
9. No run-level output limit exists in `RunLimits`, CLI, HTTP, or setup config.
10. A terminal model step with no visible content fails with
    `EmptyModelOutput`.
11. The default offline suite passes.

## Risks

- Passing an unlisted effort level through moves the failure to the provider.
  That is the intended trade-off; providers whose enumeration is complete
  declare it exhaustive and keep local rejection.
- Automatic output now uses host policy for cloud routes too; longer replies
  may require an explicit output setting.
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
