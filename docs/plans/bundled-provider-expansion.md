# Expand Bundled Provider Coverage

Status: approved on 2026-09-24, with provider-only selection and all upstream
models retained, as requested by the human.

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
size. Keep the CLI's deterministic JSON formatting so regeneration is direct.

This change covers catalog data, focused acceptance tests, and a short coverage
note in `docs/models.md`. It adds no adapters, route mappings, credential
behavior, network access at startup, update command, or provider ordering rule.
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
selection; representative-model filtering is explicitly excluded.
