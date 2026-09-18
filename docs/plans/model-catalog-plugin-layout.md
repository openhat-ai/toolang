# Model plugin layout

## Goal and scope

Repackage the model plugins so packaging matches our concepts:

- `plugin/catalogs/` holds one directory or module per catalog plugin;
- `plugin/adapters/` holds one module per adapter plugin;
- a catalog plugin only produces an immutable `ModelCatalogSnapshot`; combining,
  projecting, and caching the merged result belong to `toolang.setup`;
- `plugin/models/` temporarily keeps the model runtime/query layer that this
  change does not move.

No runtime behavior changes: catalog contents, entry-point names, cache keys,
and inspection/selection results stay identical. This supersedes the catalog
placement sketched in `plugins.md` and `models.md`.

## Target layout

```text
src/toolang/
├── common/
│   ├── cache.py                  # portable cache document envelope, digest, canonical, secret scan
│   └── json.py                   # deterministic Decimal-safe JSON dumps
├── plugin/
│   ├── adapters/                 # moved from plugin/models/adapters, content unchanged
│   │   ├── __init__.py
│   │   ├── loading.py            # load_model_adapters
│   │   ├── _structured_output.py
│   │   ├── _usage.py
│   │   ├── chat_completions.py
│   │   ├── generate_content.py
│   │   ├── messages.py
│   │   └── responses.py
│   ├── catalogs/
│   │   ├── __init__.py
│   │   ├── loading.py            # load_model_catalogs
│   │   ├── _local.py             # shared helpers used by ollama and llama_cpp only
│   │   ├── models_dev/
│   │   │   ├── __init__.py
│   │   │   ├── catalog.py        # ModelsDevModelCatalog, read_model_catalog_snapshot, factory
│   │   │   ├── parsing.py        # models.dev-compatible record parsing/validation and snapshot rebuild
│   │   │   ├── path.py           # MODEL_CATALOG_ENV, PACKAGED_MODEL_CATALOG, resolve_model_catalog_path
│   │   │   ├── cache.py          # static catalog artifact cache
│   │   │   └── data/catalog.json # packaged catalog
│   │   ├── ollama.py
│   │   └── llama_cpp.py
│   ├── models/                   # temporary: not moved by this change
│   │   ├── __init__.py           # docstring states this is the migrating model runtime/query layer
│   │   ├── budget.py
│   │   ├── collections.py
│   │   ├── config.py
│   │   ├── discovery.py
│   │   ├── messages.py
│   │   ├── provider_resolver.py
│   │   ├── resolution.py
│   │   └── views.py
│   ├── channels/
│   ├── sandboxes/
│   ├── toolsets/
│   ├── config.py
│   └── loading.py
└── setup/
    ├── cache.py                  # derived model context projection cache
    ├── catalog.py                # existing, plus MergedModelCatalog and origin attachment
    ├── models.py                 # existing, plus model_info_from_catalog and its cost helper
    ├── watcher.py
    ├── config.py
    ├── tools.py
    ├── types.py
    └── errors.py
```

`plugin/models/` no longer contains `catalog.py`, `local.py`, `cache.py`,
`loading.py`, `adapters/`, or `data/`. `discovery.py` stays: it only reads
`Provider.resolved` and is generic provider inspection, not local-catalog
discovery.

## Module map

| Current | New |
| --- | --- |
| `plugin/models/adapters/*` | `plugin/adapters/*` (unchanged) |
| `plugin/models/loading.py::load_model_adapters` | `plugin/adapters/loading.py` |
| `plugin/models/loading.py::load_model_catalogs` | `plugin/catalogs/loading.py` |
| `plugin/models/catalog.py` models.dev plugin, reader, factory | `plugin/catalogs/models_dev/catalog.py` |
| `plugin/models/catalog.py` parsing/validation + `model_catalog_snapshot_from_data` | `plugin/catalogs/models_dev/parsing.py` |
| `plugin/models/catalog.py` `resolve_model_catalog_path`, `PACKAGED_MODEL_CATALOG` | `plugin/catalogs/models_dev/path.py` |
| `plugin/models/catalog.py` `MergedModelCatalog`, `_with_catalog_origin` | `toolang/setup/catalog.py` |
| `plugin/models/catalog.py` `model_info_from_catalog`, `_cost_per_token` | `toolang/setup/models.py` |
| `plugin/models/catalog.py` `query_catalog_models` | deleted (tests only) |
| `plugin/models/catalog.py` `catalog_json_dumps` | `toolang/common/json.py::dumps` |
| `plugin/models/local.py` `OllamaModelCatalog` and private helpers | `plugin/catalogs/ollama.py` |
| `plugin/models/local.py` `LlamaCppModelCatalog` and private helpers | `plugin/catalogs/llama_cpp.py` |
| `plugin/models/local.py` shared local helpers | `plugin/catalogs/_local.py` |
| `plugin/models/cache.py` static catalog artifact cache | `plugin/catalogs/models_dev/cache.py` |
| `plugin/models/cache.py` context projection cache | `toolang/setup/cache.py` |
| `plugin/models/cache.py` document envelope and helpers | `toolang/common/cache.py` |
| `plugin/models/data/catalog.json` | `plugin/catalogs/models_dev/data/catalog.json` |

