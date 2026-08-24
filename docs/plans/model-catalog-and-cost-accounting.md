# Define Model Catalog and Cost Accounting

## Status

Approved for implementation on 2026-08-24.

## Goal

Make models.dev-compatible data the durable model catalog for provider and model
metadata, capabilities, and pricing while keeping runtime availability,
protocol execution, and cost accounting separate. Users must be able to replace
the catalog without a Toolang release and retain auditable usage and cost
records.

## Success Criteria

- Toolang loads one complete models.dev-compatible `models.json` snapshot from
  an explicit, agent-home, root, or packaged source.
- Explicit, agent-home, and root catalog files allow replacement without a
  Toolang release.
- Catalog plugins expose raw-schema `Provider` and `Model` values; adapters do
  not own model discovery, matching, availability, or pricing.
- Every loaded provider is enriched once as
  `resolved: {adapter, api, env, ready}`. Inspection, selection, and calls
  consume only that value; catalog JSON and hashes remain raw.
- Ollama and llama.cpp add currently discovered local models without writing
  them into the static catalog or inheriting same-name remote prices.
- Model filters can be exported as another valid, deterministic `models.json`.
- Durable accounting distinguishes cache, reasoning, audio, reported cost,
  estimated cost, pricing provenance, and partial coverage.
- Progress stays compact: `↑12.4k(65.3%) ↓2.3k ~$0.042137`.
- The default test suite stays offline and deterministic.

## Scope

In scope:

- catalog schemas, loading, validation, provenance, source precedence,
  filtering, JSON export, table display, and an in-process immutable snapshot;
- exact provider/model identity and centralized target resolution;
- plural model/provider/adapter CLI resources;
- configured remote availability and explicit local endpoint discovery;
- reasoning controls, normalized usage meters, models.dev pricing, reported and
  estimated cost, compact totals, and best-effort cost limits;
- compatibility decoding for existing durable accounting data.

Out of scope:

- built-in catalog download/update commands, managed catalog versions, automatic
  background downloads, or startup downloads;
- provider plugins, generic capability or variant wrappers, and automatic npm
  installation from catalog metadata;
- persistent discovery/result caches, local port scanning, stale local results,
  catalog merging across precedence levels, or local-to-remote price inference;
- invoice reconciliation, currency conversion, account balances, automatic
  version pruning/rollback commands, and guaranteed pre-call cost reservation.

## Vocabulary and Boundaries

- `Provider` and `Model` mirror the models.dev `api.json` provider and model
  records. `(provider_id, model_id)` is the only exact identity. The first `/`
  separates provider from model; the remaining model ID may contain `/`.
- Models.dev fields remain top-level. Missing optional booleans mean unknown;
  there is no `capabilities` object and no Toolang `variants` abstraction.
- `ModelCatalog` is the catalog plugin protocol that provides providers and
  models. Implementations are
  `ModelsDevModels`, `OllamaModels`, `LlamaCppModels`, and
  `MergedModelCatalog`.
- A thin loader-owned resolver maps catalog `npm`, `api`, and `env` plus
  explicit Toolang configuration to `Provider.resolved` exactly once. External
  npm names are data only and never trigger installation or import.
- `ModelTarget` is the resolved runtime value containing model identity,
  adapter, API base URL, runtime credentials, headers, and options. Secrets are
  never catalog fields, persisted, or printed.
- `ModelAdapter` owns its protocol default API, invocation, streaming, and
  response/usage normalization only. There is no `ModelProvider`,
  `ProviderConnection`, or adapter matching declaration.

The loading and execution path is:

```text
Catalog plugins -> MergedModelCatalog -> Provider resolver -> frozen snapshot
frozen snapshot -> ModelTarget -> ModelAdapter
```

## Models.dev Schema

The importer consumes the raw provider map and nested model map. It preserves
the current upstream fields, including provider `id`, `env`, `npm`, `api`,
`name`, `doc`, and `models`, and model `id`, `name`, `description`, `family`,
`attachment`, `reasoning`, `reasoning_options`, `tool_call`, `interleaved`,
`structured_output`, `temperature`, `knowledge`, `release_date`,
`last_updated`, `modalities`, `open_weights`, `limit`, `status`, `experimental`,
`provider`, and `cost`.

