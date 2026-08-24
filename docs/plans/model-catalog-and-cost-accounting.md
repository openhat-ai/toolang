# Define Model Catalog and Cost Accounting

## Status

Approved for implementation on 2026-08-23.

## Goal

Make models.dev-compatible data the durable source of provider, model,
capability, and pricing knowledge while keeping runtime availability, protocol
execution, and cost accounting separate. Users must be able to update or
replace the catalog without a Toolang release, inspect its provenance, and
retain auditable usage and cost records.

## Success Criteria

- Toolang loads one complete models.dev-compatible `models.json` snapshot from
  an explicit, agent-home, root, or packaged source.
- `too models update` validates and atomically activates immutable catalog
  versions without changing the active file on failure.
- `ModelCatalog` exposes raw-schema `Provider` and `Model` values; adapters do
  not own model discovery, matching, availability, or pricing.
- Ollama and llama.cpp add currently discovered local models without writing
  them into the static catalog or inheriting same-name remote prices.
- Model filters can be exported as another valid, deterministic `models.json`.
- Durable accounting distinguishes cache, reasoning, audio, reported cost,
  estimated cost, pricing provenance, and partial coverage.
- Progress stays compact: `↑12.4k(65.3%) ↓2.3k ~$0.042137`.
- The default test suite stays offline and deterministic.

## Scope

In scope:

- catalog schemas, loading, validation, provenance, source precedence, update,
  filtering, export, inspection, and an in-process immutable snapshot;
- exact provider/model identity and centralized target resolution;
- plural model/provider/adapter CLI resources;
- configured remote availability and explicit local endpoint discovery;
- reasoning controls, normalized usage meters, models.dev pricing, reported and
  estimated cost, compact totals, and best-effort cost limits;
- one-cycle compatibility for current model CLI entry points and durable data.

Out of scope:

- automatic background catalog downloads or startup downloads;
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
- `ModelCatalog` provides providers and models. Implementations are
  `ModelsDevModels`, `OllamaModels`, `LlamaCppModels`, and
  `MergedModelCatalog`.
- `ModelTargetResolver` maps catalog `npm`/provider request-shape signals and
  explicit Toolang configuration to an installed adapter. External npm names
  are data only and never trigger installation or import.
- `ModelTarget` is the resolved runtime value containing model identity,
  adapter, endpoint, runtime credentials, headers, and options. Secrets are
  never catalog fields, persisted, or printed.
- `ModelAdapter` owns protocol invocation, streaming, and response/usage
  normalization only. There is no `ModelProvider`, `ProviderConnection`,
  catalog plugin, or adapter matching declaration.

The execution path is:

```text
MergedModelCatalog -> ModelTargetResolver -> ModelTarget -> ModelAdapter
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

`reasoning_options` is the model knowledge source for toggle, effort, and token
budget support. Runtime requests record the exact requested and selected values;
they do not create variants or guess a nearest effort/default. The recognized
effort vocabulary follows models.dev, including `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`, and `default` when advertised.

## Catalog Sources and Snapshot

Resolve one complete static catalog in this order:

1. global `--model-catalog PATH`;
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
path, SHA-256 revision, file identity, providers, and models. Parsing occurs
once and the snapshot is reused. A new setup, explicit update, symlink-target
change, or file identity change builds a new snapshot. Runs retain their
starting revision and never reprice history.

Ollama probes its configured/default endpoint's `/api/tags`; llama.cpp probes
`/v1/models`. Toolang probes only declared/default endpoints, uses short
timeouts, and never scans ports. Results live only in the setup snapshot. Calls
may coalesce an in-flight probe but there is no TTL, disk cache, cross-process
cache, last-good result, or stale fallback. A local-only model has partial
runtime knowledge and unknown price.

## Catalog Update and Export

Canonical update commands are:

```text
too models update --root [--url URL]
too models update --home [--url URL]
```

The default URL is `https://models.dev/api.json`. Exactly one destination is
required. Update downloads to a temporary file, validates the entire snapshot,
computes SHA-256, takes a destination lock, and then writes:

```text
models/models-YYYYMMDDTHHMMSSZ-<sha12>.json
models.json -> models/models-YYYYMMDDTHHMMSSZ-<sha12>.json
```

