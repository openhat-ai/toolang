# Model plugins

## Goal and scope

This PR simplifies model types around catalog records, makes agent setup own
model caching and versioned publication, and separates catalog and adapter
plugins. Provider plugins and model aliases are not supported. Records and their
existing serialized formats stay unchanged; any record redesign is a follow-up.

One place defines what the model plugins are, what data they produce, and how
that data reaches the CLI and the executor.

- `plugin/catalogs/` holds one directory or module per catalog plugin;
- `plugin/adapters/` holds one module per adapter plugin;
- a catalog plugin produces immutable snapshots only; combining, resolving,
  projecting, and caching belong to `toolang.setup`;
- `plugin/models/` temporarily keeps the model runtime/query layer that the
  layout change does not move;
- the setup publishes one model state that both CLI inspection and the executor
  read.

This document records the model layout, data model, and flow.
[Model route publication](model-route-publication.md) specifies the approved
source-cache and effective-route boundary in detail. It supersedes the catalog placement sketched in `plugins.md` and
`models.md`, and the earlier `model-catalog-plugin-layout.md`.

## Layout

```text
src/toolang/
├── common/
│   ├── cache.py                    # cache document envelope, digest, canonical value, secret scan
│   ├── files.py                    # atomic writes and file locks
│   └── json.py                     # deterministic msgspec JSON dumps
├── plugin/
│   ├── adapters/                   # one module per adapter plugin
│   │   ├── loading.py              # load_model_adapters
│   │   ├── _structured_output.py
│   │   ├── _usage.py
│   │   ├── chat_completions.py
│   │   ├── generate_content.py
│   │   ├── messages.py
│   │   └── responses.py
│   ├── catalogs/                   # one module or subpackage per catalog plugin
│   │   ├── loading.py              # load_model_catalogs
│   │   ├── _local.py               # helpers shared by the two local catalogs
│   │   ├── models_dev/
│   │   │   ├── catalog.py          # ModelsDevModelCatalog, capture, reader, factory
│   │   │   ├── parsing.py          # models.dev-compatible record parsing and validation
│   │   │   ├── path.py             # MODEL_CATALOG_ENV, packaged catalog, path precedence
│   │   │   └── data/catalog.json   # packaged catalog
│   │   ├── ollama.py
│   │   └── llama_cpp.py
│   ├── models/                     # temporary: model runtime/query layer, moved later
│   ├── channels/
│   ├── sandboxes/
│   ├── toolsets/
│   ├── values.py                   # shared readers for loosely typed plugin payloads
│   ├── config.py
│   └── loading.py
└── setup/
    ├── cache.py                    # per-catalog model cache
    ├── catalog.py                  # snapshot merge
    ├── models.py                   # ordering, compact selection, catalog projection
    ├── watcher.py                  # installed setup publication
    ├── config.py
    ├── tools.py
    ├── types.py
    └── errors.py
```

`plugin/models/` no longer holds `catalog.py`, `local.py`, `cache.py`,
`loading.py`, `adapters/`, or `data/`. It is not a plugin family: it has no
entry point, and it does not import `plugin/adapters` or `plugin/catalogs`.

### Cache ownership

| Concern | Owner |
| --- | --- |
| Per-catalog model cache | `toolang/setup/cache.py` |
| Local catalog state (`ollama`, `llama_cpp`) | `toolang/setup/cache.py`, with its own detection stamp |
| Cache document envelope and primitives | `toolang/common/cache.py` |
| Deterministic msgspec JSON | `toolang/common/json.py` |

Setup persists one complete file per catalog. The models.dev source is captured
once per changed file observation, then decoded from a matching source cache or
parsed from the captured bytes. There is no separate persisted filtered view.

`CACHE_SCHEMA` is 10. Older source caches are rebuilt; source documents retain
catalog declarations and ownership, never effective routes or readiness.
Imported flat `env` lists are resolved by setup rather than treated as explicit
plugin OR alternatives.
`Model.provider` is a fixed `ModelProvider` declaration with typed
`ProviderToolang` metadata. Imported raw `_toolang` objects are discarded; plugins
can construct trusted declarations. msgspec reuses these dataclasses directly.
Cache reads use finite float prices and both boolean and object `interleaved`
values. A skipped probe write uses a content revision instead of the old file's
stamp.

### Entry points

