# Model Catalog and Runtime Integration

Toolang separates model knowledge, runtime readiness, and protocol execution.
The catalog describes what exists; adapters describe how to call one protocol;
the setup resolver joins those facts once for the current process.

## Core Terms

| Term | Meaning |
| --- | --- |
| `Provider` | One models.dev-compatible provider record |
| `Model` | One model record linked by `_toolang.provider` |
| `ModelCatalog` | A plugin that returns an immutable provider/model snapshot |
| `ModelAdapter` | A plugin that invokes one wire protocol |
| `ModelRequest` | One run's concrete model demand |
| `ModelRoute` | The effective connection published at `Model._toolang.route` |
| `ModelCall` | One model call: content plus effective controls |
| `ModelCollection` | The immutable effective model set published by Setup |

There is no model-provider plugin layer. A provider does not execute calls, and
an adapter does not discover models, match providers, own prices, or determine
availability.

## Static Catalog

The preferred static input is the combined `{models, providers}` object from
models.dev `catalog.json`. Toolang currently consumes its `providers` member
and also accepts the provider map from `api.json` directly. The
provider-agnostic `models.json` lacks provider execution records and is not a
valid static catalog.

Download the preferred input as:

```bash
curl -fsSL https://models.dev/catalog.json -o catalog.json
```

Toolang selects the catalog file in this order:

1. command-level `--catalog PATH`, where supported;
2. `TOOLANG_MODEL_CATALOG`;
3. the active agent home `catalog.json`;
4. `${TOOLANG_ROOT}/catalog.json`;
5. the lightweight catalog packaged with Toolang.

A higher-priority file fully replaces lower-priority files. Toolang does not
merge multiple static files and does not download catalog data during startup.
Explicit CLI and environment paths are filename-agnostic. Implicit discovery
recognizes only `catalog.json`; `models.json` has no special legacy meaning and
is ignored. If no catalog is selected, Toolang uses the packaged data.

When no agent is selected, inspection uses only the root source and root model
context; it does not read an implicit `agents/default`. In a Docker guest, an
external `--catalog` source is mounted read-only and
`TOOLANG_MODEL_CATALOG` is rewritten to its guest path.

Use `too alice models` to inspect a resident agent's model context. It layers
the agent's provider/plugin configuration and dotenv values over root inputs,
and prefers its home catalog according to the precedence above. The agent
does not need to be running. `--catalog`, `--all`, `--query/-q`, and `--json` work in
both root and resident forms:

```bash
too models
too alice providers --all
too alice models --all --query '*[available=false]'
too --root /path/to/root agent:alice models --catalog /path/to/catalog.json --json
```

The target goes before `models` or `providers`; use `agent:<name>` when a name matches
a command name. Both forms default to ready models permitted by `allow.models`.
`--all` includes both unready models and models excluded by `allow.models`.
Queries narrow the selected view; `--json` changes only the output format.
The commands do not display `default.model`/`compact.model`. Availability reflects
the invoking process's configuration and environment, not a running agent's
session or sandbox.

The importer validates both members of a combined catalog before selecting its
provider map. It keeps models.dev provider and provider-model fields at the top
level, drops unmodelled additive fields, parses prices as decimal values, and
rejects an invalid complete snapshot. Canonical model metadata from the
combined input is not retained in the runtime snapshot. `Provider.to_data()`
and `Model.to_data()` emit only raw provider catalog data, so `too models
--json` remains a round-trippable filtered catalog export.

## Model Selection

Configure authorization and preference together, at root or agent scope:

```toml
[allow]
models = ["openai/*", "google/*", "*"]
# A collection query string is also accepted: "openai/*, google/*, *"
[default]
model = "openai/gpt-5 effort=medium"
[compact]
model = "openai/gpt-5 effort=low"
```

Without `allow.models`, available providers are preferred in this order: alibaba,
anthropic, deepseek, google, meta, minimax, mistral, moonshotai, openai, openrouter,
xai, zai, zhipuai, then all remaining providers. Models within a provider retain
catalog order. This preference order excludes no ready models. Explicit queries
replace the ordering; `*` preserves catalog order, while `all` restores the default.

Omit `compact.model` to select the first allowed, available model with both tool
calls and structured output. Session/request model restrictions still apply.
Compact never inherits the normal model or its effort. An explicit compact model
must meet the same requirements; invalid choices or parameters fail without
fallback. Set `model = "unset"` to disable automatic compaction.

