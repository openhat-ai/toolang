# Model plugins

## Goal and scope

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

This document is the single record for the model work: layout, data model, and
flow. It supersedes the catalog placement sketched in `plugins.md` and
`models.md`, and the earlier `model-catalog-plugin-layout.md`.

## Layout

```text
src/toolang/
├── common/
│   ├── cache.py                    # cache document envelope, digest, canonical value, secret scan
│   ├── files.py                    # atomic writes and file locks
│   └── json.py                     # deterministic Decimal-safe JSON dumps
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
    ├── catalog.py                  # snapshot merge and the models.dev source read
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
| Deterministic Decimal-safe JSON | `toolang/common/json.py` |

The static models.dev artifact cache was removed: loading it still ran full
validation, the cached shape is the source shape again, and the default packaged
catalog is small enough that the extra read costs more than a direct parse. The
catalog file is captured once instead: `ModelsDevModelCatalog.capture()` returns
a stable `FileObservation` plus a `ModelCatalogSource` holding the payload bytes
and the portable `sha256:` revision, and `ModelCatalogSource.snapshot()`
validates those same bytes.

`CACHE_SCHEMA` and the `model_projection_key` formula are unchanged, so existing
`.setup` caches keep hitting.

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
| `ModelRoute` | the effective connection one call must use | computed from setup data at call time | executor, adapter |
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
`Model` whose single `_toolang` sub-record (`ModelToolang`) carries `ready` and
the owning `provider`. The type is the same, so a field keeps one name across
the catalog instance and the resolved instance, and nothing in the record points
at the resolved instance.

The request and the call do not need `adapter`, `api`, or `ready`: a request
names the model by `ref` and carries parameters, and the call carries content
plus effective controls. The effective connection an adapter must use is
computed at call time as a `ModelRoute` and is not stored on the model.

`ready` is the persisted readiness (`ModelToolang.ready`). `available` is not a
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

A models.dev provider record has exactly `id`, `name`, `env`, `npm`, `api`,
`doc`, and `models`; a models.dev model record has exactly the 21 fields the
importer accepts. Anything else our records carry is a Toolang-side fact, not
catalog data.

The resolved instance carries `ProviderToolang{env, adapter}`: the normalized
OR-of-AND `env` rule and the effective `adapter`. Configuration overrides are
applied while producing that instance.
`api` is not overwritten: `Provider.api` keeps the catalog value and the
effective api is computed per call.

### Request headers and options

`headers` and `options` are not catalog data, and they are load-bearing: every
built-in adapter sends them.

| What | Read by |
| --- | --- |
| `headers` | `chat_completions` and `responses` as client default headers; `messages` and `generate_content` merged into the raw request headers |
| `options` | all four adapters, merged into the provider request body, and read for `audio`, `modalities`, and `max_completion_tokens` |

They come from core provider configuration (`[models.providers.<name>].options`),
the built-in OpenRouter header defaults (`PROVIDER_CONVENTIONS`), and an
advertised `experimental.modes.<mode>` body and headers. Resolution merges them
per model into the call-time `ModelRoute`; they are never written to a record
entry and never exported.

`Provider` is the catalog group's state: the setup publishes it and
`too providers` renders it. It is not execution input.

### Fields that are not model data

These do not belong to `Model`, `ModelRequest`, or `ModelCall`, and are not
reintroduced under another name:

| Field | Where it comes from | Resolution |
| --- | --- | --- |
| `scope` | not authored model data | computed per query row as `local` or `remote` from the provider's locality |
| `tags` | no owner after `ModelAlias` was removed | dropped; the query row leaves `tags` empty |
| `selectors` | removed | the query identity is `provider/model` (`IdentitySpec`) |
| `streaming` | fixed `_MODEL_STREAMING = True` in the executor | not model data; whether to stream is an execution and adapter decision |
| `available` | projected from `ModelToolang.ready` | a display/query concept only; never persisted |
| `api_key` | selected from the environment | never stored on a record; the adapter reads the credential names a provider declares from the environment it is given |

### Field collisions and how they resolve

Two questions stay separate: what the catalog says, and what a call must use.
A catalog value stays on the record; the effective value is either a Toolang-side
fact on `_toolang` or a call-time computation. No second name is invented and no
record points at the other instance.

| Name | Catalog instance | Effective value |
| --- | --- | --- |
| `api` | `Provider.api` | computed per call (`model_api`); never written back |
| `env` | `Provider.env`, the declared names | `ProviderToolang.env`, the normalized OR-of-AND rule |
| `adapter` | not catalog data: `ProviderToolang.adapter`, or a model-level deviation on `Model.provider._toolang.adapter` | `model_adapter(provider, model)` |
| `headers`, `options` | provider/model configuration | merged into the call-time `ModelRoute` |
| `ready` | unset | `ModelToolang.ready`, the persisted availability |
| `available` | derived | display-only projection of `ready`; never persisted |
| `local` | `ModelCatalogSnapshot.local` | the catalog's own property; a derived provider index, never a record field |

Whether a model is local or remote derives from its provider's
`ProviderToolang.local`, which the setup attaches from the declaring catalog.

Export uses the catalog instances, so `--json` stays a raw catalog projection
and never shows an effective route. Inspection keeps the merged catalog
instances it loaded and renders selection and availability from the resolved
ones.

### Which api a call uses

Resolution picks one effective api, in this order: explicit provider
configuration, then the catalog's `api`, then the adapter's own `default_api`.
The winner is returned by `model_api` and travels on the call-time `ModelRoute`;
it is not written back onto the provider record. `ModelAdapter.default_api` is
resolution input only; it is never consulted while building a request.

### Reasoning: capability, demand, effective control

Reasoning holds three different things in three places. It is not one value that
moves.

| What | Where | Meaning |
| --- | --- | --- |
| Capability | `Model.reasoning`, `Model.reasoning_options` | what this model can do; the catalog knows it |
| Demand | `ModelRequest.reasoning` | what this run asks for |
| Effective control | `ModelCall.reasoning` | what this call uses, and what the adapter and the durable record see |

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
- only the call value reaches the provider and the durable call record.

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
identity or selection, so an adapter is invoked as `(Model, ModelCall)` and the
durable record replays a call without resolving a model for controls again.

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
- `too adapters` renders the published adapters plus their source.
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
  declared environment names and their value digests, the effective allow set,
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
   effective api is computed per call and never written back, so `Provider.api`
   keeps the catalog value; the adapter's own `default_api` only feeds
   resolution.
9. Per-million prices and the `ModelInfo.metadata` bag are not stored again;
   they derive from the catalog fields that already exist.
10. `local` belongs to the catalog and nowhere else. The catalog declares it on
    its snapshot; no `Provider` or `Model` carries it, and a provider's locality
    is derived from the source that declared it.
11. `scope`, `tags`, `selectors`, and `streaming` are not model, request, or
    call data; each is dropped, moved to its owner, or computed where it is
    used. `mode` is provider-declared catalog data (58 published models use it),
    so it stays.
12. `headers` and `options` are not catalog data but are request data: resolution
    merges them per model into the call-time `ModelRoute`; they are never stored
    on a record entry, never exported, and never written to a durable record.
13. `api_key` is never a record field. The setup trims the process environment
    to the names a provider declares and passes that mapping through the call
    site; the adapter selects the credential from the mapping and never reads the
    process environment. The same rule answers availability inspection.
14. Catalog provenance is not stored per record. `Model` carries no
    `catalog`/`catalog_revision`; the cache/snapshot outer layer marks it, and
    `model_projection_key(catalogs=...)` is the current carrier. Reattaching it
    to inspection output (`ModelQueryView.catalog`, `pricing.source`) is a
    deferred follow-up.

## Unused catalog fields

A record of every catalog field we parse and export but never read, kept so a
later review can judge whether the logic is incomplete. None of these is read by
resolution, inspection, execution, or accounting today.

| Field | Scope | Today | Note |
| --- | --- | --- | --- |
| `knowledge` | model | parsed, exported, never read | only appears in `parsing.py` and `Model.to_data()` |
| `interleaved` | model | parsed, exported, never read | 1003 published models declare `{"field": "reasoning_content"}` |
| `limit` keys other than `context`, `output` | model | only `context` and `output` are read | `setup/models.py`, `plugin/models/collections.py` |
| `cost` keys other than `input`, `output`, `tiers` | model | `cache_read`, `cache_write`, `context_over_200k`, `reasoning`, `audio` parsed, never read | `execution/accounting.py` reads `input`/`output`/`tiers` |
| `provider.body` (model-level override) | model | parsed raw, never read | 1 published model; no adapter reads it |
| `provider.headers` (model-level override) | model | parsed raw, never read | models.dev schema allows it; 0 published today |
| `doc` | provider | parsed, exported, never read | only `Provider.to_data()` |
| unknown top-level fields | provider, model | ignored at parse time | not modelled by any type |

Each row is either wired up in a later change or left as export-only; this list
exists so the gap is visible rather than silent.

## Durable record

After the fold the durable model step keeps only what a replay cannot re-derive
from the model ref:

```text
StoredModelStepGiven
  model: str                 # the resolved model ref (provider/model identity)
  call: ModelCallRefs
    instructions: str        # reference into the prompt store
    messages: {head, delta}  # incremental message templates
    tools: str | None        # reference
    output_schema: dict | None
    cont: object | None
    reasoning: Reasoning | None        # effective control (moved off ModelTarget)
    max_output_tokens: int | None      # the allowance this call actually applied
    version: int