Unknown additive fields survive filtered export. Consumed fields are validated;
an incompatible value rejects the complete snapshot. Prices parse as `Decimal`
and serialize as JSON numbers without passing through binary floating point.
The importer also enforces a configurable maximum size, nested key/ID
consistency, non-negative prices, and non-negative limits.

`reasoning_options` is the model catalog field for toggle, effort, and token
budget support. Runtime requests record the exact requested and selected values;
they do not create variants or guess a nearest effort/default. The recognized
effort vocabulary follows models.dev, including `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`, and `default` when advertised.

## Catalog Sources and Snapshot

Resolve one complete static catalog in this order:

1. global `--models PATH`;
2. `TOOLANG_MODEL_CATALOG`;
3. the active agent home `models.json`;
4. `${TOOLANG_ROOT}/models.json`;
5. the packaged `models.json`.

Explicit paths resolve at the CLI boundary and fail without fallback when
missing or invalid. Higher-priority files fully shadow lower-priority files;
they never merge. A regular file or symlink is valid. The packaged fallback is
a lightweight valid snapshot containing representative, non-deprecated models
for `openai`, `anthropic`, `google`, `deepseek`, and `openrouter`; it is not
copied into root on first setup.

Each setup/process holds one immutable `ModelCatalogSnapshot`, including source
path, SHA-256 revision, file identity, providers, and models. Static parsing is
reused until the selected file identity changes. Dynamic catalogs are reprobed
on setup refresh; unchanged merged results reuse the current setup. Runs retain
their starting source revision and never reprice history.

`Provider` retains the raw models.dev fields and adds one optional runtime-only
default route:

```text
resolved: {adapter: string?, api: string?, env: (string | string[])[], ready: bool}
```

Each `Model` also receives a runtime-only `{adapter, api, ready}` route so
model-level provider overrides can select a different protocol without changing
raw JSON. The outer `env` list is OR; a nested list is AND. An empty list means no
environment requirement. Default inference treats names ending in `_API_KEY`,
`_PAT`, or `_TOKEN` as alternatives and requires every other name, distributing
the common requirements into each alternative. Small provider-specific rules
cover credential schemes such as Amazon Bedrock. A provider is ready only when
its adapter is installed, its API base is concrete after configuration,
catalog, and adapter-default precedence, and one environment alternative is
satisfied. No call-time fallback or re-resolution is permitted.
The raw `Provider.api` remains unchanged; `Provider.resolved.api` is the
effective API base and becomes `ModelTarget.base_url` only at the call boundary.

Ollama probes its configured/default endpoint's `/api/tags` and enriches each
listed model through `/api/show`. llama.cpp combines `/v1/models` metadata with
`/props` for the single active model. Toolang maps reported context, output,
modalities, capabilities, family, and concise runtime details without inferring
missing model behavior from its name. It probes only declared/default endpoints,
uses short timeouts, and never scans ports. Results live only in the setup
snapshot. Calls may coalesce an in-flight probe but there is no TTL, disk cache,
cross-process cache, last-good result, or stale fallback. Local-only models have
explicit zero API token prices; host compute costs remain outside model token
accounting.

## Catalog Replacement and Export

There is no catalog update CLI command. Users replace an explicit, agent-home,
or root `models.json` through their preferred external download or file
management workflow. Toolang validates the selected complete snapshot when it
loads it but does not download, archive, version, or replace catalog files.

`too models --filter SELECTOR --json` writes a complete, valid catalog document
to stdout: full provider fields, only selected nested models, and no empty
providers. Runtime-only adapter, availability, scope, and source facts are
excluded. Repeated filters are ORed; conditions within one selector retain
normal selector semantics. Output is deterministic. Strict export fails with a
clear list when selected local-only models cannot satisfy the schema. There are
no `--output` or `--force` options.

## Availability, Resolution, and Filters