```toml
[project.entry-points."toolang.model_adapter"]
chat_completions = "toolang.plugin.adapters.chat_completions:create_model_adapter"
generate_content = "toolang.plugin.adapters.generate_content:create_model_adapter"
messages = "toolang.plugin.adapters.messages:create_model_adapter"
responses = "toolang.plugin.adapters.responses:create_model_adapter"

[project.entry-points."toolang.model_catalog"]
models_dev = "toolang.plugin.catalogs.models_dev.catalog:create_models_dev_model_catalog"
ollama = "toolang.plugin.catalogs.ollama:create_ollama_model_catalog"
llama_cpp = "toolang.plugin.catalogs.llama_cpp:create_llama_cpp_model_catalog"
```

Entry-point names are unchanged, so configured plugin tables and
`too catalogs`/`too adapters` keep working.

### Layout decisions

- `plugin/models/loading.py` splits per family, matching
  `channels/loading.py`, `sandboxes/loading.py`, and `toolsets/loading.py`.
- Catalogs follow the adapters convention: a plugin per module or subpackage,
  shared helpers as a leading-underscore module. No `common/` directory.
- `models_dev` is a subpackage: it owns the packaged data file and the catalog
  path precedence in addition to its reader.
- `models.dev`-only parsing lives in its subpackage. `Provider`, `Model`, and
  `ModelCatalogSnapshot` stay in `toolang.base.types.model`, shared by every
  catalog and adapter plugin.
- Combining snapshots and projecting to runtime values belong to
  `toolang.setup`.
- No compatibility shims remain: importers, tests, and documentation moved
  together.
- Local probe failures are classified: unreachable logs at debug, invalid
  payloads log a warning, and both still produce an offline snapshot.
- `@ai-sdk/openai-compatible` is models.dev's adapter-inference vocabulary.
  Sources that are not models.dev records declare their protocol through
  `ProviderToolang.adapter` instead of fabricating an npm package.

## Data model

### Type roles

| Type | Role | Produced by | Read by |
| --- | --- | --- | --- |
| `Provider` | one catalog definition; resolution fills its `_toolang` | catalog plugin, then setup | inspection |
| `Model` | one catalog definition; resolution fills its `_toolang` | catalog plugin, then setup | inspection, executor, adapter |
| `ModelCatalogSnapshot` | one immutable `{providers, models, revision, source, local}` | catalog plugin, merge | setup |
| `ModelCollection` | effective selection index over `Model` | setup | inspection, executor |
| `ModelAdapter` | protocol implementation | adapter plugin | setup, executor |
| `ModelRoute` | effective adapter, API, environment rule, headers, options | setup publication | CLI, executor, adapter via Model |
| `ModelOverride` | sparse authored model operation | config/env/CLI/chat parsing | setup, execution policy |
| `ModelRequest` | one run's concrete model demand | policy composition | executor |
| `Reasoning` | one reasoning control, `{effort | budget_tokens}` | request parsing, call assembly | setup validation, adapter |
| `ModelCall` | one call: content plus effective controls | execution assembly | adapter, durable record |
| `ModelCallResult`, `ModelUsage` | one call's outcome and meters | adapter | execution, accounting |

`ModelInfo`, `ModelTarget`, `ModelEntry`, `ModelParameters`, `ModelAlias`, and
`ProviderConfig` are removed. There is no `ResolvedProvider` or `ResolvedModel`:
resolution returns another instance of the same definition whose `_toolang`
sub-record holds the Toolang-side facts. The query engine keeps one internal
flat row type for schema columns; it is not a second model state.

### Model

Catalog facts, verbatim and exportable: `id`, `name`, `description`, `family`,
`attachment`, `reasoning`, `reasoning_options`, `tool_call`, `interleaved`,
`structured_output`, `temperature`, `knowledge`, `release_date`, `last_updated`,
`modalities`, `open_weights`, `limit`, `status`, `experimental`, `provider`,
and `cost`.

The provider id is not a field: `Model._toolang.provider` holds it, and
`identity` and `ref` derive from it. `local` is not model data and
`catalog`/`catalog_revision` are not carried per record; see *Data decisions* 10
and 14.

A resolved model is an *instance*, not a field: the setup produces another
`Model` whose single `_toolang` sub-record (`ModelToolang`) carries `ready`,
the owning `provider`, and its effective `route`. The type is the same, so a field keeps one name across
the catalog instance and the resolved instance, and nothing in the record points
at the resolved instance.

The request and the call do not need `adapter`, `api`, or `ready`: a request
names the model by `ref` and carries parameters, and the call carries content
plus effective controls. The effective connection an adapter must use is
published once as `Model._toolang.route` and consumed without re-resolution.

