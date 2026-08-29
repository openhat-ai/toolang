# Define Run Model Parameters and Chat Selection

## Status

Proposed on 2026-08-29; awaiting human approval.

## Goal

Use catalog model identity in effective model lists, keep Chat session defaults
outside run requests, and support catalog-advertised reasoning effort as a
structured model-call parameter.

## Success Criteria

- Generated model selectors use `provider/model_id` without a redundant
  `[provider]` suffix.
- Model-list metadata and run requests use the same typed parameter grouping.
- A run request is one self-contained submission, not a transport for mutable
  Chat session defaults or fallback rules.
- Local and remote Chat use a conventional model picker and submit identical
  model requests.
- Reasoning effort is validated against the selected catalog model before run
  acceptance and reaches the adapter and accounting record unchanged.

## Scope

In scope:

- effective model-list selectors and model parameter metadata;
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
- redesigning authored input values or run event transport.

## Request Boundary

Chat owns mutable session state. A submission builder combines a snapshot of
that state with one parsed user input:

```text
ChatSession + ParsedInput -> build_run_request -> RunRequest -> RunSpec
```

Changing `/model`, `/runnable`, or standalone session policy updates only the
client. It does not send a validation request. Server validation happens when a
materialized run request is submitted.

The core vocabulary is:

```python
ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "default"
]


@dataclass(frozen=True, slots=True)
class ModelReasoningParameters:
    effort: ReasoningEffort | None = None


@dataclass(frozen=True, slots=True)
class ModelCallParameters:
    reasoning: ModelReasoningParameters | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    selector: str
    parameters: ModelCallParameters = ModelCallParameters()


@dataclass(frozen=True, slots=True)
class RunPolicyLayer:
    commands: tuple[RunOverride, ...]


@dataclass(frozen=True, slots=True)
class RunRequest:
    thread: str
    request_id: str
    runnable: str
    model: ModelRequest | None
    policy: tuple[RunPolicyLayer, ...]
    input: RunnableInputRaw
```

`runnable` is concrete. `model` is `None` only when the runnable requires no
model. `RunPolicyLayer` contains only request-scoped allow and limit controls;
ordered layers retain ceiling intersection and limit precedence. Default model
and runnable commands are materialized before construction and never appear in
`policy`.

The current `session_commands` and `runnable_fallbacks` fields are removed.
Call sites select one runnable, snapshot the session model and parameters, apply
input-local overrides to that copy, and build the request without mutating the
session. Queued input retains this snapshot even if the UI changes later.

Authored and direct HTTP requests expose the same selection shape and differ
only in their existing input representation:

```json
{
  "thread": "term_123",
  "request_id": "term_request_123",
  "runnable": "agic:chat",
  "model": {
    "selector": "openai/gpt-5",
    "parameters": {
      "reasoning": {"effort": "high"}
    }
  },
  "policy": [],
  "input": {"primary": "Hello", "named": []}
}
```

The parameter schema is closed; unknown fields fail. A future `temperature`
field belongs directly under `ModelCallParameters`, while reasoning-owned
fields remain grouped under `reasoning`. Provider wire options remain separate.

## Model List Contract

Effective model lists expose one item per route. Catalog efforts are metadata,
not duplicated models or selector filters:

```json
{
  "selector": "openai/gpt-5",
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

Generated selectors use exact catalog identity. Existing authored provider
filters remain accepted for one compatibility cycle, but lists and diagnostics
do not generate `[provider]`.

## Resolution and Persistence

Preparation resolves `model.selector`, then validates
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
  retain the structured model request.
- Raw model catalog JSON and selector-list syntax do not change.

## Implementation Touchpoints

- `src/toolang/base/types/model.py` for model request and parameter vocabulary;
- `src/toolang/execution/schemas.py`, `calls.py`, policy resolution, preparation,
  and retry/rerun persistence;
- `src/toolang/api/schemas.py`, conversion, model projection, and run routers;
- `src/toolang/plugin/models/resolution.py` for clean selectors, parameter
  metadata, and effort application;
- `src/toolang/cli/toolang/commands/chat/` for session state, submission
  construction, queue snapshots, commands, picker, and status;
- model, API, Chat, and input-syntax documentation plus focused tests.

## Acceptance Tests

1. Effective lists generate exact catalog selectors and structured parameter
   metadata; legacy provider-filter input still resolves.
2. Core and HTTP requests round-trip the grouped model request, reject unknown
   fields, require a concrete runnable, and contain no session or fallback
   fields.
3. Submission building applies session and input-local choices without mutating
   session state and preserves request policy layer semantics locally and
   remotely.
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
