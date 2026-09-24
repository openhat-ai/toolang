# Model output budget

Status: implemented.

Revises [Route capabilities and call budgets](auto-output-admission.md) and
[Compaction contract and model-call admission](compaction-contract-and-admission.md).

## Goal and evidence

Ordinary calls should not require manual `max_output` tuning because output or
reasoning metadata is missing. Use one automatic rule, honor known limits, and
explain impossible requests before dispatch.

Thread `term_x0bydf0w` records two DeepSeek calls exhausting 4096 output tokens
entirely on reasoning: `run_y8n26e8n.10` and `run_y3twznac.0`. Both requested
automatic output. After a manual 32000 override, `run_er8qnyzz` succeeded; step
`.9` used 4583 reasoning plus 991 visible tokens. This motivates a larger default,
without assuming that every future response will fit.

The earlier PR already improved known-output routes, but retained a 4096 fallback,
or 8192 only when reasoning capability was advertised. Missing metadata selected
less room even when the model might reason. Its claimed quarter-context guarantee
was also inaccurate: explicit R=32768 with C=32768 produced O=33792, then failed
input admission. This revision unifies the fallback and documents that boundary.

Success: every case below has one deterministic result; missing capabilities do
not select a smaller fallback; recorded and dispatched allowances agree. O is a
shared ceiling for reasoning, text, and tool arguments, not a completion guarantee.
Truncated-call recovery remains in [issue #582](https://github.com/openhat-ai/toolang/issues/582).

## Inputs and ownership

| Input | Meaning |
| --- | --- |
| `C = limit.context` | Confirmed joint input and output window |
| `L = limit.input` | Confirmed independent input ceiling |
| `H = limit.output` | Confirmed route output allowance |
| `D` | Explicit `max_output`, otherwise an authored supported adapter output option |
| `R` | Explicit `reasoning.budget_tokens`, if present |

Missing means unknown. Never infer H from L, C from L+H, or reasoning tokens from
an effort label. Discovery normalizes unknown markers; invalid normalized limits
remain errors. The resolver reads no provider names, environment, catalog provenance,
or service defaults. Call sites resolve inheritance and adapters normalize aliases.
Conflicting authored aliases remain invalid even when `max_output` is supplied.

`auto` clears inherited `max_output` but preserves authored adapter options, as
today. Explicit and authored numeric allowances have identical budget semantics.
Reasoning validation and wire encoding remain separate from allowance selection.

## One automatic rule

```text
if D is present:
    O = D
else:
    O = H if known else 32768
    O = min(O, floor(C / 4))     # only when C is known
    O = max(O, R + 1024)         # only when R is explicitly supplied

O = min(O, H)                   # only when H is known
require O > 0
require O > R                   # only when R is explicitly supplied
```

32768 replaces both 4096 and 8192. It is a host fallback, not an inferred model
limit, minimum, or service default. It provides more room for reasoning and tool
arguments without depending on capability metadata. A smaller known H still wins;
a larger known H remains usable, so there is no global 32768 cap.

The quarter-context preference reserves input room without promising a particular
prompt will fit. Explicit R can exceed that preference; actual admission still
applies. The 1024 reasoning headroom is a planning reserve, not a guaranteed answer
size; H can reduce it while O must still exceed R. This also applies to R=0.

There is no `reasoning_capable` input or separate reasoning floor. Absent, true,
and false capability metadata, automatic reasoning, and unbudgeted effort controls
all select output by the same rule. Do not manufacture a reasoning token budget.
Existing validation can still reject unsupported explicit reasoning controls.
Keep the fallback, context fraction, and reasoning headroom fixed; add no knobs
or provider-specific table.

## Input admission and compaction

```text
M = max(1024, ceil(min(known C, L) / 20))
B = min(known C - O, known L) - M
```

Omit unknown terms. Without C or L, B is unknown and local input admission is
skipped. With only L, enforce L-M without subtracting O: L is an independent input
ceiling. With only C, enforce C-O-M. Require B>0. Resolve O and B before dispatch
using the same normalized limits.

Preserve existing admission: estimate the entire request, including instructions,
tools, schemas, media, current exchanges, and continuation overhead. On overflow,
compact eligible history, preserve the required last root and tool pairs, rebuild,
and recheck. Keep O fixed: shrinking output to squeeze in history reintroduces
truncated responses. Compact runs cannot recursively compact themselves.

If fixed content/current exchanges/required last root alone exceed B, fail without
ineffective compaction. A rebuilt request or summary that still exceeds B also
fails. Compaction must advance. Neither positive B nor the context fraction
proves the retained root will fit. Never drop required content or reduce R or D.

## Missing-data matrix

All eight combinations use C=131072, L=65536, H=65536 where present, without R.
D=16384 applies equally to `max_output` and authored adapter allowances. A dash
means unknown, not zero. This covers all 24 limit/source combinations.

| Known limits | Automatic O | Automatic B | D: O | D: B |
| --- | ---: | ---: | ---: | ---: |
| none | 32768 | - | 16384 | - |
| C | 32768 | 91750 | 16384 | 108134 |
| L | 32768 | 62259 | 16384 | 62259 |
| H | 65536 | - | 16384 | - |
| C, L | 32768 | 62259 | 16384 | 62259 |
| C, H | 32768 | 91750 | 16384 | 108134 |
| L, H | 65536 | 62259 | 16384 | 62259 |
| C, L, H | 32768 | 62259 | 16384 | 62259 |

Additional boundaries and realistic cases:

| Limits and controls | O | B / result |
| --- | ---: | --- |
| C=1048576, H=65536; automatic, including auto reasoning | 65536 | 930611 |
| C=1000000, H=384000; automatic | 250000 | 700000 |
| C=32768 only; automatic | 8192 | 22937 |
| C=4096 only; automatic | 1024 | 2048 |
| H=2048 only; automatic, any capability metadata | 2048 | unknown |
| C=131072, L=10000 only; automatic | 32768 | 8976 |
| No limits; automatic with R=8192 | 32768 | unknown |
| C=32768; automatic with R=8192 | 9216 | 21913 |
| No limits; automatic with R=65536 | 66560 | unknown |
| H=8193; automatic with R=8192 | 8193 | unknown; only one token beyond R |
| H=8192; automatic with R=8192 | 8192 | reasoning/output conflict |
| C=32768; automatic with R=32768 | 33792 | no input room; R conflicts with C |
| C=32768; D=32768 | 32768 | no input room; D conflicts with C |
| H=8192; D=65536 | 8192 | unknown; H clamps D |
| No limits; D=4096, R=8192 | 4096 | reasoning/output conflict; do not raise D |
| C=1024 only, or L=1024 only | 256, or 32768 | no input room after margin |

## Errors and acceptance

Invalid controls/normalized limits remain errors. O<=R reports the selected O,
explicit R, and known H. B<=0 reports O, C, L, and the margin; the executor adds
the route and allowance source (`max_output`, adapter option, or automatic).
Existing prompt-overflow errors distinguish no compactable history from required
content that cannot fit. Empty final responses remain failures; their existing
recovery guidance is unchanged.

Send and persist exactly O in streaming and non-streaming calls. Add no automatic
catalog writes, retries, learned hidden budgets, or new inspector schema.

Implementation touchpoints are `src/toolang/plugin/models/budget.py` and
`src/toolang/execution/executor/frame.py`. Offline acceptance covers:

- `tests/unit/execution/test_model_budget.py`: all eight missing-data combinations
  with automatic/explicit demands, reasoning boundaries, invalid values, immutable
  limits, independent input ceilings, and existing exact-B versus B+1 admission.
- `tests/integration/execution/test_model_assembly.py`: missing reasoning metadata,
  both call modes, explicit/adapter/automatic precedence, cleared inherited caps,
  persisted allowances, and a fixture using the observed 4583+991 token need.
  A 4096 cap fails; automatic output completes without caller tuning. This tests
  deterministic execution, not a prediction of live provider behavior.
- `tests/unit/setup/test_catalog_declarations.py`: partial catalogs remain partial;
  budget resolution never writes synthetic limits or reasoning controls.
- `tests/integration/execution/test_compact_scenarios.py`: missing H still permits
  compaction, retains the required root, records the unchanged allowance, and
  rechecks the rebuilt request. Existing required-content and overflow cases stay.
- Existing adapter and empty-output tests preserve wire encoding, unsupported
  controls, and rejection of reasoning-only terminal responses.

[Model documentation](../models.md) and the two linked budget plans describe the
same policy. Verification: Ruff check/format, ty, and the default offline pytest
suite. Provider tests are opt-in.

## Tradeoffs and scope

A 32768 fallback can permit more generated tokens and earlier compaction. It is
not a measured safe maximum: missing/stale H can still cause provider rejection
or truncation. No universal numeric fallback guarantees compatibility with an
unknown output limit; verified route metadata is the durable fix. Unknown C
prevents a joint-window guarantee, and input estimates are not exact tokenization.

A small H or C still constrains automatic output. Even a large O can be exhausted
by reasoning; increasing the fallback reduces avoidable failures but does not
replace truncated-call recovery. Preserve user caps and do not silently lower
effort or retry billed calls. No new flags, discovery, transport, or recovery
policy is in scope.

No open design questions. Truncation classification, recovery, and empty-response
diagnostic redesign remain outside this budget change.
