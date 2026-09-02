# Reasoning Token Presentation

## Status

Approved by the human on 2026-09-02.

## Goal

Expose provider-reported reasoning token usage in compact execution facts and
historical call inspection without double-counting output or treating missing
provider data as zero.

## Current Behavior

- OpenAI Responses and Chat Completions normalize `reasoning_tokens`, Anthropic
  Messages normalizes `thinking_tokens`, and Gemini Generate Content normalizes
  `thoughtsTokenCount` as `ModelUsage.output_reasoning_tokens`.
- A completed model Step persists the value as the `output.reasoning` token
  meter in versioned `ModelAccounting`. Record round trips preserve the meter.
- `output_tokens` is inclusive: reasoning is one component of output, not an
  additional total.
- Compact progress, model-call Human inspection, and aggregate tree metrics
  currently expose only total input and output tokens. Raw accounting remains
  the only historical view of the reasoning component.
- When a provider omits reasoning usage, the stored value is unknown rather
  than zero.

## Success Criteria

- Completed execution footers show known reasoning usage as an output
  breakdown while preserving the existing input, output, cache, and cost facts.
- Model-call Human inspection shows the same per-call reasoning breakdown.
- Historical tree JSON exposes reasoning totals and whether the aggregate is
  complete.
- Explicit zero, unknown, exact aggregate, and partial aggregate states remain
  distinguishable.
- Existing durable records require no migration and total output is never
  double-counted.

## Display Contract

The complete compact facts bar is:

```text
1m21s · 26 runs 37 models 13 tools · ↑72.5k(33.5%) ↓14.1k(8.6k) · ≈$0.01
```

Duration omits internal spaces. The count group retains the readable `runs`,
`models`, and `tools` labels but omits middle-dot separators inside the group.
`models` and `tools` are compact presentation labels for model calls and tool
calls, not counts of distinct configured resources. The fixed order makes the
three execution counts one scannable fact.

The input and output values retain their current meanings. The reasoning value
is parenthesized without an intervening space immediately after inclusive
output so it reads as a breakdown, not a third total. No label or symbol is
needed: by position, the parenthesized input suffix is the cache-read ratio and
the parenthesized output suffix is the reasoning token count. User-facing
execution-presentation documentation must define this positional grammar.

The states are:

| Stored or aggregate state | Human form |
| --- | --- |
| exact positive value | `↓2.3k(1.8k)` |
| explicit zero | `↓2.3k(0)` |
| some known calls and some unknown calls | `↓2.3k(1.8k+)` |
| no known reasoning value | omitted |

`+` means a known lower bound and applies only to the reasoning component. It
does not make otherwise complete input/output totals appear partial. Compact
counts use the existing `k` and `m` rules.

The model-call summary keeps one line:

```text
Usage        ↑12.4k ↓2.3k(1.8k)
```

At narrow widths, wrap only between the four semantic groups: duration,
execution counts, token usage, and cost. Keep `26 runs 37 models 13 tools` and
the complete input/output/reasoning token group intact when either group fits
on one line. Do not drop counts or introduce additional abbreviations merely to
force a single line.

Cost uses at most four decimal places with `ROUND_HALF_UP`. Omit an absolute
zero amount, including a known zero local-model cost. For a positive amount,
first render at two decimal places. If that would render as zero, retry at four
places. Trim trailing fractional zeroes at the selected precision. A positive
amount that still rounds to zero at four places renders as `<$0.0001` rather
than disappearing or displaying `$0`. Replace the existing `~` estimate marker
with `≈` (U+2248 ALMOST EQUAL TO) for ordinary amounts, producing forms such as
`≈$0.01`. For a positive estimated amount below four-decimal display precision,
combine approximate and less-than semantics as `≲$0.0001` (U+2272 LESS-THAN OR
EQUIVALENT TO). Both glyphs occupy one terminal cell under the shared Rich width
calculation.

| Amount | Cost fact |
| --- | --- |
| `0` | omitted |
| `1.234` | `$1.23` |
| `0.010762` | `$0.01` |
| `0.001276` | `$0.0013` |
| `0.0000124` | `<$0.0001` |

Estimated versions use `≈` for the first two positive examples and use
`≲$0.0001` for the last.

The focused structural-tree Human projection remains exactly `NODE`,
`ACTIVITY`, and `OCCUR`; this feature does not restore a metrics column removed
by the approved focused-inspection design.

## Known And Complete Semantics

A model call has known reasoning usage only when its durable accounting has an
integral `output.reasoning` meter whose unit is `token`. A missing meter,
including a legacy model Step with only input/output totals, is unknown. Do not
infer zero from the selected model, requested effort, visible output, pricing,
or absence of reasoning content.

