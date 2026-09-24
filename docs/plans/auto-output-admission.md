# Route capabilities and call budgets

Status: implemented. The automatic output allowance is revised by
[Model output budget](model-output-budget.md).

## Contract

Catalog describes the model offered by a specific route under its current
service configuration, not the checkpoint’s theoretical limits. Plugins publish
neutral facts; Toolang owns routing, refresh, and execution policy. Plugin
authors need no access to the service operator’s files or environment.

Keep existing fields and snapshot discovery. Add no `defaults.output`,
`_toolang.runtime`, or per-call discovery protocol.

| Fields | Meaning |
| --- | --- |
| `id`, provider/API identity | Exact model/service binding |
| `limit.context` | Effective joint context capacity |
| `limit.input` | Separately reported input ceiling |
| `limit.output` | Confirmed configured output allowance, even if the API permits overrides |
| `reasoning`, `reasoning_options` | Ability and independently established controls |
| Existing tools, modalities, attachment, structured-output, temperature fields | Confirmed route and transport capabilities |

Missing means unknown. Omit unlimited sentinels, placeholders, and unconfirmed
values; never substitute training maxima or invent controls and metadata.

## Plugin discovery

| Evidence | Ollama | llama.cpp |
| --- | --- | --- |
| List | `/api/tags` | `/v1/models` |
| Optional details | `/api/show`, `/api/ps` | `/props?model=ID&autoload=false` |
| Context | Matching loaded `context_length`, otherwise configured positive `num_ctx` | `default_generation_settings.n_ctx`, otherwise runtime `meta.n_ctx` |
| Output | Applicable positive configured `num_predict` | Confirmed effective positive `n_predict` / `max_tokens` |
| Capabilities | Explicit capabilities and documented API controls | Template capabilities, modalities, and documented API controls |

Use existing endpoint/auth/timeout defaults and configurable discovery headers.
Match exact IDs or unique advertised aliases. Never load models or change service
settings. Optional failures retain valid list facts; listing failures use existing
error handling. Ignore malformed observations with diagnostics. Normalize sentinels
and aliases locally: Ollama last scalar wins; llama.cpp `n_predict` wins.

Reasoning ability does not establish effort levels, disabling, or token budgets.
`supports_reasoning_effort=false` does not mean reasoning is unsupported.

Verified caveat: llama.cpp b10566/bb4caa754 `/props` does not copy the global
prediction count; its `n_predict=-1` cannot establish the configured allowance.
Ollama 0.34.1 model parameters are configuration evidence; its internal 10×context
guard is not a universal catalog limit.

## Toolang policy

Resolve from normalized facts, user controls, and host policy only. Adapters own
wire-option normalization and encoding; budget code never checks provider names.

- Explicit `max_output`, otherwise an authored supported output option, wins;
  reject conflicting aliases and clamp to known route output limit H.
- Otherwise start O at known route output allowance H, or the host fallback
  32768 when H is unknown. Cap the candidate at `floor(C/4)` for known context C,
  raise to R+1024 for explicit reasoning tokens R, then clamp to H. Require O>0
  and O>R. Reasoning capability metadata and effort do not change this fallback.
  The context fraction is a preference that explicit R can exceed, not an input
  admission guarantee. See [Model output budget](model-output-budget.md).
- `auto` clears inherited caps but preserves authored provider options. Send and
  record the same O; prompt growth uses existing compaction, not output reduction.
- Preserve input admission: M=`max(1024, ceil(min(known C, known L)/20))`;
  B=`min(known C-O, known input limit L)-M`. Omit unknown terms; reject nonpositive
  B and compact/recheck overflow. With neither C nor L, omit local input admission.
- Missing reasoning controls: auto sends none; explicit effort, `none`, or tokens
  may be attempted. Reject invalid/conflicting values, known restrictions, or
  unencodable controls. Never silently downgrade or retry provider errors.

Use existing refresh; new snapshots affect later calls. Invalidate service facts
on endpoint changes and obsolete discovery caches via a format revision. Preserve
historical snapshots. Never write resolved call budgets into catalog data.

## Acceptance and implementation

Offline tests cover origin-independent resolution; both plugins and a minimal
third-party catalog without output metadata; distinct limits for the same model
on differently configured routes; partial discovery, sentinels, aliases, router
no-autoload, refresh/cache behavior; incomplete reasoning controls; budget
boundaries, compaction, and matching recorded/wire values in both call modes.

Touchpoints: base model types/protocols, catalog plugins, setup routes/cache,
model budget/reasoning resolution, executor frames, inspection, tests, and model/
admission docs. Implement neutral declarations, discovery, then host resolution;
run default Ruff, ty, and pytest checks before implementation commits.

Exclude server mutation, new adapters/flags, pricing, media/history changes,
retries, and auto-continuation. Risks: stale facts, truncated output, and unknown
context preventing guaranteed local admission. The model documentation describes
the automatic allowance and its effect on input admission. No open design questions.
