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
| `ModelEntry` | One stable concrete ref, execution target, and metadata record |
| `ModelCollection` | The immutable effective model set published by Setup |

There is no model-provider plugin layer. A provider does not execute calls, and
an adapter does not discover models, match providers, own prices, or determine
availability.

## Static Catalog

The static catalog is one complete models.dev-compatible `models.json`. Toolang
selects it in this order:

1. command-level `--models PATH`, where supported;
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

- `ModelsDevModelCatalog`, for the selected static file;
- `OllamaModelCatalog`, for the configured Ollama endpoint;
- `LlamaCppModelCatalog`, for the configured llama.cpp endpoint;
- `MergedModelCatalog`, which combines ordered snapshots and rejects identity
  conflicts.

A catalog plugin receives concrete configuration from its factory call. It
must not read global CLI state or install packages. Local catalog plugins probe
only their configured/default endpoint, use short timeouts, and keep results in
the current setup snapshot. There is no TTL, disk, or last-good model cache.
The setup watcher re-probes dynamic catalogs on every refresh while reusing an
unchanged parsed static file.

External catalog entry points are opt-in. Configure one by its entry-point name:

```toml
[plugin.model_catalog.company]
url = "https://catalog.example/models.json"
credential_env = "COMPANY_CATALOG_TOKEN"
```

The merged mapping is passed unchanged to the catalog factory; the plugin owns
resolution of `credential_env` when it needs the credential. Built-in
`models_dev`, `ollama`, and `llama_cpp` catalogs remain enabled. Core provider
routes remain under `[models.providers.<name>]`; they are not plugin factory
configuration.

## One-Time Route Resolution

After catalog snapshots are merged, the setup resolver enriches every
`Provider` with its default route and every `Model` with its effective route:

```text
resolved: {
  adapter: string?,
  api: string?,
  env: (string | string[])[],
  ready: bool
}
```

The model route has `adapter`, `api`, and `ready`. Model-level
`provider.npm`, `provider.shape`, and `provider.api` override the provider's
default protocol facts. This supports mixed-protocol routers without a provider
plugin.

`Provider.api` is the raw catalog value. `Provider.resolved.api` is the
effective API base after configuration, catalog, and adapter-default
precedence. It becomes `ModelTarget.base_url` only at the call boundary, where
`base_url` is the client SDK term.

The resolver applies:

- explicit provider configuration before catalog `api` before the adapter's
  protocol default API;
- a small maintained `npm`-to-protocol map, including the major native packages
  whose services expose one of the built-in wire protocols;
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

`ready` is true only when an adapter is installed, an API base is concrete, one
environment alternative is satisfied, and any local probe succeeded. Secrets
are selected only while constructing `ModelTarget`; they are never stored in
`Provider.resolved`, catalog JSON, hashes, or inspection output.

Local provider configuration is passed separately to the resolver. It is never
inserted into raw catalog `extra` fields, so unknown catalog extensions cannot
be interpreted as trusted API routes or credentials. Selection, inspection, and
execution consume resolved facts directly; they do not repeat npm matching,
API fallback, or env interpretation. `--json` therefore remains a raw
catalog projection.

## Adapter Plugins

Adapter plugins use the `toolang.model_adapter` entry-point group and implement:

```python
class ModelAdapter(Protocol):
    name: str
    description: str | None
    default_api: str | None

    async def invoke(self, target, request) -> ModelCallResult: ...
    async def stream(self, target, request, *, on_event) -> ModelCallResult: ...
```

Built-in adapters are:

- `chat_completions`;
- `responses`;
- `messages`;
- `generate_content`.

Adapter factory configuration uses the same plugin grammar:

```toml
[plugin.model_adapter.responses]
```

Only this merged table is passed to the `responses` factory. The built-in
adapters currently define no authored plugin options; external adapters may
define their own non-sensitive values and secret-reference fields.

Adapters receive a concrete API base URL in `ModelTarget`. They translate
canonical messages and tools, normalize streaming, usage, cache, reasoning,
and audio meters, and preserve protocol state needed by later calls. For
example, the Generate Content adapter retains Gemini thought signatures in
provider state and restores them on subsequent tool-call turns. The Messages
adapter likewise preserves signed Anthropic thinking and redacted-thinking
blocks and replays them before the associated tool use.