`ready` is setup readiness (`ModelToolang.ready`), omitted from source caches. `available` is not a
persisted field: it is the display projection of readiness on a query row
(`ModelQueryView.available`) and in CLI output. `to_data()` emits only the
catalog facts, so `too models --json` stays a raw catalog export and never leaks
a resolved route, a secret, or a derived value.

The row projection that `ModelInfo` used to carry dissolves into these fields:
its `family`, `limit`, `cost`, `modalities`, `status`, `reasoning`,
`reasoning_options`, `tool_call`, `temperature`, `structured_output`,
`attachment`, `open_weights`, `release_date`, `last_updated`, `experimental`,
and `provider` are already catalog fields, and its
`resolved_api`/`resolved_ready` are the resolved route facts. Per-million prices
are derived from `Model.cost` on read instead of stored again.

### Provider

One catalog definition; resolution returns another instance of the same type
whose single `_toolang` sub-record (`ProviderToolang`) holds the Toolang-side
facts, with no `resolved` field.

External models.dev provider records contain `id`, `name`, `env`, `npm`, `api`,
`doc`, and nested `models`. Internal Provider records omit `models`: snapshots
and setup hold providers and models separately, joined by
`Model._toolang.provider`. Source caches and private full-view payloads store
each model only once. Parsing flattens either supported external format; export
rebuilds the nested models object. Provider summaries group the selected models
by ownership when needed, without retaining a second model collection.

The resolved instance carries `ProviderToolang{env, adapter, route}`. The first
two fields preserve trusted catalog declarations; `route` carries effective
connection facts. Catalog plugins supply provider routes; core provider
overrides are rejected. `Provider.api` keeps the catalog value; effective API
resolution happens at setup publication.

### Request headers and options

Effective `headers` and `options` are call-time request data that every built-in
adapter sends. Their raw source blocks remain catalog data.

| What | Read by |
| --- | --- |
| `headers` | `chat_completions` and `responses` as client default headers; `messages` and `generate_content` merged into the raw request headers |
| `options` | all four adapters, merged into the provider request body, and read for `audio`, `modalities`, and `max_completion_tokens` |

They come from model-level catalog provider blocks, the built-in OpenRouter
header defaults (`PROVIDER_CONVENTIONS`), and an
advertised `experimental.modes.<mode>` body and headers. Resolution merges them
per model into the published `ModelRoute`. The merged values stay in setup
memory; source caches and exports preserve only the raw catalog blocks.

`Provider` is the catalog group's state: the setup publishes it and
`too providers` renders it. It is not execution input.

### Fields that are not model data

These do not belong to `Model`, `ModelRequest`, or `ModelCall`, and are not
reintroduced under another name:

| Field | Where it comes from | Resolution |
| --- | --- | --- |
| `scope` | not authored model data | query vocabulary retains the field, but current rows do not populate it |
| `tags` | no owner after `ModelAlias` was removed | dropped; the query row leaves `tags` empty |
| `selectors` | removed | the query identity is `provider/model` (`IdentitySpec`) |
| `streaming` | fixed `_MODEL_STREAMING = True` in the executor | not model data; whether to stream is an execution and adapter decision |
| `available` | projected from `ModelToolang.ready` | a display/query concept only; never persisted |
| `api_key` | selected from the environment | never stored on a record; the adapter reads the credential names a provider declares from the environment it is given |

### Field collisions and how they resolve

Two questions stay separate: what the catalog says, and what a call must use.
A catalog value stays on the record; the effective value is either a Toolang-side
fact on `_toolang.route` or a per-call control. No second name is invented and no
record points at the other instance.

| Name | Catalog instance | Effective value |
| --- | --- | --- |
| `api` | provider/model catalog API | setup expands templates into `route.api`; source fields stay unchanged |
| `env` | `Provider.env` or a trusted plugin rule | `route.env`, the satisfied rule; None means unmet, () means not required |
| `adapter` | npm/shape metadata or a trusted plugin declaration | `route.adapter`, the installed adapter or None |
| `headers`, `options` | model catalog provider blocks and built-in conventions | merged into the published `ModelRoute` |
| `ready` | unset | `ModelToolang.ready`, derived from the three non-None route fields |
| `available` | derived | display-only projection of `ready`; never persisted |
| `local` | `ModelCatalogSnapshot.local` | the catalog's own property; a derived provider index, never a record field |

