# Chat Effective Model Default

## Goal

Keep one concrete Chat session model whenever the effective model collection is
non-empty. A model query may exclude configured `default.model`; Chat then
selects a deterministic fallback instead of displaying `[model not set]`.

Success means local and remote Chat apply the same query ordering and fallback
rules, preserve an allowed current request, and display a distinct empty-state
label only when no models are available.

## Scope

- Order model query unions by authored top-level match order. Preserve the
  existing `ModelCollection` base order within each match and deduplicate with
  first-match-wins. Do not change generic tool or cap query ordering.
- Resolve the effective session model in this order:
  1. the current complete `ModelRequest` when it remains allowed;
  2. configured `default.model` when it is allowed;
  3. a fresh request for the first ordered model;
  4. `None` only when the effective collection is empty.
- Apply the rule at Chat initialization, after a session model ceiling changes,
  and when `/model default` is requested.
- Add one-run `:model unset`, which removes the inherited model binding without
  changing the allowed model collection or the Chat session.
- Remove `/model none` and `:model none` immediately. Keep `models=none` for an
  empty allow collection, `effort=none` as a reasoning value, and
  `--default model=none` for clearing layered configuration.
- Replace `[model not set]` with `[no models available]` for an empty effective
  collection.

Provider-internal ranking and models.dev default metadata are out of scope. The
first implementation keeps the current collection base order within each query
match.

## Design Touchpoints

- `src/toolang/plugin/models/collections.py`: ordered model-only query union.
- `src/toolang/cli/toolang/commands/chat/policy.py`: shared effective-model
  reconciliation.
- `src/toolang/cli/toolang/commands/chat/local.py` and `remote.py`: initialization
  and ordered model-list integration.
- `src/toolang/cli/toolang/commands/chat/slashes.py`: `/allow`, `/model default`,
  removed session `none`, feedback, and status presentation.
- `src/toolang/execution/policy.py`: model identity grammar and one-run `unset`.
- `src/toolang/api/routers/agent.py`: query-relative model default projection.

## Acceptance Tests

1. `openai/*,anthropic/*` returns OpenAI matches first, preserves each match's
   base order, and emits overlapping models once at their first match.
2. Chat starts with configured `default.model` when available, otherwise the
   first effective model, and starts without a model only for an empty
   collection.
3. Narrowing `allow.models` preserves an allowed current request and its
   parameters; otherwise it selects the allowed configured default or the first
   ordered fallback with no explicit reasoning parameters.
4. Expanding an empty model ceiling selects a concrete effective default.
5. Local and remote Chat produce the same selection and list ordering.
6. `:model unset` clears only the current run binding. `/model unset`,
   `/model none`, and `:model none` are rejected.
7. `models=none`, `effort=none`, and `--default model=none` retain their existing
   meanings.
8. Empty collections display `[no models available]`; non-empty collections do
   not display an unset-model placeholder.

## Risks

- Ordered-union semantics must remain model-specific so other public collection
  ordering does not change accidentally.
- Remote payload defaults must never identify a model omitted from their item
  list.
- Fallback selection must not carry reasoning parameters across model
  identities.
