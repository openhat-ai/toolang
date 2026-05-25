# Model Integrations

This document defines model identity, providers, routes, and execution
selection.


## Core Terms

Toolang separates model identity, discovery, and execution:

| Term | Meaning |
| --- | --- |
| `selector` | One operational input string such as `gpt-5`, `openai/gpt-5`, `openai/gpt-5@openai`, or `gateway` |
| `ref` | One route-neutral canonical identity such as `openai/gpt-5` |
| `provider` | One execution backend such as `openai` or `ollama` |
| `adapter` | One model adapter plugin such as `responses` |
| `model info` | One provider-scoped model entry used for discovery, selector matching, and capability display |
| `model route` | One local named route that binds one `ref` to one provider and optional execution overrides |
| `model target` | One fully resolved execution target used for one runtime call |


## Canonical Model Ref

Toolang identifies models by canonical refs such as:

- `openai/gpt-5`
- `qwen/qwen3`

A canonical ref identifies the model family and name, not a provider route.


## Selectors

Operational surfaces may accept:

- one route name
- one canonical ref
- one shorthand selector
- one provider-qualified selector

Examples:

- `gateway`
- `openai/gpt-5`
- `gpt-5`
- `qwen/qwen3`
- `qwen3`
- `openai/gpt-5@openai`
- `qwen/qwen3@ollama`


## Model Info

Each provider exposes zero or more model infos.

A model info may include:

- `ref`
- `name`
- `model`
- accepted selectors
- `adapter`
- tool-calling support
- streaming support
- optional context window
- optional pricing metadata

Toolang uses model infos for:

- `too model list`
- richer `too model providers` output
- route-neutral thunk ref expansion
- selector matching inside one provider


## Model Config

Root config may define model defaults, provider config, and named aliases under
`[models]`.

An alias binds:

- `ref`
- `provider`
- optional `model`
- optional `adapter`
- optional `endpoint`
- optional `key_env`
- optional `scope`
- optional `tags`
- optional `headers`
- optional `options`

Example:

```toml
[models]
default = ["gateway", "openai/gpt-5[openai]"]

[models.aliases.gateway]
ref = "openai/gpt-5"
provider = "openai"
adapter = "responses"
endpoint = "https://gateway.example.com/v1"
key_env = "GATEWAY_API_KEY"
headers = { "X-Team" = "infra" }
```

The `default` list is ordered. The first entry is the default model selector,
and the full list defines the default allowed set.

Provider-backed aliases may omit `endpoint`, `adapter`, `key_env`, and `model`
when the provider supplies defaults. `provider = "custom"` is reserved for
alias-only OpenAI-compatible endpoints and requires an `endpoint`.

Selectors use `namespace/name[filters]`. The pattern may be omitted, so
`[remote]` means every remote model. Single-word filters map as follows:
`local` and `remote` mean `scope:local` and `scope:remote`; other words mean
`provider:<word>`. Explicit filters use `key:value`, for example
`openai/*[provider:openrouter]` or `*[remote,adapter:responses]`.


## Runtime Model Call

Toolang uses these shared run-side types:

| Type | Fields |
| --- | --- |
| `ModelCall` | `instructions`, `messages`, `tools`, optional `state` |
| `ModelCallResult` | `message`, `tool_calls`, optional `usage`, optional `state` |

Streaming providers emit:

- `ModelPartStartEvent`
- `ModelPartDeltaEvent`
- `ModelPartEndEvent`


## Built-In Model Providers

Current built-in model providers are:

- `openai`
- `openrouter`
- `ollama`

### OpenAI

Built-in OpenAI resolution supports:

- canonical selectors such as `openai/gpt-5`
- shorthand selectors beginning with `gpt-` or `o`
- explicit provider-qualified selectors such as `openai/gpt-5@openai`

### OpenRouter

Built-in OpenRouter resolution supports:

- canonical selectors such as `openai/gpt-5`
- shorthand selectors based on the model slug such as `gpt-5`
- explicit provider-qualified selectors such as `openai/gpt-5@openrouter`

The OpenRouter provider uses:

- `https://openrouter.ai/api/v1`
- `OPENROUTER_API_KEY`
- `GET /api/v1/models` for discovery
- one stateless responses adapter for execution
- default OpenRouter app attribution headers so requests appear as `Toolang`
  in OpenRouter analytics and rankings

### Ollama

Built-in Ollama resolution supports:

- canonical selectors such as `qwen/qwen3`
- shorthand selectors such as `qwen3`, `llama3`, `deepseek-r1`
- explicit provider-qualified selectors such as `qwen/qwen3@ollama`

The Ollama provider uses the local Ollama HTTP API and defaults to:

- `http://127.0.0.1:11434/v1`
- It also discovers installed local models from Ollama's `/api/tags` endpoint.


## Resolution Rule

One run resolves exactly one model target before loop execution starts.

Resolution proceeds in this order:

1. explicit CLI selector
2. default selector from activation config
3. default model route or selector from root config
4. built-in default selector

When a thunk declares route-neutral refs through `model = ...` and the
activation also provides `--models`, Toolang keeps only the intersection and
preserves activation order.
