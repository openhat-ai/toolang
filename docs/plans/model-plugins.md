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
    ├── cache.py                    # derived model context projection cache
    ├── catalog.py                  # one-shot inspection load and snapshot merge
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
| Model context projection (`effective.json`, `identity.json`) | `toolang/setup/cache.py` |
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
  `Provider.adapter` instead of fabricating an npm package.

## Data model

### Type roles

| Type | Role | Produced by | Read by |
| --- | --- | --- | --- |
| `Provider` | catalog record plus resolved route facts | catalog plugin, then setup | inspection |
| `Model` | the single model record plus resolved/selection/capability facts | catalog plugin, then setup | inspection, executor, adapter |
| `ModelCatalogSnapshot` | one immutable `{providers, models, revision, source}` | catalog plugin, merge | setup |
| `ModelCollection` | effective selection index over `Model` | setup | inspection, executor |
| `ModelAdapter` | protocol implementation | adapter plugin | setup, executor |
| `ModelOverride` | sparse authored model operation | config/env/CLI/chat parsing | setup, execution policy |
| `ModelRequest` | one run's concrete model demand | policy composition | executor |
| `Reasoning` | one reasoning control, `{effort | budget_tokens}` | request parsing, call assembly | setup validation, adapter |
| `ModelCall` | one call: content plus effective controls | execution assembly | adapter, durable record |
| `ModelCallResult`, `ModelUsage` | one call's outcome and meters | adapter | execution, accounting |

`ModelInfo`, `ModelTarget`, `ModelEntry`, `ModelParameters`,
`ResolvedProvider`, and `ResolvedModel` are removed. The query engine keeps one
internal flat row type for schema columns; it is not a second model state.

### Model

Catalog facts, verbatim and exportable: `provider_id`, `id`, `name`,
`description`, `family`, `attachment`, `reasoning`, `reasoning_options`,
`tool_call`, `interleaved`, `structured_output`, `temperature`, `knowledge`,
`release_date`, `last_updated`, `modalities`, `open_weights`, `limit`, `status`,
`experimental`, `provider`, `cost`, `extra`, `local`, `catalog`,
`catalog_revision`.

Facts the setup attaches once:

- selection: `ref` (`provider/id`), `selectors`, `scope`, `tags`;
- connection: `adapter`, `base_url`, `api_key`, `headers`, `options`, `ready`;
- capability: `reasoning_controls`.

`available` is `ready`; there is no second availability flag. `to_data()` emits
only the catalog facts, so `too models --json` stays a raw catalog export and
never leaks a resolved route, a secret, or a derived value.

### Provider

Catalog facts (`id`, `name`, `env`, `models`, `npm`, `adapter`, `api`, `doc`,
`extra`, `local`, `catalog`, `catalog_revision`) plus resolved facts (`adapter`,
`api`, `env`, `ready`). The nested `resolved` value and its wrappers are
removed; those fields live directly on `Provider` and `Model`.

`Provider` is the catalog group's state: the setup publishes it and
`too providers` renders it. It is not execution input.

### Reasoning: capability, demand, effective control

Reasoning holds three different things in three places. It is not one value that
moves.

| What | Where | Meaning |
| --- | --- | --- |
| Capability | `Model.reasoning`, `Model.reasoning_options`, `Model.reasoning_controls` | what this model can do; the catalog knows it |
| Demand | `ModelRequest.reasoning` | what this run asks for |
| Effective control | `ModelCall.reasoning` | what this call uses, and what the adapter and the durable record see |

Effort levels are provider-defined. The only source of truth is the model's
catalog `reasoning_options`, which declares `effort`, `budget_tokens`, or
`toggle` options with their `values` and optional `exhaustive: true`.

Toolang owns no level vocabulary. The setup normalizes the declaration once:

```python
class ReasoningControls:
    efforts: tuple[str, ...]   # advertised levels, catalog order
    exhaustive: bool           # provider marked the enumeration complete
    accepts_budget: bool       # a budget_tokens option exists
    toggle: bool               # a toggle option exists
```

- `Model.reasoning` stays the coarse catalog boolean;
  `Model.reasoning_controls` is the normalized derived value (`None` when the
  model advertises nothing); `Model.reasoning_options` stays verbatim and stays
  exportable.
- `Reasoning{effort: str | int | None}` is one shape for both the demand and the
  effective control: an `int` is a token budget, a `str` is a level token, and
  `auto` normalizes to `None` (provider default).
- Validation happens once, when a request becomes a call: reject a level only
  when `exhaustive` is set and the level is not advertised; reject a budget when
  `accepts_budget` is false; reject any control when there are no controls.
  Otherwise pass the value through and let the provider decide.
- `none` is the only Toolang-defined level; the adapter maps it to its
  protocol-specific disabled form. Display candidates are exactly
  `controls.efforts` plus `none` when `toggle` is set; no hardcoded allowlist.

The closed `ReasoningEffort` literal and its duplicated allowlists are removed.

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
                    resolve  adapter / api / env / ready
                    derive   ref, selectors, scope, tags, reasoning_controls
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
  `Model.ready`.
- `too providers` renders the published providers plus readiness.
- `too adapters` renders the published adapters plus their source.
- No command scans entry points, re-resolves environment rules, or re-projects a
  catalog on its own. Inspection without a running agent builds the same setup
  object once and reads it.

### Setup caching

The cache is internal to the setup. Its contract is only:

- every group's state is readable after one build;
- the first build of a process may be slow;
- a cache miss, a corrupt entry, or a half-written entry never changes the
  result, only the cost;
- the key covers exactly the inputs that change the result: catalog revisions,
  the projected setup configuration, plugin provenance, environment readiness,
  the effective allow set, and the schema version.

### Data decisions

1. Fold `ResolvedProvider` and `ResolvedModel` into `Provider` and `Model`; the
   nested `resolved` value disappears and its fields live directly on the
   records.
2. Keep `Provider` on the setup. Inspection reads it from the setup instead of
   re-running resolution.
3. Keep `api_key` on the model state in setup memory only; `to_data()` and the
   durable record never contain it.
4. Reasoning stays on the model as capability. The request carries the demand
   and the call carries the effective control. Nothing is moved off the model;
   only the effective control relocates, from the deleted `ModelTarget` to
   `ModelCall`.
5. Delete `ModelInfo`, `ModelTarget`, `ModelEntry`, and `ModelParameters`.
6. `none` is the only Toolang-defined effort level. Every other level comes from
   the model's advertised options.
7. Model capability keeps the catalog name `limit.output`; the run's demand and
   the call's effective allowance are different values and keep different names
   (`ModelRequest.max_output` and `ModelCall.max_output_tokens`).

## Acceptance

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Layout and cache changes are behavioral no-ops: catalog contents, entry-point
names, cache keys, and inspection/selection results stay identical. Verified:
5613 passed, 20 skipped, 147 subtests passed. Added regression coverage for
local probe diagnostics and for the declared-adapter path.

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
5. Prune `.setup` revisions; `catalog_identity_misses` currently scans every
   historical revision directory.