`Provider`, `Model`, and `ModelCatalogSnapshot` remain in
`toolang.base.types.model`, shared by every catalog and adapter plugin. Only
models.dev currently has JSON parsing, so `parsing.py` lives inside the
models_dev subpackage rather than a shared directory; `_local.py` stays a plain
module because only ollama and llama_cpp share it.

## Cache ownership

`plugin/models/cache.py` is removed; its three concerns separate by owner.

| Concern | Owner |
| --- | --- |
| Static file catalog artifact (`CatalogSource`, `FileObservation`, `capture_catalog_source`, artifact load/store, `CATALOG_PARSER_SCHEMA`, `_CATALOG_FILE`) | `plugin/catalogs/models_dev/cache.py` |
| Derived model context projection (`ModelProjectionCache`, `CachedModelProjection`, context load/store, `catalog_identity_misses`, `model_projection_key`, `hydrate_model_infos`, `environment_readiness`, `ModelInfo`/`ModelQueryView` codecs) | `toolang/setup/cache.py` |
| Document envelope and primitives (`_store_document`, `_load_document`, digest, canonical value, secret scan, `CACHE_SCHEMA`) | `toolang/common/cache.py` |
| Decimal-safe deterministic JSON | `toolang/common/json.py` |

The context projection cache is not owned by any catalog: it stores the merged,
`allow.models`-filtered effective set whose `catalog_revisions` include runtime
revisions from ollama and llama_cpp. The artifact cache is models.dev-only and
rejects local records.

`CACHE_SCHEMA = 4`, `CATALOG_PARSER_SCHEMA = 1`, and the `artifact_key` and
`model_projection_key` formulas stay byte-identical, so existing `.setup`
caches keep hitting; only module ownership changes. The context envelope keeps
its faster `from_json` parse path by an explicit loader flag instead of the
`kind` check.

`setup/catalog.py` and `setup/watcher.py` construct two caches after this
change: a models.dev artifact cache and a setup context projection cache.

## Decisions

- `plugin/models/loading.py` splits per family, matching
  `channels/loading.py`, `sandboxes/loading.py`, and `toolsets/loading.py`.
- Catalogs use adapters' convention: a catalog per module or subpackage, shared
  helpers as a leading-underscore module. No `common/` directory.
- `models_dev` is a subpackage: it owns the packaged data file and catalog path
  precedence in addition to its reader.
- Combining snapshots and projecting to runtime `ModelInfo` belong to
  `toolang.setup`, per the catalog-plugin boundary.
- No compatibility shims remain: internal importers, tests, and documentation
  move in the same change.
- No cross-plugin shared module is added at `plugin/` level; adapters and
  catalogs currently share no code beyond `toolang.base` types.

## Entry points

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
`too catalogs`/`too adapters` output keep working.

## Touchpoints and acceptance

Update `setup/catalog.py`, `setup/watcher.py`, the new `setup/cache.py`, CLI
`commands/model_catalog.py`, `pyproject.toml`, the affected unit and integration
tests, and `docs/models.md` and `docs/plugins.md`. Regenerate or ignore
`reference/generated/`, which is already stale.

Replace imported symbols only; keep assertions and file names unchanged.
Delete the `query_catalog_models` cases in
`tests/unit/plugin/test_model_catalog.py`, and update imports in
`test_local_models.py`, `test_models.py`, `test_protocol_adapters.py`,
`test_provider_resolver.py`, `test_plugin_toolsets.py`, `test_sandboxes.py`,
`tests/unit/setup/test_setup_watcher.py`, and
`tests/integration/cli/test_model_catalog_commands.py`.

Acceptance:

- `too models`, `too providers`, `too catalogs`, `too adapters` outputs unchanged;
- packaged catalog loads from its new path and still validates;
- models.dev artifact cache and setup context cache still hit across runs;
- ollama and llama_cpp probe, enrichment, offline behavior, and endpoint
  rewriting unchanged;
- default verification passes:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## Risks

- Moving `plugin/models/adapters` breaks any third-party deep import of the old
  internal module paths; these are implementation packages, not documented
  public API.
- `PACKAGED_MODEL_CATALOG` moves under the subpackage; verify the wheel still
  ships `data/catalog.json` and update the packaged-catalog test.
- The `models_dev` `isinstance` checks in setup depend on the single class
  identity; switch importers in one change without aliases.
- Dependency direction is one-way: `plugin.catalogs.models_dev.cache` imports
  `common.cache` and `models_dev.parsing`; `setup.cache` imports `common.cache`
  and `plugin.models.collections`. No cycles.

## Open questions

None. Catalog placement, parsing ownership, cache split, and combiner/projection
ownership are decided above.
