# Expand Bundled Provider Coverage

Status: approved on 2026-09-24, with provider-only selection and all upstream
models retained. The human subsequently requested natural per-provider defaults
and a maintainable update process to fix the review's default-model regression.

## Goal and Success Criteria

Expand the offline fallback catalog from five providers and 15 models to the
36 providers listed below. A fresh installation must expose these providers
through `too providers --all` without downloading a replacement catalog.
Configured providers must resolve through existing adapters, subject to model
capabilities and the user's allow rules.

## Baseline Behavior

- `src/toolang/plugin/catalogs/models_dev/data/catalog.json` contains Anthropic,
  DeepSeek, Google, OpenAI, and OpenRouter, with three models each.
- `setup/routes.py` already maps the npm packages needed by the proposed list
  to `responses`, `messages`, `generate_content`, or `chat_completions`.
- Catalog presence does not establish readiness: credentials, endpoint
  resolution, installed adapters, and allow rules still apply.
- Explicit, environment, agent-home, and root catalogs replace the bundled
  catalog. Startup does not download upstream data.
- `setup/models.py` orders providers and then includes all remaining providers;
  its default provider list is not an inclusion allowlist.

## Scope and Decisions

Refresh the five existing providers (`anthropic`, `deepseek`, `google`,
`openai`, `openrouter`) and add these 31 provider IDs using their exact
upstream identities:

| Group | Added provider IDs |
| --- | --- |
| Chinese model APIs, including regional endpoints | `alibaba`, `alibaba-cn`, `minimax`, `minimax-cn`, `moonshotai`, `moonshotai-cn`, `zai`, `zhipuai`, `volcengine`, `stepfun`, `stepfun-ai`, `xiaomi`, `tencent-tokenhub` |
| Other model APIs | `xai`, `mistral`, `meta`, `llama`, `perplexity` |
| Inference platforms and gateways | `groq`, `cerebras`, `togetherai`, `fireworks-ai`, `deepinfra`, `siliconflow`, `siliconflow-cn`, `nvidia`, `huggingface`, `modelscope`, `nebius`, `novita-ai`, `vercel` |