Static model catalog entries remain inspectable even when unavailable. Remote
providers are selectable when explicit configuration or required environment
variables resolve an executable target; this means configured, not confirmed
account entitlement. Catalog-list readiness uses `Provider.resolved.ready`.
Model tables expose only
`AVAILABLE` as `yes` or `no`; provider diagnostics own the missing configuration
details. Local models are selectable and displayed only when their endpoint
reports the exact ID and their runtime target is available.

Selectors use `PATTERN[field:value,...]`. Identity is
`provider/model_id`. Catalog filters directly expose fields such as `family`,
`reasoning`, `tool_call`, `temperature`, `structured_output`, `attachment`,
`modalities.input`, `modalities.output`, and `status`; runtime views add
`provider`, `adapter`, `scope`, and `available`. Arrays use membership. `false`
matches only explicit false, while missing values remain unknown. `tools` is a
one-cycle alias for `tool_call`.

Public commands are:

```text
too models [--filter SELECTOR] [--json]
too providers [--filter SELECTOR] [--json]
too adapters [--filter SELECTOR] [--json]
```

`too models` is a leaf command and accepts no subcommands. Human-readable text
uses `model catalog` consistently. The human-readable `models` table splits
model details into `CONTEXT`, `OUTPUT`, `INPUT`, `CAPABILITY`, and
`PRICE ($/1M)`. Modalities and
capabilities are comma-separated and use models.dev field names such as
`tool_call`; price cells contain only `$input / $output` base rates because the
header owns the per-million unit. Context and output sizes use full integers
with underscore digit grouping, such as `1_048_576`, so displayed values remain
copyable numeric literals. Every numeric price is rounded and padded to two
decimal places. The context, output, and price columns are right-aligned.

The model-list summary names every catalog component and its displayed model
count in the form
`18 models from 3 catalogs: models.dev 15, ollama 2, llama_cpp 1`. It does not
repeat revisions, APIs, or runtime status.

The provider table orders its diagnostic columns as `ADAPTERS`, `API`, and
`ENV`. All three columns render only `Provider.resolved`. Unavailable adapter
and API values are dimmed. The outer environment list uses comma for OR
and nested groups use ` + ` for AND; separators remain unstyled.
Offline local providers remain listed with `AVAILABLE` equal to `0` and a dimmed
API. Online providers use `n/m`. Provider and model `--json` output
excludes `resolved` and remains a valid raw models.dev-compatible document.
Anthropic, OpenAI, and Google APIs are made concrete from their installed
adapters during provider resolution.
The provider footer uses the same source-count form as the model footer, for
example `7 providers from 3 catalogs: models.dev 5, ollama 1, llama_cpp 1`.

The singular `too model` group is removed. The public catalog commands are
`too models`, `too providers`, and `too adapters`.

## Usage, Pricing, and Durable Accounting

Add one versioned structured accounting value to completed model-step records.
It contains:

- inclusive input and output token totals;
- mutually exclusive `input.uncached`, `input.cache_read`, and
  `input.cache_write` meters;
- `output.visible`, `output.reasoning`, input/output audio, request, and
  namespaced provider meters with quantity and unit;
- requested, selected, and provider-reported reasoning controls independently
  from observed reasoning usage;
- price source, catalog revision, selected plan/tier/mode, applied conditions,
  and per-meter rate/amount lines;
- provider-reported cost and estimated cost side by side, selected source,
  currency, and complete/partial coverage.

Decimal quantities, rates, and money persist as decimal text. Missing provider
values stay unknown, not zero. Inclusive totals and component meters must not
double count cache or reasoning. Streaming and non-streaming results normalize
to the same final accounting.

Models.dev prices are USD per one million units. The estimator supports input,
output, reasoning, cache read/write, input/output audio, context tiers, and
recognized mode prices. Provider-reported cost wins; otherwise the captured
catalog revision estimates known meters. Unsupported service-tier, regional,
time-band, or other dimensions remain partial unless the provider reports the
amount. Applied lines can represent those dimensions later without changing
the durable record schema.