Timestamps use the successful UTC download time. Same-SHA content is a no-op.
The link switch is atomic and there is no `prev` link. Old immutable versions
remain. If an existing active `models.json` is a valid regular file, update
archives it under the same timestamp/SHA convention before installing the
managed relative link. A failure never changes the active catalog. Platforms
without symlink support fall back to an atomic regular-file replacement while
retaining the immutable version file.

`too models --filter SELECTOR --output PATH` and `--json` produce a complete,
valid catalog document: full provider fields, only selected nested models, and
no empty providers. Runtime-only adapter, availability, scope, and source facts
are excluded. Repeated filters are ORed; conditions within one selector retain
normal selector semantics. Output is deterministic, revalidated, atomically
written, and requires `--force` to replace a file. Strict export fails with a
clear list when selected local-only models cannot satisfy the schema.

## Availability, Resolution, and Filters

Static catalog knowledge remains inspectable even when unavailable. Remote
providers are selectable when explicit configuration or required environment
variables resolve an executable target; this means configured, not confirmed
account entitlement. Local models are selectable only when their endpoint
reports the exact ID.

Selectors use `PATTERN[field:value,...]`. Identity is
`provider/model_id`. Catalog filters directly expose fields such as `family`,
`reasoning`, `tool_call`, `temperature`, `structured_output`, `attachment`,
`modalities.input`, `modalities.output`, and `status`; runtime views add
`provider`, `adapter`, `scope`, and `available`. Arrays use membership. `false`
matches only explicit false, while missing values remain unknown. `tools` is a
one-cycle alias for `tool_call`.

Public commands are:

```text
too models [--filter SELECTOR] [--json] [--output PATH]
too models inspect [IDENTITY] [--available] [--json]
too models update --root|--home [--url URL]
too providers [--filter SELECTOR] [--json]
too adapters [--filter SELECTOR] [--json]
```

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
selected amount is estimated. Unknown cost is omitted. Model and run inspection
show exact meters, reasoning controls, pricing revision, matched tier/rates,
reported and estimated amounts, differences, and coverage.

## Compatibility and Migration

- Existing durable model steps decode as accounting version 0 with their old
  cost treated as an estimate of unknown revision.
- New records retain current summary token/price/cost projections for one
  compatibility cycle.
- Current aliases and authored selectors continue to resolve through
  `ModelTargetResolver`; legacy `tools` filters map to `tool_call` for one cycle.
- Existing singular CLI commands forward for one cycle.
- Existing provider model-list cache records stop being read or written and the
  `${TOOLANG_ROOT}/.setup/models/` data can be removed by a later explicit
  cleanup; implementation does not destructively delete user files.

## Implementation Touchpoints

- `src/toolang/base/types/model.py`, `run.py`, and protocol exports;
- new catalog-owned schema/import/update modules under
  `src/toolang/plugin/models/` and packaged `data/models.json`;
- `src/toolang/plugin/models/resolution.py`, adapters, local discovery, views,
  configuration, and plugin loading;
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
   SHA provenance, parse reuse, and invalidation on file/link change.
3. Prove atomic update, same-SHA no-op, regular-file archival, immutable version
   naming, failure recovery, lock behavior, and root/home isolation.
4. Prove exact provider/model identity, nested model IDs, adapter selection,
   missing-adapter failure, secret redaction, and no npm auto-loading.
5. Prove configured remote availability and Ollama/llama.cpp discovery,
   endpoint failure, no port scan, no stale result, and unpriced local-only models.
6. Round-trip filtered output through the importer and cover OR filters,
   nested fields, unknown booleans, deterministic output, overwrite protection,
   and local-only export failure.
7. Normalize cached input, cache writes, visible output, reasoning/thinking,
   audio, inclusive provider totals, and streaming without double counting.
8. Estimate flat and tiered models.dev prices, prefer reported USD, preserve
   partial/unknown cost, retain historical applied rates, and enforce the
   best-effort completed-call limit.
9. Render cache ratio, reported/estimated marker, and unknown cost exactly;
   expose full details through model and run inspection.
10. Keep compatibility decoders and CLI aliases working for one cycle and keep
    all default tests offline and deterministic.
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