Catalog snapshots declare locality. The current merged query view does not
publish that fact; neither `Model` nor `ProviderToolang` carries a `local` field.

Export uses the catalog instances, so `--json` stays a raw catalog projection
and never shows an effective route. Inspection keeps the merged catalog
instances it loaded and renders selection and availability from the resolved
ones.

### Which api a call uses

Setup picks one effective API from the model override, provider catalog API,
and adapter default, in that order. It expands templates into `route.api`
without writing back onto the source record. `ModelAdapter.default_api` is
resolution input only; it is never consulted while building a request.

### Reasoning: capability, demand, effective control

Reasoning holds three different things in three places. It is not one value that
moves.

| What | Where | Meaning |
| --- | --- | --- |
| Capability | `Model.reasoning`, `Model.reasoning_options` | what this model can do; the catalog knows it |
| Demand | `ModelRequest.reasoning` | what this run asks for |
| Effective control | `ModelCall.reasoning` | what this call uses and what the adapter sees |

Effort levels are provider-defined. The only source of truth is the model's
catalog `reasoning_options`, which declares `effort`, `budget_tokens`, or
`toggle` options with their `values` and optional `exhaustive: true`.

Toolang owns no level vocabulary and keeps no normalized `reasoning_controls`
field or `ReasoningControls` type. `plugin/models/resolution.py` derives what it
needs per call:

```python
model_reasoning_controls(model)        # () or the verbatim reasoning_options
model_reasoning_efforts(model)         # advertised effort levels, catalog order
model_reasoning_effort_applicable(model)
```

- `Model.reasoning` stays the coarse catalog boolean; `Model.reasoning_options`
  stays verbatim and stays exportable. Nothing is copied onto `Model`.
- `Reasoning{effort: str | None, budget_tokens: int | None}` is one shape for
  both the demand and the effective control: a string is a level, an integer
  budget uses `budget_tokens`, and `auto` normalizes to `None` (provider
  default).
- Validation happens once, when a request becomes a call: reject a level only
  when `exhaustive` is set and the level is not advertised; reject a budget when
  no `budget_tokens` option exists; reject any control when the model advertises
  none. Otherwise pass the value through and let the provider decide.
- `none` is the only Toolang-defined level; the adapter maps it to its
  protocol-specific disabled form. Display candidates are exactly the advertised
  effort levels plus `none`; no hardcoded allowlist.

`ReasoningEffort` (the closed literal), its `_REASONING_EFFORTS` set, and the
duplicated allowlists are removed: `ModelEffort = str | int | Literal["auto"]`.

### max_output: capability, demand, effective allowance

Capability is catalog data and keeps its catalog name: `Model.limit["output"]`.
It is not renamed or re-wrapped.

The two Toolang-owned values are **not the same thing**, so they keep distinct
names:

| Value | Type | Meaning | `None` means |
| --- | --- | --- | --- |
| `ModelRequest.max_output` | `int \| None` | the run's demand, authored on a surface and stable for the whole run | `auto`: impose no demand |
| `ModelCall.max_output_tokens` | `int \| None` | the allowance this call actually applies | the provider applies its native maximum |

They differ for three reasons:

- the demand is authored policy that survives from the surface into the run
  record, where it can be inherited, overridden, or left to `auto`;
- the allowance is derived per call from the demand, the model's catalog
  capability, and the remaining input window, so it can differ between steps and
  is usually concrete even when the demand was `auto`;
- only the call allowance reaches the provider and the durable call record.

One function derives the allowance from `(demand, model, remaining window)`, and
it runs only while assembling the call. `ModelTarget.max_output` and
`ModelInfo.max_output_tokens`, the previous middle copies, are deleted. The
invariant that an explicit allowance must exceed an explicit reasoning budget is
checked there.

`ModelOverride.max_output: int | "auto" | None` stays the sparse authored form:
`auto` clears an inherited demand and `None` leaves it untouched.

### Requests

```python
class ModelOverride:   # sparse, authored on one surface
    identity: str | None       # exact ref | "default" | "unset"
    effort: str | int | "auto" | None
    max_output: int | "auto" | None

class Reasoning:
    effort: str | None
    budget_tokens: int | None

class ModelRequest:    # concrete, one run
    ref: str
    reasoning: Reasoning | None
    max_output: int | None
```

`apply(override, base) -> ModelRequest | None` and
`compose(overrides) -> ModelOverride` keep their current semantics.
`ModelParameters` is removed; its fields are `ModelRequest` fields.

