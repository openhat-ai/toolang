# Catalog CLI Input

## Status

Proposed.

## Goal

Make Toolang's catalog input vocabulary match models.dev: use `--catalog` and
prefer the combined `catalog.json`, while continuing to accept the provider map
published as `api.json`.

## Success Criteria

- Every command that currently accepts `--models PATH` accepts `--catalog PATH`
  instead and describes it as a model catalog override.
- `--models` is rejected as an unknown option; no deprecated alias remains.
- A models.dev `catalog.json` payload loads through its `providers` member.
- A models.dev `api.json` provider-map payload remains valid input.
- A provider-agnostic `models.json` payload fails without a traceback and names
  the two supported models.dev endpoints.
- Implicit static catalog resolution uses `catalog.json` only and rejects a
  legacy implicit `models.json` source with an actionable migration error.
- Internal static artifacts use `catalog.json`, effective model-set caches use
  `effective.json`, and no active catalog or context file is named `models.json`.
- Catalog export, runtime execution, caching, sandbox mounting, and environment
  propagation preserve their current behavior.

## Scope and Decisions

### CLI Vocabulary

- Replace the shared `--models PATH` option with `--catalog PATH` on `info`,
  `run`, `start`, `chat`, `retry`, `rerun`, `models`, `providers`, and `serve`.
- Remove `--models` immediately. Click's standard unknown-option error is the
  migration diagnostic; there is no hidden alias or deprecation period.
- Keep the internal `model_catalog` parameter vocabulary. This change concerns
  the public CLI, not package-internal names.

### Supported Payloads

- Prefer the models.dev combined shape `{ "models": {...}, "providers": {...} }`
  published at `https://models.dev/catalog.json`.
- Consume the combined payload's `providers` member for the current runtime
  snapshot. Validate that both top-level members are objects before selecting
  the provider map.
- Continue to accept the direct provider map published at
  `https://models.dev/api.json`.
- Detect the provider-agnostic map published at
  `https://models.dev/models.json` and fail with an actionable message that
  recommends `catalog.json` first and `api.json` as the compatible alternative.
- Keep the selected file's full byte digest as its revision. A combined
  catalog therefore changes revision when either top-level member changes.

### File Resolution and Compatibility

The static source precedence becomes:

1. command-level `--catalog PATH`;
2. `TOOLANG_MODEL_CATALOG`;
3. active agent-home `catalog.json`;
4. root `catalog.json`;
5. the packaged provider-map fallback.

Agent-home scope continues to win over root scope. Before selecting the
packaged fallback, resolution checks the applicable agent-home and root scopes
for a legacy `models.json`. If one exists without any selected `catalog.json`,
resolution fails and directs the user to rename or replace it. The legacy file
is never loaded implicitly and never silently bypassed in favor of packaged
data.

Filename restrictions apply only to implicit discovery. An explicit
`--catalog PATH` or `TOOLANG_MODEL_CATALOG` path may have any filename; its
payload still determines whether it is a supported combined catalog or
provider map.

The recommended installation command is:

```bash
curl -fsSL https://models.dev/catalog.json -o catalog.json
```

`TOOLANG_MODEL_CATALOG`, `[plugin.model_catalog.*]`, and Docker's read-only
external-input mechanics remain unchanged.

### Internal File Vocabulary

- Rename the packaged provider-map fallback from `data/models.json` to
  `data/catalog.json`.
- Keep normalized static cache artifacts as `catalog.json`.
- Rename effective model-set cache documents from `models.json` to
  `effective.json`; the containing `.setup/models/contexts/` path already names
  the model domain. Keep the adjacent compact index as `identity.json`.
- Do not read legacy internal cache filenames. Setup caches are rebuildable, so
  old `models.json` context entries become unreferenced misses and are replaced
  on demand without migration or deletion.
- Keep the JSON document shapes and cache schemas unchanged. This is a filename
  vocabulary change, not a persisted-format migration.

### Output and Canonical Metadata

