# Define Run Model Parameters and Chat Selection

## Status

Approved for implementation on 2026-08-30.

## Goal

Use catalog model identity in effective model lists, keep Chat session defaults
outside run requests, and support catalog-advertised reasoning effort as a
structured model-call parameter.

## Success Criteria

- Generated model refs use `provider/model_id` without a redundant `[provider]`
  suffix.
- Model-list metadata and run requests use the same typed parameter grouping.
- A run request is one self-contained submission, not a transport for mutable
  Chat session defaults or fallback rules.
- Local and remote Chat use a conventional model picker and submit identical
  model requests.
- Reasoning effort is validated against the selected catalog model before run
  acceptance and reaches the adapter and accounting record unchanged.

## Scope

In scope:

- effective model-list refs, parameter metadata, and selector compatibility;
- the core, authored HTTP, direct HTTP, local, and remote run-request boundary;
- Chat session-to-request construction and queued submission snapshots;
- reasoning-effort selection, validation, persistence, retry, and rerun;
- interactive and scripted Chat model selection;
- focused documentation and offline deterministic tests.

Out of scope:

- adding call parameters to selector-list syntax;
- reasoning toggle or token-budget controls in Chat;
- implementing temperature or other new call parameters now;
- changing raw `models.json`, catalog export, pricing, or adapter wire formats;
- changing text input parsing, resolved runnable values, or run event transport.

## Request Boundary

The current request and model schemas are defined in
[`execution/schemas.py`](../../src/toolang/execution/schemas.py) and
[Models](../models.md). This plan's request-boundary intent is retained:

- A `RunRequest` snapshots one concrete runnable request, model request,
  authored input, and materialized execution policy.
- Call sites apply session defaults and input-local overrides before submission.
  Queue entries retain that request when later session settings change.
- `CallInput[str]` is a flat mapping: `_` holds primary input and argument keys
  hold their own source values. It has no separate named-input compartment.
- `RunPolicy` carries ordered allow ceilings and effective run limits. It does
  not transport authored commands or mutable session state.
- Local and remote requests use the same schema; the HTTP boundary does not
  maintain a second vocabulary or invent runnable fallback choices.

## Model List Contract

Effective model lists expose one item per route. Catalog efforts are metadata,
not duplicated models or selector filters:

```json
{
  "ref": "openai/gpt-5",
  "name": "GPT-5",
  "provider": "openai",
  "parameters": {
    "reasoning": {
      "effort": ["minimal", "low", "medium", "high"]
    }
  }
}
```

Efforts are distinct non-empty values in catalog order from
`reasoning_options` entries with `type=effort`. Recognized values are `none`,
`minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `default`. Missing
support produces an empty effort list.

Generated refs use exact catalog identity. Existing authored provider filters
remain accepted as selector-list input for one compatibility cycle, but lists,
requests, and diagnostics do not generate `[provider]`.

## Resolution and Persistence

Preparation resolves `model.ref`, then validates
`model.parameters.reasoning.effort` against the selected model metadata. An
unsupported value fails before acceptance and reports the model and allowed
values. Toolang never guesses a default or nearest effort.

An explicit effort is the complete per-run reasoning choice and produces
`ModelTarget.reasoning == {"effort": VALUE}`. It replaces alias or provider
reasoning defaults so incompatible controls cannot leak into the request. With
no explicit effort, existing target reasoning remains unchanged. Literal
`none` and `default` values are passed unchanged.

Prepared run state persists the `ModelRequest`. Retry preserves it. Rerun
preserves it unless the rerun request supplies a replacement model request.

## Chat Contract

In the interactive TUI, `/model` opens a two-stage picker instead of printing a
flat diagnostic block.

The first stage is a searchable model list:

- display name is primary and `provider/model_id` is muted secondary text;
- `Current` and `Default` are compact badges;
- typing filters; Up/Down navigate; Enter selects; Escape cancels.

If the model advertises efforts, the second stage shows `Auto` followed by
title-cased catalog values. `Auto` sends no effort. A literal advertised
`default` remains a distinct `Default` choice. Enter commits model and effort
atomically; Escape returns to the model list without changing session state.

The status bar shows `MODEL · Effort`, such as `GPT-5 · High`, and omits the
effort under `Auto`. It never displays JSON or selector-filter syntax.

Scripted Chat uses one command family:

```text
/model                         # list models and supported efforts
/model openai/gpt-5            # select the model with Auto effort
/model openai/gpt-5 high       # select the model and effort
/model openai/gpt-5 auto       # clear the explicit effort
```

Text listings show each model once and render effort choices as secondary
human-readable metadata. Local and remote clients derive session state from the
same model-list payload and send the same materialized run request.

## Compatibility

- Chat command syntax remains compatible for `/model` and `/model MODEL`;
  `MODEL EFFORT` is additive.
- The authored run schema intentionally removes `session_commands` and
  `runnable_fallbacks`. There is no dual legacy decoder; mismatched client and
  runtime protocol versions fail visibly.
- Existing durable run history requires no migration. New preparation records
  retain the structured model request. Durable authored-input adapters keep
  their existing record representation; the `_` rename applies to the raw
  request type and wire boundary, not stored record field names.
- Raw model catalog JSON and selector-list syntax do not change.

## Implementation Touchpoints

- `src/toolang/base/types/model.py` for model request and parameter vocabulary;
- `src/toolang/base/types/policy.py` for the explicit request policy fields;
- `src/toolang/lang/input.py` for the `_`-named raw primary input field;
- `src/toolang/execution/schemas.py`, `calls.py`, policy resolution, preparation,
  and retry/rerun persistence;
- `src/toolang/api/schemas.py`, conversion, model projection, and run routers;
- `src/toolang/plugin/models/resolution.py` for clean refs, legacy selector
  compatibility, parameter metadata, and effort application;
- `src/toolang/cli/toolang/commands/chat/` for session state, submission
  construction, queue snapshots, commands, picker, and status;
- model, API, Chat, and input-syntax documentation plus focused tests.

## Acceptance Tests

1. Effective lists generate exact catalog refs and structured parameter
   metadata; legacy provider-filter selector input still resolves.
2. Core and HTTP requests round-trip the grouped model request, reject unknown
   fields, preserve `thread_id`, group each runnable with its input, require a
   concrete runnable ref, expose `_` directly in `CallInput[str]`, perform no
   request-boundary primary alias conversion, and contain no session or fallback
   fields.
3. Submission building applies session and input-local choices without mutating
   session state and materializes named allow and limit policy fields locally
   and remotely while retaining allow-ceiling intersection.
4. Valid effort reaches the target, adapter, accounting, retry, and rerun;
   missing effort preserves defaults; unsupported effort fails before
   acceptance.
5. The TUI picker supports search, navigation, atomic model/effort commit,
   cancellation, conventional labels, and queued snapshots.
6. Scripted `/model` lists, selects, and clears effort through the same typed
   session state.
7. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
   and `uv run pytest` pass.

## Risks

- The run-request schema change is breaking. Version mismatch must fail clearly.
- A modal picker adds UI state. A reusable overlay with atomic commit contains
  the complexity.
- Explicit call parameters can conflict with configured provider options.
  Treating the run value as the complete reasoning choice prevents mixed wire
  requests.

## Open Questions

None.
