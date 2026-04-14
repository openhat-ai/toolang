# Toolang Model Selection And Model Plugins

This document defines how Toolang should name models, resolve model selectors,
and load model integrations through the plugin system.

Execution strategy lives in [execution.md](./execution.md).
Plugin-family rules live in [plugins.md](./plugins.md).
CLI surface rules live in [api.md](./api.md).


## 1. Design Goals

Toolang should support:

- first-party model integrations
- third-party model integrations
- local model backends such as Ollama
- routed APIs such as OpenRouter
- explicit local configuration
- zero-config use when one installed plugin can satisfy a selector

The design should keep these concerns separate:

- model identity
- access path
- runtime strategy

Rules:

- authored `.too` source should describe model intent, not local transport
  details
- runtime strategy stays in Toolang core
- model plugins perform one model turn only
- model selection should be explicit when more than one installed plugin can
  satisfy the same model request


## 2. Terms

`model ref`

- the canonical identity of one model family and name
- format: `<namespace>/<name>`
- examples:
  - `openai/gpt-5`
  - `anthropic/claude-sonnet-4`
  - `qwen/qwen3`

`model plugin`

- one installed Toolang plugin under the `toolang.model` entry-point group
- examples:
  - `openai`
  - `anthropic`
  - `openrouter`
  - `ollama`

`plugin model name`

- the exact `model` value sent to one plugin backend
- examples:
  - OpenAI plugin for `openai/gpt-5` may use `gpt-5`
  - OpenRouter plugin for `openai/gpt-5` may use `openai/gpt-5`
  - Ollama plugin for `qwen/qwen3` may use `qwen3`

`model selector`

- one user-facing selector string
- may be:
  - one named profile
  - one canonical `model ref`
  - one shorthand alias that can be expanded to a canonical `model ref`
  - one canonical `model ref` plus an explicit plugin route

`resolved model`

- the fully resolved runtime target used for one model call
- contains:
  - canonical `ref`
  - chosen `plugin`
  - `plugin model name`
  - resolved auth and endpoint settings
  - provider-specific options


## 3. Canonical Model Refs

Canonical refs identify the model, not the access path.

Examples:

- `openai/gpt-5`
- `anthropic/claude-sonnet-4`
- `qwen/qwen3`

Rules:

- canonical refs must be stable across plugins
- canonical refs must not encode one local API endpoint
- a canonical ref may be satisfiable by more than one plugin
- the same canonical ref may map to different plugin model names depending on
  the chosen plugin

Example:

- canonical ref
  - `openai/gpt-5`
- satisfiable through:
  - `openai` plugin
  - `openrouter` plugin


## 4. Authored `.too` Source

Thunk-level `model = ...` in `.too` should describe required model identity.

Recommended authored form:

```too
model = openai/gpt-5
```

Toolang may also accept shorthand aliases in authored source if they normalize
deterministically to one canonical ref:

```too
model = gpt-5
```

Rules:

- authored source should resolve to one canonical `model ref`
- authored source should not name one model plugin by default
- authored source should not depend on local credentials, routing, or one
  vendor-specific endpoint layout
- prepared state should store the canonical ref after normalization

This keeps authored source portable across environments.


## 5. Operational Selectors

Operational surfaces may accept richer selectors than authored source.

Recommended selector forms:

- profile name
  - `fast`
- canonical ref
  - `openai/gpt-5`
- shorthand
  - `gpt-5`
- explicit routed ref
  - `openai/gpt-5@openrouter`

Rules:

- `@<plugin>` names the Toolang model plugin that should satisfy the request
- routed selectors are allowed in CLI and API request surfaces
- routed selectors should not be the default authored form in `.too`


## 6. CLI And Runtime Override Rules

`run` and `start` may accept:

```text
too run alice --model <selector>
too alice start --model <selector>
```

This selector is an activation-level default model selector.

It does not rewrite authored source.

Resolution precedence should be:

1. explicit per-run selector from a direct run submission or API request
2. thunk-local canonical model ref from authored `.too`
3. activation default selector from `run/start --model`
4. root-level configured defaults
5. plugin-provided zero-config defaults

Rules:

- `run/start --model` is operational override state for the activation
- an authored thunk model ref should win over the activation default
- if users later need a hard force-override, that should be a separate surface
  such as `--force-model`, not a silent change of `--model` semantics


## 7. Named Profiles

Toolang root config may define named model profiles:

```toml
[models]
default = ["openai_gpt5"]

[models.openai_gpt5]
ref = "openai/gpt-5"
plugin = "openai"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"

[models.router_gpt5]
ref = "openai/gpt-5"
plugin = "openrouter"
model = "openai/gpt-5"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[models.local_qwen3]
ref = "qwen/qwen3"
plugin = "ollama"
model = "qwen3"
base_url = "http://127.0.0.1:11434/v1"

[models.local_qwen3.options]
reasoning = true
reasoning_effort = "medium"
```