Override compact selection with `TOOLANG_COMPACT_MODEL='openai/gpt-5 effort=low'`
or `too alice run --compact-model 'openai/gpt-5 effort=low'`. Precedence is CLI,
environment, agent config, root config, then automatic selection.
`--compact-model` also applies to `start` and `chat` when starting a runtime;
it cannot reconfigure an already running agent. There is no `compact.models` setting.

## Catalog Plugins

Catalog plugins use the `toolang.model_catalog` entry-point group and implement:

```python
class ModelCatalog(Protocol):
    name: str

    async def snapshot(self) -> ModelCatalogSnapshot: ...
```

Built-in catalog plugins live in `toolang.plugin.catalogs`:

- `ModelsDevModelCatalog`, for the selected static file;
- `OllamaModelCatalog`, for the configured Ollama endpoint;
- `LlamaCppModelCatalog`, for the configured llama.cpp endpoint.

`toolang.setup` combines ordered snapshots with `MergedModelCatalog`, which
rejects identity conflicts, and resolves them into effective `Provider` and
`Model` instances. Providers contain no models list: snapshots and setup hold
separate provider and model collections, joined by `Model._toolang.provider`.
The parser flattens external nested catalogs and JSON export rebuilds that
structure. Provider display counts and availability use the selected models
joined by ownership.

A catalog plugin receives concrete configuration from its factory call. It
must not read global CLI state or install packages. Local catalog plugins probe
only their configured/default endpoint and use short timeouts. The setup watcher
re-probes dynamic catalogs and publishes their current result; callers only read
a published Setup version. Every source's records are cached, and a dynamic
catalog persists one probe file whose mtime stamps its current result.

Each root or agent model context keeps one cache file per catalog below the
owning `.setup`. The models.dev file is read once per change; its revision is the
payload digest plus the file mtime. A local catalog's file is rewritten only when
its probe result differs, so its mtime marks when the current run of identical
results was first saved. Cache revisions cover model-affecting configuration,
plugin provenance, environment values, catalog revisions, and effective
`allow.models`; they contain neither absolute paths nor environment values, so a
cache produced on the host stays reusable when the same root and home are mounted
at different guest paths. Invalid, unsafe, or legacy cache entries are misses, and
a cache write failure does not reject a valid in-memory Setup.

Persistence retains all source records, independently of readiness or allow rules.
Both source caches and private full-view payloads store each model once in a
top-level list; provider records contain neither models nor model IDs.
The published setup indexes only ready, allowed models and their providers.
`setup.model_catalog()` returns that default view; `setup.model_catalog(all=True)`
materializes the complete resolved view from compact serialized records pinned to
that setup version. Full reads do not retain another query index, re-read a source,
or change the models available to a run. Old setup versions remain consistent
after later refreshes or cache deletion.

External catalog entry points are opt-in. Configure one by its entry-point name:

```toml
[plugin.model_catalog.company]
url = "https://catalog.example/models.json"
credential_env = "COMPANY_CATALOG_TOKEN"
```

The merged mapping is passed unchanged to the catalog factory; the plugin owns
resolution of `credential_env` when it needs the credential. Built-in
`models_dev`, `ollama`, and `llama_cpp` catalogs remain enabled. Provider routes
belong to the declaring catalog plugin; core provider override tables are rejected.

## One-Time Route Resolution

After catalog snapshots are merged, the setup resolver enriches every
`Provider` with its default route and every `Model` with its effective route:

```text
ProviderToolang: { env: declared rule, adapter: declared adapter, route: ModelRoute }
ModelToolang:    { provider: string, ready: bool, route: ModelRoute }
ModelRoute:      { adapter: string?, api: string?, env: rule?, headers, options }
```

Setup resolves routes before publication. Provider metadata retains trusted
catalog declarations plus its effective default route; each model carries its
own effective route. Model-level protocol and API overrides remain catalog
facts, without injected resolution fields. CLI and executor consume these
published routes, and adapters receive `(model, request, *, environ)`.

`Provider.api` stays the raw catalog value. Setup resolves model/provider API
values, adapter defaults, and templates into `route.api`. Route environment
rules contain names only; actual values remain in `setup.envs`.