Use the provider records from a single captured
[models.dev catalog](https://models.dev/catalog.json). The proposed IDs, npm
routes, and presence of model records were checked on 2026-09-24.
Record the implementation snapshot's capture date and SHA-256 in the PR body.
Preserve upstream `id`, `name`, `npm`, `api`, `env`, and `doc`; regional providers
remain separate records, including any shared credential variable names.

Generate the bundled provider map using the existing CLI:
`too models --catalog FULL_CATALOG --all -q 'PROVIDER/*' ... --json`.
Repeat `-q` for all 36 provider IDs. Filter only by provider: retain every
upstream model, including deprecated, preview, non-text, and non-tool models.
Use the existing JSON export normalization, which preserves modeled metadata,
omits unknown fields and runtime facts, and normalizes zero limits as unknown.
Do not invent prices, capabilities, endpoints, or reasoning controls.

The captured source produces 1,772 models and approximately 1.76 MB of JSON.
Remove the obsolete 64 KiB test bound; use the loader's existing maximum catalog
size. The generator keeps metadata canonical and model preferences explicit.

### Model Order and Maintenance

- Preserve model order within each provider through parsing, merging, caches,
  and both catalog CLI JSON exports. Keep provider priority, explicit model
  selection, and authored allow-query ordering unchanged.
- Maintain provider IDs and ordered preferred model IDs in
  `scripts/catalog-preferences.json`. Prefer general-purpose text models with
  tools; text-only providers such as Perplexity remain selectable without tools.
- `scripts/update_model_catalog.py` downloads a full source or accepts
  `--source PATH`, exports all selected providers through the existing CLI,
  promotes the preference lists, then orders remaining models by ID. It retains
  all models and metadata, including models unsuitable as automatic defaults.
- Missing providers/preferences, duplicate or empty preference lists, deprecated
  or non-text preferences, and non-tool preferences when tools are available
  fail without replacing the output. `--check` verifies reproducibility without
  writing. The script prints source provenance and replaces output atomically.
- Bump the catalog and listing cache schemas so preexisting alphabetical snapshots cannot
  silently restore the wrong default. The preference file is a maintenance
  input; installed runtimes need only the generated catalog.

This change covers catalog data, order preservation, a maintenance script,
focused acceptance tests, and documentation. It adds no adapters, route mappings,
credential behavior, network access at startup, or provider ordering rule.
It refreshes all selected providers from the same upstream snapshot, rather
than preserving stale model records. Unselected providers, including separate
subscription/coding-plan variants, remain outside this expansion.
Local Ollama and llama.cpp catalogs remain dynamically discovered.

Amazon Bedrock, Azure, Google Vertex, and Cohere are not advertised as newly
supported: their upstream npm packages lack mappings in the existing resolver.
Their integration requires a separate definition rather than a catalog-only
claim of execution support.

## Implementation Touchpoints

- `src/toolang/plugin/catalogs/models_dev/data/catalog.json`: add the records.
- `tests/unit/plugin/test_model_catalog.py`: expand bundled coverage and verify
  each new provider's effective adapter, endpoint, and credential readiness.
- `tests/integration/cli/test_model_catalog_commands.py`: exercise the bundled
  fallback with an isolated root, disabled local discovery, and synthetic env.
- `docs/models.md`: describe bundled coverage, regional entries, readiness
  requirements, and reproducible provider-only export commands.
- `scripts/catalog-preferences.json`, `scripts/update_model_catalog.py`: the
  single maintained provider/preference list and reproducible generator.
- Catalog parsing/merging, `base/types/model.py`, catalog CLI serialization,
  `common/json.py`, and setup cache/listing records: preserve semantic model order.
- `tests/unit/test_update_model_catalog.py` and cache tests: safe regeneration,
  stale preference failures, cache order, and old-schema invalidation.

## Snapshot Provenance

Keep the filename `catalog.json` and add a reserved top-level `_meta` object
containing `snapshot_date` (YYYY-MM-DD), `source_url`, and `source_sha256`.
The user approved embedding provenance directly in the catalog. The updater
records the upstream snapshot date, retaining it when the source hash matches
the existing output. New downloads default to the UTC date; unrecorded local
sources require `--snapshot-date`, which also permits an explicit correction.
Preference-only regeneration and `--check` retain provenance without date churn.
The importer accepts `_meta` on direct and combined catalogs, requires an object,
and excludes it from runtime providers and CLI exports. Existing metadata-free
catalogs continue to load. No model/provider data or order changes in this step.

## Acceptance Tests

1. The packaged file parses offline and contains exactly the 36 approved IDs;
   every provider owns at least one model, identities are unique, and the file
   meets the loader's size limit.
2. Real CLI export from the latest full upstream catalog contains exactly these
   providers and every model ID under each. Compare exported modeled metadata
   with parsed upstream records. Offline regression tests cover repeated `-q`
   and `--query`, regional IDs, nested model IDs, and export/reload, with no
   capability, status, readiness, or allow-policy filtering under `--all`.
3. With an empty environment, added providers requiring credentials are not
   ready. With synthetic required credentials, their effective model routes
   select existing adapters and concrete HTTPS endpoints. Assert regional
   endpoints and model-level overrides are honored; perform no live API calls.
4. `too providers --all --json` and `too models --all --json` expose bundled
   additions from an empty root. Normal listing includes a newly configured
   provider when allowed and excludes it when credentials are absent.
5. An explicit small catalog still replaces the bundled fallback completely;
   added records do not leak into that result.
6. Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
   `uv run pytest -n auto`, and `git diff --check` before implementation commits.
7. With only an OpenAI key, cold and warm setup select the maintained preferred
   model for normal execution and compaction, never `chatgpt-image-latest`.
   That image model remains present and exportable. Non-alphabetical model order
   survives parsing, merge, cache, repeated CLI queries, and JSON reload.
8. Repeated regeneration from the same source is byte-identical. New unpreferred
   models are retained; invalid preferences leave the existing output untouched.
9. Embedded metadata loads offline, never becomes a provider, and does not alter
   `-q --json` exports. Matching hashes retain dates; new downloads receive a UTC
   date; unrecorded local sources need a valid explicit date. Failed validation
   and read-only checks leave the output untouched.

## Risks and Tradeoffs

- A curated snapshot ages and upstream metadata may be inaccurate. Preserve
  source provenance; offline route tests establish configuration compatibility,
  not live-provider certification or price accuracy.
- Keeping every model increases package size and parse work. Catalog inclusion
  does not certify that the current adapters execute every model modality;
  capability metadata and runtime constraints remain authoritative.
- Existing credential variables can make newly bundled providers eligible.
  Provider ordering is unchanged, but automatic model/compaction selection can
  change when a newly present provider ranks earlier. Explicit model selection
  remains the way to pin behavior.
- Shared keys can enable both regional entries; preserve upstream identity and
  endpoints and document this rather than inferring a user's region.
- Perplexity's upstream models currently lack tool calls. Preserve
  that capability metadata; inclusion must not make them eligible for workflows
  that require tools.

## Open Questions

None. The provider set and implementation are approved with provider-only
selection and maintained model ordering; representative-model filtering is
explicitly excluded.