Field meanings:

- `ref`
  - canonical model ref
- `plugin`
  - installed Toolang model plugin name
- `model`
  - plugin-specific request model name
- `base_url`
  - optional plugin endpoint override
- `api_key_env`
  - environment variable name to resolve at the call site
- `headers`
  - optional extra request headers
- `options`
  - plugin-specific model options

Rules:

- `models.<name>` is one complete profile
- Toolang should not build inheritance stacks between profiles
- profile parsing should stay explicit and flat


## 8. Route Overrides

Optional route config may choose the default plugin for one canonical ref or
one canonical namespace prefix.

Conceptual form:

```toml
[model_routes]
"openai/*" = "openai"
"anthropic/*" = "anthropic"
"openai/gpt-5" = "openrouter"
```

Rules:

- exact ref routes win over prefix routes
- routes affect plugin choice only
- routes do not change the canonical ref


## 9. Zero-Config Resolution

Installed model plugins should declare what they can satisfy without a named
profile.

Each model plugin should be able to publish:

- canonical namespaces or exact refs it supports
- shorthand aliases or shorthand patterns
- the default environment variable names it expects
- the default base URL it uses
- a priority value used only after explicit routes are checked

Example:

- installed plugin: `anthropic`
- plugin claims:
  - canonical namespace `anthropic/*`
  - shorthand `claude-*`
  - default auth env `ANTHROPIC_API_KEY`

Then:

- if `ANTHROPIC_API_KEY` is available
- and the selector is `claude-sonnet-4`

Toolang may resolve it without user config to:

- `ref = anthropic/claude-sonnet-4`
- `plugin = anthropic`
- `model = claude-sonnet-4`

Rules:

- zero-config resolution is allowed only when one available plugin can satisfy
  the request unambiguously
- if more than one available plugin can satisfy the same canonical ref,
  Toolang should require:
  - an explicit `@plugin` route
  - or a configured route/profile
- silent plugin preference should not hide ambiguity between official and
  routed backends


## 10. Why Plugins And Model Refs Are Separate

Toolang should not treat the plugin name as the canonical model namespace.

Example:

- canonical ref
  - `openai/gpt-5`
- satisfiable through:
  - `openai` plugin
  - `openrouter` plugin

So:

- `openai` in `openai/gpt-5`
  - is part of the model identity namespace
- `openrouter`
  - is one access path

Rules:

- canonical refs name model identity
- plugin names name installed Toolang integrations
- one canonical ref may be satisfiable by multiple plugins


## 11. Model Plugin Family

Toolang should add one model plugin family:

- entry point group:
  - `toolang.model`

Conceptual protocol:

```python
class ModelPlugin(Protocol):
    name: str
    description: str | None

    def claims(self) -> ModelClaims: ...
    def invoke(self, target: ResolvedModel, request: ModelCall) -> ModelCallResult: ...
    def stream(
        self,
        target: ResolvedModel,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult: ...
```

Key point:

- a model plugin performs one model turn
- it does not own the full run strategy


## 12. Strategy Boundary

Run strategy belongs to Toolang core, not to one model plugin.

Model plugins should only handle:

- request encoding
- one provider call
- streaming event decoding
- tool-call extraction
- opaque plugin state, if any

Toolang core should continue to own:

- run strategy
- retry and round policy
- run-input assembly
- run-step persistence
- message persistence

This keeps strategies replaceable without coupling them to one provider.


## 13. Continuation Support

Some backends support provider-managed continuation and some do not.

Examples:

- OpenAI Responses API
  - supports `previous_response_id`
- Ollama OpenAI-compatible `/v1/responses`
  - supports the endpoint
  - does not support stateful `previous_response_id` or `conversation`

Toolang core should not branch on these details.

Instead:

- run strategy always passes full `instructions` plus full `messages`
- model plugin may keep one opaque state payload between turns
- one stateful plugin may internally compress a follow-up into
  `previous_response_id + new messages`
- one stateless plugin may ignore state and resend the full request

Conceptual capability fields:

- `tools`
- `streaming`
- `reasoning_summary`

Rules:

- run strategy carries opaque plugin state but does not interpret it
- model plugin owns provider-specific continuation semantics


## 14. Recommended First-Party Plugins

Reasonable first implementations:

- `openai`
- `anthropic`
- `openrouter`
- `ollama`

An internal helper for OpenAI-compatible wire formats may exist, but it should
not replace dedicated plugins for routed or local backends.