### Call

```python
class ModelCall:
    instructions: str
    messages: list[Message]
    tools: tuple[ToolDefinition, ...]
    output_schema: dict | None
    continuation: ModelContinuation | None
    reasoning: Reasoning | None
    max_output_tokens: int | None
```

The call carries content plus the effective controls. It never carries model
identity or selection. An adapter receives `(Model, ModelCall)` and explicit environment values.
Durable call reasoning is deferred to the separate records follow-up.

`ModelCall.reasoning` is the validated, effective control for this call and uses
the same `Reasoning` shape as the request demand. It replaces the
`ModelTarget.reasoning` mapping that sat on a deleted type. The model's
advertised options are not copied here: the adapter reads capability from
`Model` and the effective control from `ModelCall`.

### Flow

```text
catalog plugin  -> ModelCatalogSnapshot{providers: Provider[], models: Model[]}   raw
adapter plugin  -> ModelAdapter
toolset plugin  -> Toolset -> Tool[]                                             unchanged
                        |
                  AgentSetup (one per process)
                    resolve  adapter / env / ready
                    derive   ref / identity, query rows
                    index    ModelCollection (effective, allow-filtered)
                    publish  providers, models (all), adapters (+source), tools,
                             defaults, limits, compact_model, envs, environment
                    cache    internal; first build may be slower
                        |
        +---------------+----------------+
   CLI inspection                       executor
   too models / providers / adapters    ModelRequest -> Model -> ModelCall -> adapter
```

- `too models` renders the published models, with availability from
  `Model._toolang.ready`.
- `too providers` renders the published providers plus readiness.
- `too adapters` renders published adapters with `AgentSetup.adapter_sources`,
  an immutable mapping captured by setup alongside installed-plugin provenance.
- No command scans entry points, re-resolves environment rules, or re-projects a
  catalog on its own. Inspection without a running agent builds the same setup
  object once and reads it.

### Setup caching

The cache is internal to the setup. Its contract is only:

- every group's state is readable after one build;
- the first build of a process may be slow;
- a cache miss, a corrupt entry, a half-written entry, or a failed write never
  changes the result, only the cost;
- every source's records are persisted with a per-source revision; a record keeps
  only the fields it models (see *Setup-owned catalog pipeline*);
- the key covers exactly the inputs that change the result: the per-source
  revision list, the projected setup configuration, plugin provenance, the
  published environment names and their exact value digests, the effective allow set,
  and the schema version.

### Data decisions

1. Keep one definition per record. Resolution returns a second *instance* of
   the same type, never a `resolved` field and never a `Resolved*` type: the
   catalog instance is what a plugin produced, and the resolved instance is what
   the setup applied configuration and policy to. The Toolang-side facts ride in
   the single `_toolang` sub-record.
2. Keep `Provider` on the setup. Inspection reads it from the setup instead of
   re-running resolution.
3. No `api_key` field exists on any record. The setup supplies a trimmed
   credential environment to the adapter, which selects the value from it;
   `to_data()` and the durable record never contain a credential.
4. Reasoning stays on the model as capability. The request carries the demand
   and the call carries the effective control. Nothing is moved off the model;
   only the effective control relocates, from the deleted `ModelTarget` to
   `ModelCall`.
5. Delete `ModelInfo`, `ModelTarget`, `ModelEntry`, `ModelParameters`,
   `ModelAlias`, and `ProviderConfig`.
6. `none` is the only Toolang-defined effort level. Every other level comes from
   the model's advertised options.
7. Model capability keeps the catalog name `limit.output`; the run's demand and
   the call's effective allowance are different values and keep different names
   (`ModelRequest.max_output` and `ModelCall.max_output_tokens`).
8. A catalog value and its effective counterpart keep one field name. The
   effective api is resolved by setup and never written back, so `Provider.api`
   keeps the catalog value; the adapter's own `default_api` only feeds
   resolution.
9. Per-million prices and the `ModelInfo.metadata` bag are not stored again;
   they derive from the catalog fields that already exist.
10. `local` belongs to the catalog and nowhere else. The catalog declares it on
    its snapshot; no `Provider` or `Model` carries it. Query locality remains
    unpopulated until setup publishes the required source association.
11. `scope`, `tags`, `selectors`, and `streaming` are not model, request, or
    call data; each is dropped, moved to its owner, or computed where it is
    used. `mode` is provider-declared catalog data (58 published models use it),
    so it stays.