A missing/uninstalled adapter or invalid selected catalog mode yields
`route.adapter=None`; an unresolved API
yields `route.api=None`; unmet environment requirements yield `route.env=None`.
An empty env rule means no credential is required. Setup resolves each field
independently and sets `ready` only when all three are non-None. No issues list
is stored. Headers and options are recursively immutable; adapters copy them
into mutable provider request payloads.

A declared `provider.mode` must select an object in `experimental.modes`.
Missing or invalid selections make only that model unavailable, with empty
effective headers/options; other models still publish. The full view preserves
its source declarations, and a corrected catalog revision can restore readiness.
Explicit default/compact selections still require an available model.

Source cache files preserve complete catalog declarations, never effective
routes or readiness. The version-pinned full view includes resolved facts in
its private in-memory serialization. Changing credentials rebuilds setup facts
without rewriting an otherwise unchanged catalog cache.

The resolver applies:

- model-level `provider.api` before provider-level catalog `api` before the
  adapter's protocol default API;
- a provider-declared `adapter` from catalogs that are not models.dev records,
  such as local runtimes, which takes precedence
  over the `npm` map;
- a small maintained `npm`-to-protocol map, including the major native packages
  whose services expose one of the built-in wire protocols;
- environment availability rules;
- installed-adapter and local-probe state.

The resolved `_toolang.route.env` list is OR; a nested group is AND. An empty rule
requires no environment value. A models.dev source retains its raw flat `env`
list until setup infers the rule. During that inference, names ending in
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
are selected only at the call boundary; they are never stored in a record,
catalog JSON, hashes, or inspection output.

Catalog plugins own provider configuration. Core `[models.providers.*]` overrides
are rejected. Raw `_toolang` mappings from catalog JSON are not trusted runtime
facts. Setup resolves adapters and readiness; call assembly derives the endpoint,
headers and options from its pinned setup. JSON exports omit Toolang facts.

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

Adapters receive the effective connection, the resolved `Model`, and the
`ModelCall`. They translate
canonical messages and tools, normalize streaming, usage, cache, reasoning,
and audio meters, and preserve protocol state needed by later calls. For
example, the Generate Content adapter retains Gemini thought signatures in
provider state and restores them on subsequent tool-call turns. The Messages
adapter likewise preserves signed Anthropic thinking and redacted-thinking
blocks and replays them before the associated tool use.

Canonical model-call resource controls are `effort` and `max_output`:

```text
effort     = auto | LEVEL | TOKENS
max_output = auto | TOKENS
```

`auto` imposes no extra restriction: the model or provider applies its native
reasoning behavior or output allowance. `effort = TOKENS` is a reasoning-token
budget and is valid only where the model advertises budget support;
`max_output = TOKENS` caps one call's output and must exceed an explicit
reasoning budget. An omitted control inherits the value in effect, while `auto`
restores native behavior and cancels an inherited value. `effort = none`
disables reasoning, including for models that only advertise a toggle.

Catalog `reasoning_options` map to the canonical controls: an `effort` option
advertises `LEVEL`, a `budget_tokens` option advertises `TOKENS`, and a `toggle`
option advertises `none`. A toggle-only model therefore reports `none`, and
reasoning control applies whenever the model advertises any of the three. `auto`
is always available and never derives from the catalog.

The enumerations are evidence rather than authority: an unlisted effort level is
rejected locally only when the source marks its enumeration exhaustive;
otherwise it passes through and the provider decides. An output allowance of
`auto` resolves to the catalog maximum, or is omitted when the provider treats
omission as that same allowance.

Adapters translate `effort` and `budget_tokens` to their wire protocol and
reject unsupported or conflicting combinations. A provider-native reasoning
toggle is an adapter implementation detail, not a third canonical control:
`effort = none` becomes the adapter's disabled form. The Chat Completions adapter includes only small,
explicit dialect mappings for well-known compatible providers; unknown provider
extensions are not inferred.

An external adapter should contain no provider matching table. If a new npm
package needs to use it automatically, add that small mapping to the resolver;
users can also select the adapter explicitly in provider configuration.

## Local Providers

Ollama and llama.cpp are catalog plugins using the same `Provider` and `Model`
types as the static source. They publish what their endpoint reports and
publish nothing when it cannot be reached, so every local model that appears is
usable. An unreachable local runtime therefore has no provider row. Local models
have explicit zero API token prices; host compute cost is outside model token
accounting.

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
guest. Configure routes through the owning catalog plugin; core
`[models.providers.<name>]` overrides are not supported.