An aggregate sums every known reasoning meter. `reasoning_complete` is true
only when every counted model call has a known reasoning meter. When there are
zero model calls, the aggregate is `0` and complete, matching existing token
aggregate semantics. When model calls exist but none is known, JSON uses
`null` and false. When only some are known, JSON retains the numeric lower bound
and false, while Human output adds `+`.

The canonical structural-tree JSON metrics add:

```json
{
  "reasoning_tokens": 1800,
  "reasoning_complete": false
}
```

These fields are additive. `reasoning_tokens` is included in `output_tokens`
and must not be added to it when calculating totals, limits, or cost.

## Scope

Included:

- compact root and Flow footer token facts shared by Script and Chat;
- model-call Human inspection usage;
- historical execution-tree reasoning aggregation and additive JSON fields;
- shared extraction and formatting helpers, tests, and affected documentation.

Excluded:

- record schema, provider adapter, pricing, cost accounting, or token-limit
  changes;
- display or persistence of hidden reasoning content;
- live token estimates before the provider returns final usage;
- a metrics column in structural-tree Human output;
- profile overview API changes, dashboards, filtering, sorting, or billing
  reports.

## Design Touchpoints

- `src/toolang/execution/accounting.py`: read one validated integral token meter
  from durable accounting without duplicating decimal conversion rules.
- `src/toolang/execution/trees.py`: aggregate reasoning totals and completeness
  and serialize the additive JSON fields.
- `src/toolang/cli/common/execution_progress/state.py`: retain known reasoning
  counts across model Steps and nested execution metrics.
- `src/toolang/cli/common/execution_progress/formatting.py`: extend the shared
  compact token fact with the optional exact or lower-bound breakdown and own
  the shared adaptive cost formatter.
- `src/toolang/cli/toolang/commands/inspect.py`: reuse the formatter for the
  model-call summary without changing call JSON or tree columns.
- `tests/unit/execution/test_model_accounting.py` and `test_trees.py`: meter
  extraction, persistence compatibility, aggregation, and JSON semantics.
- `tests/unit/cli/test_execution_progress_projector.py`, `test_chat_tui.py`, and
  inspect rendering tests: per-call, aggregate, wrapping, and omission rules.
- `docs/execution-presentation.md` and `docs/api.md`: inclusive-output and
  historical JSON contracts.

## Acceptance Tests

1. OpenAI, Anthropic, and Gemini normalized reasoning usage still round-trips as
   an `output.reasoning` token meter without a record migration.
2. A model Step with 2,300 output tokens and 1,800 reasoning tokens renders
   `↓2.3k(1.8k)` while output remains 2,300.
3. An explicit zero meter renders `(0)`; a missing meter renders no reasoning
   fact.
4. Multiple known meters sum exactly. A mix of known and unknown calls renders
   the known sum with `+`; all-unknown aggregates omit it.
5. Nested Run and Flow metric aggregation produces the same reasoning total and
   completeness as a flat aggregation of their model Steps.
6. Tree JSON emits integer `reasoning_tokens` plus
   `reasoning_complete`; zero-call, all-unknown, partial, and exact cases match
   the defined null and completeness semantics.
7. A representative completed bar renders as
   `1m21s · 26 runs 37 models 13 tools · ↑72.5k(33.5%) ↓14.1k(8.6k) ·
   ≈$0.01`; model-call Human inspection uses the same token grammar.
8. Narrow Script and Chat layouts wrap between semantic groups without
   splitting the execution-count or token group. Existing cache percentage,
   cost-source marker, indentation, and focused tree columns remain unchanged.
9. Absolute zero cost, including all-zero local-model aggregates, is omitted.
   Positive cost uses two decimal places whenever that preserves a positive
   display value, then four as the only precision fallback; smaller positive
   exact amounts use `<$0.0001`, smaller positive estimates use `≲$0.0001`, and
   trailing zeroes are omitted.
10. Ruff, formatting, ty, and the complete default offline pytest suite pass.

## Risks And Open Questions

- Providers differ in whether they report a reasoning component. The explicit
  completeness state prevents a partial aggregate from looking exact.
- The added parenthetical consumes terminal width; existing fact wrapping must
  wrap it as a unit and preserve indentation.
- The unlabeled output parenthetical must be defined in user-facing
  execution-presentation documentation because terminal facts bars cannot
  provide a hover tooltip.
- Output totals are inclusive across current adapters. Documentation and tests
  must keep this visible so consumers do not add reasoning twice.
- There are no open product questions.