12. Effective `headers` and `options` are merged by setup into each model's
    `Model._toolang.route`, without new durable record fields. Raw catalog
    provider blocks remain on the model and in catalog exports.
13. `api_key` is never a record field. The executor trims the run-pinned setup
    environment to the names the resolved route declares and passes that mapping
    through the call site; the adapter selects the credential from the mapping and never reads the
    process environment. The same rule answers availability inspection.
14. Catalog provenance is not stored per record. `Model` carries no
    `catalog`/`catalog_revision`; the published setup marks it, and
    `AgentSetup.catalog_sources` maps provider IDs to source names and revisions.
    Accounting consumes the run-pinned mapping for existing pricing fields;
    `AgentSetup.revision` identifies the complete setup version.

## Unused catalog fields

A record of every catalog field we parse and export but never read, kept so a
later review can judge whether the logic is incomplete. None of these is read by
resolution, inspection, execution, or accounting today.

| Field | Scope | Today | Note |
| --- | --- | --- | --- |
| `knowledge` | model | parsed, exported, never read | only appears in `parsing.py` and `Model.to_data()` |
| `interleaved` | model | parsed, exported, never read | 1003 published models declare `{"field": "reasoning_content"}` |
| `limit` keys other than `context`, `output` | model | only `context` and `output` are read | `setup/models.py`, `plugin/models/collections.py` |
| `cost.context_over_200k`, `cost.audio` | model | parsed, exported, never read | Accounting reads `input`, `output`, `tiers`, `cache_read`, `cache_write`, `reasoning`, `input_audio`, and `output_audio` |
| `doc` | provider | parsed, exported, never read | only `Provider.to_data()` |
| unknown top-level fields | provider, model | ignored at parse time | not modelled by any type |

Each row is either wired up in a later change or left as export-only; this list
exists so the gap is visible rather than silent.

## Durable record boundary

No records schema change is included. Runtime `ModelRequest` fields are flat,
but serialization keeps `{ref, parameters: {reasoning, max_output}}`. Existing
run/retry/session data must retain its controls when read and re-serialized.
There is no replacement `ModelParameters` runtime object.

`ModelCall.reasoning` is effective runtime call data. Existing `ModelCallRefs`
and durable call codecs do not store it; adding replay support belongs to a
separate records change. No new durable field is claimed in this PR.

Existing accounting `pricing.source` and `pricing.revision` are populated from
the setup version pinned by the run, including after a later setup refresh.
Catalog provenance remains setup-owned rather than duplicated on every model.

## Adapter invocation

The chain is `ModelRequest -> Model -> ModelCall -> adapter`. An adapter receives
the resolved Model and the assembled ModelCall, plus explicit environment values.
It reads connection facts from `model._toolang.route` and ownership from
`model._toolang.provider`; there is no separate route argument.

Setup owns resolution in `setup/routes.py`. The published ModelRoute contains
adapter, API, satisfied environment rule, headers, and options, with no issues
list. Its nullable fields also drive CLI unavailability labels. Model retains
catalog capability fields such as structured_output, reasoning_options, and limit.

### Credential flow

An adapter never reads the process environment. The credential reaches it as a
plain mapping supplied per call:

- setup resolves the environment rule; the executor passes only declared names
  from that run's pinned setup environment;
- the call site passes that trimmed mapping to the adapter;
- the adapter selects the credential from the mapping itself, using the
  model/provider facts it is invoked with (the declared names and the
  credential-suffix rule).

Availability is decided once at setup publication: `too models`/`too providers` answer `ready` from the
same declared names and the same trimmed environment, so inspection and execution
agree on what "available" means. Nothing about the credential is stored on a
record: no `api_key` field on `Model`, `Provider`, `ModelTarget` (deleted), or
`ModelCall`, and nothing in `to_data()` or the durable record.

The credential value is therefore never a `Model`/`Provider` field; only the
declared *names* are catalog/provider data (`Provider.env`), and only the trimmed
*values* are transient call input.

## Acceptance

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Regression coverage must verify:

- existing run/retry/session model requests retain nested wire controls while
  runtime request fields remain flat;
- models.dev environment inference requires common fields and one credential;
- plugin-owned mutable mappings cannot alter a published setup version;
- `models` and `providers` share default/`--all` scope, and full inspection works
  even when a configured default or compact model is unavailable;
- accounting retains the run's catalog source and revision after setup refresh;
- unchanged local probes keep their stamp, and changed catalog input advances
  the setup revision.

## Deferred follow-ups