Canonical reasoning controls use `enabled`, `effort`, and `budget_tokens`.
Adapters translate those names to their wire protocol and reject unsupported or
conflicting combinations. The Chat Completions adapter includes only small,
explicit dialect mappings for well-known compatible providers; unknown provider
extensions are not inferred.

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

Configure discovery independently from the resolved provider call route:

```toml
[plugin.model_catalog.ollama]
endpoint = "http://127.0.0.1:11434"

[plugin.model_catalog.llama_cpp]
endpoint = "http://127.0.0.1:8080/v1"
```

When omitted, the built-ins use `OLLAMA_HOST`, `LLAMA_CPP_HOST`, and then their
loopback defaults. In a Toolang Docker guest, the defaults use
`TOOLANG_HOST_GATEWAY`; loopback values from those two environment variables are
rewritten to the gateway as well. An authored plugin `endpoint` is exact and is
never rewritten, so it can deliberately select a service running inside the
guest. `[models.providers.<name>]` remains core route configuration and is not
passed into either catalog factory.

## Inspection and Export

The public resources are:

```text
too models [--query QUERY] [--json]
too providers [--json]
too catalogs
too adapters [--json]
```

`too models` shows catalog knowledge plus a simple `AVAILABLE` yes/no column.
`too providers` owns readiness diagnostics and shows `ADAPTERS`, `API`,
and `ENV` from `Provider.resolved`. Comma separates OR environment alternatives;
` + ` separates simultaneous requirements.

`too catalogs` lists installed model-catalog plugin entry points and their
`built-in` or `external` source. It does not load the plugins or describe the
merged catalog snapshot; use `too models` for that view.

`too models --query ... --json` emits another complete, deterministic,
models.dev-compatible catalog containing only selected models. Local-only
models cannot be exported. Provider and model JSON never includes `resolved`.

Queries use `PATTERN[field=value;...]`. Exact identity is `provider/model_id`;
model IDs may contain additional `/` characters. Catalog and runtime models
share query fields, including `family`, `reasoning`, `tool_call`, `temperature`,
`structured_output`, `modalities.input`, `status`, `route.provider`,
`route.adapter`, `route.scope`, and `available`. Run `too query models` for the
complete contract.
Model-call parameters such as reasoning effort are structured request fields,
not query syntax.

## Runtime Configuration

Root or agent configuration may override provider runtime values, filter the
effective Setup collection, and select one exact default:

```toml
[models.providers.gateway]
adapter = "responses"
endpoint = "https://gateway.example.com/v1"
key_env = "GATEWAY_API_KEY"

[allow]
models = ["gateway/*"]

[default]
model = "gateway/chat"
```

Provider configuration participates in the one-time provider resolution.
`SetupWatcher` applies `allow.models` once and publishes the resulting
`ModelCollection`; request and runnable policy can only narrow that base.
`default.model` must be one concrete ref present in the effective collection.
When it is absent, Toolang does not choose a first model implicitly. Agics then
require an explicit surface selection, while model-free flows remain valid.

`[models].default` and `[models.aliases.*]` are rejected. Custom model
identities and aliases will be supplied by a future custom catalog rather than
by a parallel runtime route mechanism.

## Runtime Calls and Accounting

`ModelCall` contains provider-neutral instructions, messages, tools, an optional
normalized JSON Schema in `output_schema`, and optional opaque
`continuation` data. Built-in adapters translate the schema and continuation to
their provider request fields. Native schema controls are used only when the
resolved model advertises structured-output support; other targets receive the
deterministic provider-wire schema directive. `ModelCallResult` contains the
assistant message, tool calls, normalized usage, and next continuation.
Canonical and durable JSON use the compact `cont` key. Streaming emits ordered
`ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` updates.

The runtime records inclusive token totals plus cache read/write, visible,
reasoning, audio, and provider-specific meters. Reported provider cost is kept
separately from catalog-derived estimates so historical calls retain their
original pricing revision and coverage.
