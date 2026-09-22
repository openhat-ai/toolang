# Model output budget

Status: implemented.

Revises the automatic output allowance in
[Route capabilities and call budgets](auto-output-admission.md) and rule 1 of
[Compaction contract and model-call admission](compaction-contract-and-admission.md).

## Problem

`run_035wfx7h` aborted at step `.40` with `tool call arguments were not valid
JSON`. The route published `limit.context=1000000` and `limit.output=384000`, but
the automatic allowance was a fixed 4096 that only ever shrank, so reasoning
tokens and the answer shared it and the tool-call payload lost its closing bytes.

## Contract

`output_budget(limits, *, demand, reasoning, reasoning_capable)` resolves one
call's allowance from normalized facts, user controls, and host constants. It
reads no provider name, catalog provenance, or service default. Missing means
unknown; a missing fact never becomes a guessed value.

| Input | Meaning |
| --- | --- |
| `limit.output` H | Confirmed route output allowance |
| `limit.context` C | Effective joint input+output window |
| `limit.input` L | Separately reported input ceiling |
| `demand` | Explicit `max_output`, else an authored adapter allowance |
| `reasoning.budget_tokens` R | Explicit reasoning token budget |
| `reasoning_capable` | The route advertises reasoning |

## Resolution

Explicit demand wins:

```text
O = min(demand, H)                 # H omitted when unknown
```

Otherwise:

```text
O = H                              # known route allowance
O = 4096                           # else the host floor
O = max(O, 8192)                   # route can reason, no explicit token budget
O = min(O, floor(C / 4))           # known joint context
O = max(O, R + 1024)               # explicit reasoning token budget
O = min(O, H)                      # known route allowance
```

Then require `O > 0`, and `O > R` for an explicit R. A reasoning effort of `none`
disables the reasoning floor.

Host constants: floor 4096, unbudgeted-reasoning floor 8192, context fraction 4,
reasoning headroom 1024.

## Admission

Input admission is unchanged: `M = max(1024, ceil(min(known C, L) / 20))` and
`B = min(known C - O, known L) - M`, skipping unknown terms. The automatic
allowance never exceeds `floor(C/4)`, so a known joint context always leaves a
positive input budget and the policy cannot starve the retained near history. An
explicit `max_output` is only clamped to H; when it leaves no input room,
admission fails before dispatch with the existing `no input budget` error.

## Cases

| C | H | reasoning | O | B |
| --- | --- | --- | --- | --- |
| - | - | none | 4096 | - |
| 32768 | - | none | 4096 | 27033 |
| 4096 | - | none | 1024 | 2048 |
| - | 2048 | none | 2048 | - |
| 131072 | 65536 | none | 32768 | 91750 |
| 1048576 | 65536 | none | 65536 | 930611 |
| 1000000 | 384000 | none | 250000 | 700000 |
| - | - | capable, unbudgeted | 8192 | - |
| 16384 | - | capable, unbudgeted | 4096 | 11264 |
| 32768 | - | R=8192 | 9216 | 21913 |
| - | - | effort `none` | 4096 | - |

## Acceptance

`tests/unit/execution/test_model_budget.py` covers every row above plus invalid
policy values. `tests/integration/execution/test_model_assembly.py` covers an
incomplete-catalog route reaching the adapter with its recorded call.

## Risks

- A larger O shrinks input admission. The context-quarter cap bounds it, and a
  route with neither C nor L still performs no local admission.
- The unbudgeted-reasoning floor is a host floor, not a measurement of a route
  that publishes no reasoning budget.
- Catalog limits are route-scoped and can be stale or partial.