The setup pipeline below replaces the old parallel inspection pipeline and
projection-cache design. Remaining structural work is separate from this fix:

- Move the residual `plugin/models/` runtime/query package to its owning concept
  package and move model-setting source parsing out of `base`.
- Adapt durable call records to effective reasoning in a separate records change.

### Known gaps

Recorded so the remaining wiring is visible rather than silent:

- Query views do not expose catalog provenance columns. Accounting consumes
  setup-owned provenance through its existing pricing fields. Locality is not
  published as a query column.

## Setup-owned catalog pipeline

The setup is the only component that loads, merges, resolves, caches, and
publishes model catalogs. This replaces the parallel inspection pipeline and
folds the deferred cache work into one change.

### Ownership

| Concern | Owner |
| --- | --- |
| Per-catalog cache file | `toolang/setup/cache.py` |
| Models.dev records | persisted, revision `sha256:<payload>` + mtime |
| `ollama`/`llama_cpp` records | one file per catalog: content is the last probe result, mtime is `detected:<stamp>` |
| Merge, resolve, project, publish | `toolang/setup/watcher.py`, one builder |
| Catalog plugins | snapshots only; never cache |

A catalog plugin holds configuration only. It keeps no module-level state, does
not memoize, and writes no file. `ModelsDevModelCatalog.capture()` stays: it is
one stable read whose observation and payload the setup reuses inside a single
refresh, not plugin-owned cache.

### Persistence boundary

- Every source's records are resolved before publication, so a published setup
  never re-parses a file or re-resolves configuration.
- A record keeps only the fields it models. Any other catalog field is ignored at
  parse time: not loaded, not persisted, not exported. `too models --json` is a
  projection of the persisted facts.
- Anything the runtime reads must be a first-class field. The probe `runtime`
  block is dropped: a local catalog uses only the standard `Provider`/`Model`
  fields.
- Every catalog persists one file per catalog, in the same document format: the
  providers and models that source produced. Resolution and projection run on
  every refresh, so only the reload rule differs — models.dev compares the source
  file's mtime and payload digest, while a local catalog compares the fresh probe
  with the file's own content.
- A local file keeps the mtime of the moment the current run of identical results
  was first saved. It is rewritten only when the probe differs, so identical
  probes leave the mtime — and the revision — untouched.

### Merge and identity

- One merge serves the runtime refresh and the one-shot CLI load.
- A provider id, and a `(provider id, model id)` identity, must be unique across
  sources. A duplicate is an error; the local-overwrite exception is removed.
- A local catalog sets only `ProviderToolang(adapter=..., env=...)` and uses its
  catalog name as its provider id. The merge adds no field to a provider or a
  model and never re-tags a source.
- The existing `Model`/`Provider` design covers a local catalog: `api` is the
  endpoint, `cost` is zero, the adapter is declared, and every model it publishes
  is `ready` (an unreachable runtime is never discovered, so it publishes
  nothing).
- A local catalog declares zero rates on `cost` for every meter a probe can
  report, so the `local` flag on `build_model_accounting` and its rate override
  are deleted and a local estimate stays complete.
- Locality does not need to reach the setup. `--json` exports catalog facts
  from the selected setup view, including local models, without Toolang slots.
  The query row's `scope` (`local`/`remote`) goes with it: it is not a query column,
  nothing displays it, and the collection never populated it.
- `Model.provider` retains catalog overrides. Setup reads these declarations
  into `Model._toolang.route` without injecting adapter results into that block.

### Toolang facts

| Slot | Source facts | Published facts |
| --- | --- | --- |
| `Provider._toolang` | trusted plugin `env` and `adapter` declarations | adds default `route` |
| `Model._toolang` | owning `provider` | adds effective `route` and `ready` |
| `Model.provider._toolang` | optional trusted plugin override | unchanged by setup |

Only typed plugin declarations are trusted; raw `_toolang` mappings in catalog
JSON do not become configuration. Source cache codecs retain declarations and
ownership while omitting readiness and routes. Full-view codecs retain the
published facts in memory, pinned to setup revision. Exports strip Toolang slots.

### Source revisions

The models.dev reader reports its file revision. Setup assigns probe revisions
from persisted content and detection time, with a content-digest fallback if a
write is skipped or fails.

| Source | Revision | Reload trigger |
| --- | --- | --- |
| models.dev | `sha256:<payload>` **and** `mtime_ns` | either the content digest or the mtime advances the revision |
| `ollama`, `llama_cpp` | `detected:<file mtime>`, advanced only when the probe result changes | the probe result differing from the previous one |

