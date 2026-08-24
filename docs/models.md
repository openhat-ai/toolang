# Model Catalog and Runtime Integration

Toolang separates model knowledge, runtime readiness, and protocol execution.
The catalog describes what exists; adapters describe how to call one protocol;
the setup resolver joins those facts once for the current process.

## Core Terms

| Term | Meaning |
| --- | --- |
| `Provider` | One models.dev-compatible provider record |
| `Model` | One models.dev-compatible model record nested under a provider |
| `ModelCatalog` | A plugin that returns an immutable provider/model snapshot |
| `ModelAdapter` | A plugin that invokes one wire protocol |
| `ModelInfo` | The runtime selection projection of one catalog model |
| `ModelTarget` | One fully resolved call target with concrete execution values |

There is no model-provider plugin layer. A provider does not execute calls, and
an adapter does not discover models, match providers, own prices, or determine
availability.

## Static Catalog

The static catalog is one complete models.dev-compatible `models.json`. Toolang
selects it in this order:

1. global `--models PATH`;
2. `TOOLANG_MODEL_CATALOG`;
3. the active agent home `models.json`;
4. `${TOOLANG_ROOT}/models.json`;
5. the lightweight catalog packaged with Toolang.

A higher-priority file fully replaces lower-priority files. Toolang does not
merge multiple static files and does not download catalog data during startup.
Users can replace a root or home file with any externally downloaded complete
snapshot.

The importer keeps models.dev provider and model fields at the top level,
preserves unknown additive fields, parses prices as decimal values, and rejects
an invalid complete snapshot. `Provider.to_data()` and `Model.to_data()` emit
only raw catalog data.

## Catalog Plugins

Catalog plugins use the `toolang.model_catalog` entry-point group and implement:

```python
class ModelCatalog(Protocol):
    name: str

    async def snapshot(self) -> ModelCatalogSnapshot: ...
```

Built-in implementations are:

- `ModelsDevModels`, for the selected static file;
- `OllamaModels`, for the configured Ollama endpoint;
- `LlamaCppModels`, for the configured llama.cpp endpoint;
- `MergedModelCatalog`, which combines ordered snapshots and rejects identity
  conflicts.

A catalog plugin receives concrete configuration from its factory call. It
must not read global CLI state or install packages. Local catalog plugins probe
only their configured/default endpoint, use short timeouts, and keep results in
the current setup snapshot. There is no TTL, disk, or last-good model cache.

## One-Time Provider Resolution

After catalog snapshots are merged, the setup resolver enriches every
`Provider` once:

```text
resolved: {
  adapter: string?,
  endpoint: string?,
  env: (string | string[])[],
  ready: bool
}
```

The resolver applies:

- explicit provider configuration before catalog `api` before the adapter's
  protocol default endpoint;
- a small maintained `npm`-to-adapter map;
- environment availability rules;
- installed-adapter and local-probe state.

The outer `env` list is OR. A nested list is AND. An empty list means that no
environment value is required. During default inference, names ending in
`_API_KEY`, `_PAT`, or `_TOKEN` are credential alternatives; other names are
common requirements included in every alternative. Provider-specific rules
cover schemes that cannot be inferred, such as Amazon Bedrock:

```text
[
  [AWS_BEARER_TOKEN_BEDROCK, AWS_REGION],
  [AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION]
]
```

`ready` is true only when an adapter is installed, an endpoint is concrete, one
environment alternative is satisfied, and any local probe succeeded. Secrets
are selected only while constructing `ModelTarget`; they are never stored in
`Provider.resolved`, catalog JSON, hashes, or inspection output.

Selection, inspection, and execution consume `resolved` directly. They do not
repeat npm matching, endpoint fallback, or env interpretation.

## Adapter Plugins

Adapter plugins use the `toolang.model_adapter` entry-point group and implement:

```python
class ModelAdapter(Protocol):
    name: str
    description: str | None
    default_endpoint: str | None

    async def invoke(self, target, request) -> ModelCallResult: ...
    async def stream(self, target, request, *, on_event) -> ModelCallResult: ...
```

Built-in adapters are:

- `chat_completions`;
- `responses`;
- `messages`;
- `generate_content`.

Adapters receive a concrete endpoint in `ModelTarget`. They translate canonical
messages and tools, normalize streaming, usage, cache, reasoning, and audio
meters, and preserve protocol state needed by later calls. For example, the
Generate Content adapter retains Gemini thought signatures in provider state
and restores them on subsequent tool-call turns.

An external adapter should contain no provider matching table. If a new npm
package needs to use it automatically, add that small mapping to the resolver;
users can also select the adapter explicitly in provider configuration.

## Local Providers

Ollama and llama.cpp are catalog plugins using the same `Provider` and `Model`
types as the static source. Their endpoint is both the discovery endpoint and
an important availability fact. An offline local provider remains visible in
`too providers` with availability `0`, while its models are omitted from the
normal model table. Online local models have explicit zero API token prices;
host compute cost is outside model token accounting.

## Inspection and Export

The public resources are:

```text
too models [--filter SELECTOR] [--json]
too providers [--filter GLOB] [--json]
too adapters [--filter GLOB] [--json]
```

`too models` shows catalog knowledge plus a simple `AVAILABLE` yes/no column.
`too providers` owns readiness diagnostics and shows `ADAPTERS`, `ENDPOINT`,
and `ENV` from `Provider.resolved`. Comma separates OR environment alternatives;
` + ` separates simultaneous requirements.

`too models --filter ... --json` emits another complete, deterministic,
models.dev-compatible catalog containing only selected models. Local-only
models cannot be exported. Provider and model JSON never includes `resolved`.

Selectors use `PATTERN[field:value,...]`. Exact identity is
`provider/model_id`; model IDs may contain additional `/` characters. Catalog
filters expose models.dev fields such as `family`, `reasoning`, `tool_call`,
`temperature`, `structured_output`, `modalities.input`, and `status`. Runtime
views additionally expose `provider`, `adapter`, `scope`, and `available`.

## Local Configuration and Aliases

Root configuration may override provider runtime values and define named model
aliases:

```toml
[models]
default = ["gateway", "openai/gpt-5[openai]"]

[models.providers.gateway]
adapter = "responses"
endpoint = "https://gateway.example.com/v1"
key_env = "GATEWAY_API_KEY"

[models.aliases.gateway]
ref = "openai/gpt-5"
provider = "gateway"
headers = { X-Team = "infra" }
```

Provider configuration participates in the one-time provider resolution.
Aliases are explicit routes and may override call target fields such as model,
adapter, endpoint, key environment, headers, options, and scope.

## Runtime Calls and Accounting

`ModelCall` contains provider-neutral instructions, messages, tools, and
optional opaque protocol state. `ModelCallResult` contains the assistant
message, tool calls, normalized usage, and next protocol state. Streaming emits
ordered `ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` updates.

The runtime records inclusive token totals plus cache read/write, visible,
reasoning, audio, and provider-specific meters. Reported provider cost is kept
separately from catalog-derived estimates so historical calls retain their
original pricing revision and coverage.
