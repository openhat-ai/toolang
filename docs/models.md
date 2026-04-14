# Model Integrations

This document defines model selection and built-in model integrations.


## Canonical Model Ref

Toolang identifies models by canonical refs:

- `openai/gpt-5`
- `qwen/qwen3`

A canonical ref identifies the model family and name, not a transport route.


## Selectors

Operational surfaces may accept:

- a profile name
- a canonical ref
- a shorthand selector

Examples:

- `openai/gpt-5`
- `gpt-5`
- `qwen/qwen3`
- `qwen3`


## Profiles

Root config may define named model profiles under `[models]`.

A profile selects:

- `ref`
- `plugin`
- `model`
- optional `base_url`
- optional `headers`
- optional `options`

Profiles let one agent or runtime choose a concrete backend without changing
the authored program.


## Runtime Model Call

Toolang uses these shared run-side types:

| Type | Fields |
| --- | --- |
| `ModelCall` | `instructions`, `messages`, `tools`, optional `state` |
| `ModelCallResult` | `message`, `tool_calls`, optional `usage`, optional `state` |

Streaming model plugins emit:

- `ModelPartStartEvent`
- `ModelPartDeltaEvent`
- `ModelPartEndEvent`


## Built-In Model Plugins

Current built-in model plugins are:

- `openai`
- `ollama`

### OpenAI

Built-in OpenAI resolution supports:

- canonical selectors such as `openai/gpt-5`
- shorthand selectors beginning with `gpt-` or `o`

### Ollama

Built-in Ollama resolution supports:

- canonical selectors such as `qwen/qwen3`
- shorthand selectors such as `qwen3`, `llama3`, `deepseek-r1`

The Ollama plugin uses the local Ollama HTTP API and defaults to:

- `http://127.0.0.1:11434/v1`


## Resolution Rule

One run resolves exactly one model binding before strategy execution starts.

The binding includes:

- the resolved target
- the loaded model plugin

Model plugins perform one model turn. They do not own the tool loop or run
termination policy.