`limits.cost` sums reported USD first and otherwise the available estimate. It
is checked after each completed call, may allow one call to cross the limit,
and marks aggregates partial when calls are unknown, incomplete, or non-USD.

Compact progress uses:

```text
↑12.4k(65.3%) ↓2.3k ~$0.042137
```

The percentage is shown only for a complete positive cache-read ratio. `$`
means every selected amount is provider-reported USD; `~$` means at least one
selected amount is estimated. Unknown cost is omitted. Run inspection shows
exact meters, reasoning controls, pricing revision, matched tier/rates, reported
and estimated amounts, differences, and coverage.

## Compatibility and Migration

- Existing durable model steps decode as accounting version 0 with their old
  cost treated as an estimate of unknown revision.
- New records retain current summary token/price/cost projections for one
  compatibility cycle.
- Current configured aliases and authored selectors continue to resolve through
  `ModelTargetResolver`.
- Singular model/provider inspection commands are removed without forwarding
  aliases; the plural resource commands are the only public interface.
- Existing provider model-list cache records stop being read or written and the
  `${TOOLANG_ROOT}/.setup/models/` data can be removed by a later explicit
  cleanup; implementation does not destructively delete user files.

## Implementation Touchpoints

- `src/toolang/base/types/model.py`, `run.py`, and protocol exports;
- new catalog-owned schema/import modules under
  `src/toolang/plugin/models/` and packaged `data/models.json`;
- `src/toolang/plugin/models/resolution.py`, catalog and adapter plugins, local
  discovery, views, configuration, and plugin loading;
- `src/toolang/setup/models.py`, setup types/configuration, and watchers;
- `src/toolang/execution/records.py`, schemas, model steps, limits, inspection,
  and API projections;
- `src/toolang/cli/common/` progress formatting and
  `src/toolang/cli/toolang/commands/` resource commands;
- `pyproject.toml`, README command examples, and offline fixtures/tests.

## Acceptance Tests

1. Import representative upstream data, preserve unknown fields, reject an
   incompatible complete snapshot, and round-trip Decimal prices deterministically.
2. Prove source precedence, explicit-source failure, regular/symlink loading,
   SHA provenance, parse reuse, invalidation on file/link change, and root/home
   isolation.
3. Prove exact provider/model identity, nested model IDs, one-time provider
   resolution, API precedence, env OR/AND inference and overrides,
   missing-adapter failure, secret redaction, raw-only JSON, and no npm
   auto-loading.
4. Prove configured remote availability and enriched Ollama/llama.cpp discovery,
   endpoint failure, no port scan, no stale result, and zero-priced local-only
   models.
5. Round-trip filtered JSON output through the importer and cover OR filters,
   nested fields, unknown booleans, deterministic output, and local-only export
   failure.
6. Cover Chat Completions, Responses, Messages, and Generate Content adapters;
   normalize cached input, cache writes, visible output, reasoning/thinking,
   audio, inclusive provider totals, and streaming without double counting.
7. Estimate flat and tiered models.dev prices, prefer reported USD, preserve
   partial/unknown cost, retain historical applied rates, and enforce the
   best-effort completed-call limit.
8. Render cache ratio, reported/estimated marker, and unknown cost exactly;
   expose full details through run inspection.
9. Keep durable compatibility decoders working and all default tests offline
    and deterministic; prove removed singular CLI resources do not resolve.
11. Pass `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest`.

## Risks

- models.dev can lag provider billing. Revision provenance, applied rates,
  visible estimates, and reported-cost precedence mitigate but do not reconcile
  invoices.
- Upstream has no explicit schema version. The importer must tolerate additive
  fields while rejecting incompatible consumed fields clearly.
- Local probes can delay setup. Short timeouts, declared endpoints, and
  in-flight coalescing bound the impact.
- Some providers expose inclusive reasoning/cache totals or omit components.
  Normalization must prefer partial coverage over invented precision.
- Filesystem link and replacement behavior differs by platform. Update tests
  must cover the regular-file fallback and never risk the active catalog.

## Open Questions

None.