- `too models --json` continues to emit the current deterministic provider-map
  shape. It does not claim to reproduce the input wrapper or invent canonical
  records that are not retained by the snapshot.
- This feature does not add canonical model metadata to `ModelCatalogSnapshot`,
  expose lab/model relationships, or join canonical models to provider
  offerings. Those behaviors require a separate feature definition.
- No npm SDK or startup network dependency is added. The Python importer owns
  source parsing and startup remains offline.

## Implementation Touchpoints

- `src/toolang/cli/common/context.py`: rename the shared public option.
- `src/toolang/plugin/models/catalog.py`: accept the combined wrapper, detect
  provider-agnostic input, discover only `catalog.json`, reject legacy implicit
  input, and point the packaged fallback at `data/catalog.json`.
- `src/toolang/plugin/models/cache.py`: rename the effective model-set cache
  file to `effective.json`; retain `catalog.json` and `identity.json` for
  their existing distinct roles.
- `tests/integration/cli/test_model_catalog_commands.py`: cover command help,
  the removed option, both supported payloads, and the clean schema error.
- `tests/integration/cli/test_runtime_commands.py` and
  `tests/unit/cli/test_cli_routing.py`: update runtime propagation and routing
  coverage to `--catalog`.
- `tests/unit/plugin/test_model_catalog.py`: cover source precedence and input
  shape normalization.
- `README.md` and `docs/models.md`: document the endpoint distinction,
  preferred download, option name, and legacy filename rejection.
- `docs/layout.md`: document `catalog.json`, `effective.json`, and
  `identity.json` according to their distinct stored meanings.
- Sandbox tests change only if their fixtures exercise the new preferred
  implicit filename; mount behavior itself is unchanged.

Historical plans retain their original approved `--models` wording.

## Acceptance Tests

1. Help for all nine consuming commands lists `--catalog` and omits `--models`;
   unrelated commands list neither option.
2. Passing `--models` exits with CLI usage status 2 and reports it as unknown.
3. Passing a combined `catalog.json` through `--catalog` loads and queries its
   provider models.
4. Passing an `api.json` provider map through `--catalog` produces the same
   effective provider/model snapshot.
5. Passing a provider-agnostic `models.json` map exits with status 1, recommends
   `catalog.json` and `api.json`, and prints no Python traceback.
6. Agent-home `catalog.json` wins over root `catalog.json`. With neither
   present, a legacy implicit `models.json` produces an actionable migration
   error instead of loading that file or silently selecting packaged data.
   Environment, explicit, and packaged precedence otherwise stays intact.
7. Docker and foreground/background runtime tests preserve the resolved catalog
   path through `TOOLANG_MODEL_CATALOG` when invoked with `--catalog`.
8. `too models --json` remains a round-trippable provider map.
9. Packaged fallback loading uses `data/catalog.json`; setup writes normalized
   sources to `catalog.json`, effective model sets to `effective.json`, and
   compact indexes to `identity.json`. Legacy context-cache `models.json` files
   are not read.
10. The default offline verification suite passes.

## Risks

- Removing `--models` is an intentional public CLI compatibility break. Scripts
  must migrate to `--catalog` immediately.
- Removing implicit `models.json` discovery is an intentional compatibility
  break. The migration error prevents an existing customized source from being
  silently replaced by packaged data; explicit paths remain filename-agnostic.
- Renaming rebuildable context-cache files causes one cache miss per affected
  context after upgrade and leaves old files unreferenced on disk. No durable
  user data is migrated or deleted.
- The combined catalog is larger than the provider map and its canonical-only
  changes invalidate the static revision even though Toolang currently consumes
  only `providers`. The existing 32 MiB input limit and content-addressed cache
  bound the operational cost.
- Future canonical-model support must extend the snapshot deliberately rather
  than relying on the currently ignored `models` member.

## Open Questions

None. The requested scope intentionally breaks the old CLI flag and implicit
filename, keeps explicit paths filename-agnostic, and defers canonical-model
semantics.