```

Reasons this is the natural form:

- it stores *which model* and *what it was asked*, plus the effective controls,
  and nothing else;
- `reasoning` moves from the deleted `ModelTarget.reasoning` onto the call, so a
  replay reproduces the request without resolving a model for controls again;
- the resolved route (`api`, `adapter`, `ready`), `headers`, `options`, and the
  credential are never stored: replay re-resolves them from the model ref and the
  environment, exactly as a first run does.

## Adapter invocation

The chain is `ModelRequest -> Model -> ModelCall -> adapter`. An adapter is
invoked with the call-time `ModelRoute`, the resolved `Model`, and the assembled
`ModelCall`; it must not read model selection or the credential from the call.

`ModelRoute` (computed in `executor/frame.py` via `model_route`) carries the
connection facts an adapter needs: `provider`, `adapter`, `api`, `env`,
`headers`, and `options`. `Model` carries the catalog facts an adapter reads as
capability, such as `structured_output`, `reasoning_options`, and `limit`.

### Credential flow

An adapter never reads the process environment. The credential reaches it as a
plain mapping supplied per call:

- the setup computes the environment names a provider declares (the resolved env
  rule) and trims the process environment to exactly those names;
- the call site passes that trimmed mapping to the adapter;
- the adapter selects the credential from the mapping itself, using the
  model/provider facts it is invoked with (the declared names and the
  credential-suffix rule).

The trimming/supplying rule is implemented once in the setup and shared with
availability inspection: `too models`/`too providers` answer `ready` from the
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

Layout and cache changes are behavioral no-ops: catalog contents, entry-point
names, cache keys, and inspection/selection results stay identical. Verified on
the rebased branch: 5596 passed, 20 skipped, 147 subtests passed. Added
regression coverage for local probe diagnostics and for the declared-adapter
path.

## Deferred follow-ups

Measured on this tree (best of three, one machine) before any optimization:

| Step | 15-model packaged catalog | 3000-model catalog (1.17 MiB) |
| --- | --- | --- |
| parse and validate | 0.46 ms | 55 ms |
| resolve providers | 0.15 ms | 23 ms |
| project model facts | 0.24 ms | 44 ms |
| build model collection | 0.57 ms | 617 ms |
| catalog dataset | 0.39 ms | 75 ms |
| merge snapshots | - | 1.7 ms |
| cache write | 5.0 ms | 652 ms |
| cache read | 2.1 ms | 363 ms |

Findings that motivate the follow-ups:

- `build_model_collection` is dominated by `_discover_available_candidates`
  (532 ms of 617 ms): every model builds a target and a query string, then the
  collection and the catalog dataset each project the same models again.
  Reusing one view set cuts `ModelCollection` construction from 84 ms to 20 ms.
- On the default catalog the projection cache costs more than it saves; on the
  large catalog it is ~2.2x faster than recomputation while writing 5.3x the
  source size per revision, with no pruning.
- The watcher and the one-shot inspection build the same pipeline twice, each
  with its own `_LOCAL_CATALOG_ENV`, snapshot wrapper, catalog-config assembly,
  and merge wrapper.

Deferred work, in order:

1. Reuse one projection across the collection and the catalog dataset, and stop
   re-deriving candidates.
2. Decide the projection cache from those numbers: delete it, or rebuild it as
   one file per key with a bounded set and version-and-digest validation only.
3. Collapse the watcher and inspection pipelines into one builder, and make
   snapshot merging a pure function over snapshots.
4. Move the residual `plugin/models/` package to its own concept package,
   split the plugin-facing types in `base/types/model.py` from the runtime
   selection types, move the model-setting body parser out of `base`, and let
   `too models`, `too providers`, and `too adapters` all read the setup.
5. Prune stale `.setup` model-cache files; the cache keeps one file per catalog
   and never removes an obsolete one.

Items 2, 3, and 5 are now scoped by *Setup-owned catalog pipeline* below.

### Known gaps

Recorded so the remaining wiring is visible rather than silent:

- `build_model_accounting(..., local=)` accepts the local flag, but no caller
  passes it, so a local model is not yet zero-rated.
- Catalog provenance is carried only by the cache/snapshot outer layer
  (`model_projection_key(catalogs=...)`); `ModelQueryView.catalog`,
  `CatalogProviderView.catalog`, and `pricing.source` are not reattached yet.
- `cli._provider_api` computes and displays the effective api (display only, no
  write-back), ahead of the rest of the provider surface.

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
- Locality does not need to reach the setup. `--json` exports the models.dev
  source snapshot, which is the models.dev-compatible catalog by definition, so
  no provider index and no export guard is needed. The query row's `scope`
  (`local`/`remote`) goes with it: it is not a query column, nothing displays it,
  and the collection never populated it.
- `Model.provider` is the corrected provider this model must use, not general
  catalog data, so resolution writes its own Toolang slot into that block
  (`_resolve_model` / `_with_model_adapter`). This is intentional.

### Toolang facts

Three slots carry Toolang-side facts. Catalog data is never rewritten except to
add the third slot, which resolution adds to the corrected provider block.

| Slot | Type | Meaning |
| --- | --- | --- |
| `Provider._toolang` | `ProviderToolang{env, adapter}` | the normalized env rule and the effective adapter, so a call needs no re-resolution |
| `Model._toolang` | `ModelToolang{ready, provider}` | availability and the owning provider id |
| `Model.provider._toolang` | `ProviderToolang` | what this model corrects about its provider; only `adapter` is set there |

`Model.provider` is the corrected provider this model must use, so it legitimately
holds its own Toolang slot, as the same `ProviderToolang` type. A raw `_toolang`
mapping coming from a source is ignored — only our typed value is read.

Export emits catalog facts only: `to_data()` strips every Toolang slot, while the
persisted document keeps them.

### Source revisions

Every source reports its own revision; the setup never invents one.

| Source | Revision | Reload trigger |
| --- | --- | --- |
| models.dev | `sha256:<payload>` **and** `mtime_ns` | either the content digest or the mtime advances the revision |
| `ollama`, `llama_cpp` | `detected:<file mtime>`, advanced only when the probe result changes | the probe result differing from the previous one |

- mtime is a first-class part of the models.dev revision: touching the file
  advances the revision even when the payload is unchanged.
- Consecutive identical probes leave the file untouched, so the mtime — and the
  revision — stays at the moment the identical run was first saved. A differing
  probe rewrites the file and with it the stamp.
- A merged snapshot exposes `revisions: tuple[(name, revision), ...]` instead of
  a single revision copied from the first source.

### Projection key

The key covers exactly the inputs that change the result:

- the per-source revision list (the models.dev payload digest and each local
  `detected:` stamp);
- the projected setup configuration and plugin provenance;
- the effective allow set;
- every declared environment name **and its value digest**, not presence alone,
  because an api template is substituted with environment values;
- the schema version.

An unchanged key means no new setup version; a source's cached file removes the
parse. The key covers every published setup input, so a change to defaults,
limits, or the allow set is a new version too.

### Publication

- `AgentSetup` carries a `revision` derived from the same inputs as the
  projection key.
- The setup publishes versions: `current()` returns the newest,
  `by_revision(revision)` returns a pinned one, and `updates()` yields each new
  revision once.
- A run pins the `AgentSetup` it started with; a refresh never mutates it.
- Only a fully built setup becomes a version; a failed refresh publishes nothing.
- Retention: every version referenced by an active run, plus the latest two. The
  references need no manual bookkeeping — an active run's own Python reference
  keeps its version alive.

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
- local models never appear in `--json`, which exports the models.dev source
  snapshot;
- inspection and runtime render the same providers and models for the same
  sources.