- mtime is a first-class part of the models.dev revision: touching the file
  advances the revision even when the payload is unchanged.
- Consecutive identical probes leave the file untouched, so the mtime — and the
  revision — stays at the moment the identical run was first saved. A differing
  probe rewrites the file and with it the stamp.
- Setup includes all source revisions in its projection key and retains the
  provider-to-source mapping in `catalog_sources`. The intermediate merged
  snapshot keeps its first source revision; it is not the published setup ID.

### Projection key

The key covers exactly the inputs that change the result:

- the per-source revision list (the models.dev payload digest and each local
  `detected:` stamp);
- the projected setup configuration and plugin provenance;
- the effective allow set;
- every environment name published in `AgentSetup.envs` **and its exact value
  digest**, because tools and API templates also read names not declared by model
  providers;
- the schema version.

An unchanged key means no new setup version; a source's cached file removes the
parse. The key covers every published setup input, so a change to defaults,
limits, or the allow set is a new version too.

### Publication

- `AgentSetup` carries a `revision` derived from the same inputs as the
  projection key.
- The setup keeps only the current version: `current()` returns it and
  `updates()` yields each new revision once. A run keeps its own reference to the
  version it started with, so an older version lives exactly as long as something
  still uses it.
- A run pins the `AgentSetup` it started with; a refresh never mutates it.
- Only a fully built setup becomes a version; a failed refresh publishes nothing.

### CLI inspection

`too models`, `too providers`, and `too adapters` read a setup version. The
inspection pipeline is deleted: `CatalogInspection`, `load_catalog_inspection`,
`load_matching_catalog_inspection`, the inspection projection key, the
duplicated `_LOCAL_CATALOG_ENV`, the duplicated catalog ordering, and the
duplicate snapshot wrapper. One builder serves the watcher and the one-shot CLI
load.

### Acceptance

- the default verification suite passes;
- a record carries only the fields it models, and no persisted document carries
  an unmodelled catalog field;
- a duplicate provider id or model identity across sources is an error;
- repeating an identical local probe publishes no new setup revision;
- restarting the process reads the same local probe file, keeps its mtime, and
  keeps the setup revision stable;
- touching the models.dev file advances the revision even when its payload is
  unchanged;
- JSON includes the selected models from all sources, including local catalogs;
- inspection and runtime render the same providers and models for the same
  sources.

### Default and complete catalog views (approved)

Goal: runtime selection and CLI inspection default to the same usable resources,
while setup retains a version-pinned complete catalog for explicit inspection.

- Persist complete source catalogs regardless of readiness or `allow.models`.
  Do not persist a second filtered catalog.
- `AgentSetup.models` contains only ready models matching `allow.models`, in
  configured preference order. `providers` contains only their providers. The
  collections are joined by `Model._toolang.provider`; Provider stores no models.
- `setup.model_catalog()` projects the default view. `all=True` materializes the
  complete resolved catalog on demand, including unready models, allow-excluded
  models, and providers with no models. This never broadens runtime selection.
- Pin the complete view as compact serialized resolved data using a dedicated
  in-memory codec entry point, separate from source-cache encoding. Keep only default typed indexes resident; decode full records on
  demand. No source re-read, re-probe, or mutable cache access may change an old
  setup's full view. The full view carries the setup revision.
- `too models` and `too providers` default to the same usable view. Both accept
  `--all` to select the complete view. Model `--query` narrows the selected view;
  `--json` changes only formatting and exports raw catalog facts from all sources.
  `available` continues to mean readiness, independently of allow membership.
  Both commands support an optional resident agent target before the command.
- Runtime validation of explicit default and compact models remains strict.
  Catalog CLI calls explicitly skip selection validation so an invalid choice
  does not prevent inspecting the published catalog.

Touchpoints: setup types, watcher publication and cache codec; catalog CLI
commands; model documentation; setup and CLI acceptance tests.

Acceptance: mixed ready/unready and allowed/excluded models prove both view
memberships and ownership consistency; empty providers appear only in the
complete view; automatic default/compaction never selects unready models; source
changes and cache deletion cannot change a pinned view; root and agent CLI,
queries and JSON preserve the selected scope, including local models.

Tradeoffs: each live setup retains serialized full records but no full query
index. Explicit full reads incur decoding cost. Existing per-source persistence
and strict runtime setup validation remain unchanged. No open decisions.