## Inspection and Export

The public resources are:

```text
too models [--all] [--query QUERY] [--json]
too providers [--all] [--json]
too catalogs
too adapters [--json]
```

`too models` shows ready, allowed models plus an `AVAILABLE` yes/no column.
`too providers` lists only providers with at least one such model, and its nested
model lists use the same scope. Add `--all` to either command to inspect the
complete directory, including unready and allow-excluded entries; `providers
--all` also includes empty providers. The `available` query field describes
readiness independently of allow membership.

`too models --all` and `too providers --all` show coarse unavailability reasons
from the route's missing fields. They do not identify individual missing
credentials or distinguish unknown adapters from uninstalled ones.

Providers show `ADAPTERS`, `DEFAULT API`, `ENV`, and `REASON`. Adapter names are
aggregated from the selected models; empty providers show their default adapter.
The API column marks model endpoint overrides. ENV shows the satisfied rule, or
catalog declarations when unavailable; its red styling indicates the overall
environment requirement is unmet, not that every displayed variable is missing.
A provider is available when at least one of its selected models is ready.

`too catalogs` lists installed model-catalog plugin entry points and their
`built-in` or `external` source. It does not load the plugins or describe the
merged catalog snapshot; use `too models` for that view.

`too models --query ... --json` emits another complete, deterministic,
models.dev-compatible catalog containing only selected models, including models
from local catalogs. It exports the same setup version used for selection without
re-reading the source. `too providers --json` follows the same default/`--all`
scope and preserves empty providers in the full view. Catalog inspection skips
validation of the configured default and compact model, so `--all` can diagnose
an unready choice; execution setup still validates those choices strictly.
Provider and model JSON never includes a Toolang-side fact or an unmodelled catalog field.

Queries use `PATTERN[field=value;...]`. Exact identity is `provider/model_id`;
model IDs may contain additional `/` characters. Catalog and runtime models
share query fields, including `family`, `reasoning`, `tool_call`, `temperature`,
`structured_output`, `modalities.input`, `status`, `route.provider`,
`route.adapter`, and `available`. Run `too query models` for the
complete contract.
Model-call parameters such as reasoning effort are structured request fields,
not query syntax.

## Runtime Configuration

Catalog plugins own provider routes. Root or agent configuration filters the
published models and selects an exact default:

```toml
[allow]
models = ["gateway/*"]

[default]
model = "gateway/chat effort=high"
```

`SetupWatcher` filters readiness and applies `allow.models` once, then publishes
the resulting `ModelCollection`; request and runnable policy can only narrow
that base.
`default.model` uses the same model body as invocation, Chat, and run-input
settings: an optional concrete ref followed by typed assignments. The current
assignment is `effort=LEVEL`, `effort=TOKENS`, `effort=auto`,
`max_output=TOKENS`, or `max_output=auto`. The effective
ref must be present in the Setup collection, and its parameters are validated
before Setup publication. An agent config may use a parameter-only body such
as `effort=high` to modify the inherited root default. Setup keeps an absent
configured model absent; Chat and Script surfaces use the first effective
collection model as their runtime fallback. `unset` explicitly selects no
model at the session or one-run layer for model-free execution.

The same body is accepted by `TOOLANG_DEFAULT_MODEL`, Agent and Chat startup
`--default model=BODY`, Script/rerun `--model BODY`, `/model BODY`, and
`:model BODY`. Multi-token CLI and environment values must be shell-quoted.
Configuration deliberately supports only the string form under `[default]`;
there is no `[default.model]` table. Legacy `none` values in Setup default
sources normalize to canonical `unset`.

`[models.providers.*]`, `[models].default`, and `[models.aliases.*]` are rejected.
Custom model identities and aliases will be supplied by a future custom catalog rather than
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

Model request objects use flat `reasoning` and `max_output` fields in memory,
while their existing serialized `parameters` envelope is preserved for run,
retry, and session data. This PR does not change the durable record schema.
Persisting effective call reasoning is deferred; current call records do not
store that field. Pricing source and revision continue to populate existing
accounting fields from the run's pinned `setup.catalog_sources` mapping.
