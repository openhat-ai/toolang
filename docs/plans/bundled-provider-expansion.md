# Expand Bundled Provider Coverage

Status: proposed; awaiting human approval before implementation.

## Goal and Success Criteria

Expand the offline fallback catalog from five providers and 15 models to the
36 providers listed below. A fresh installation must expose these providers
through `too providers --all` without downloading a replacement catalog.
Configured providers must resolve through existing adapters, subject to model
capabilities and the user's allow rules.

## Current Behavior

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

Retain the five existing providers and all 15 existing model records unchanged.
Add these 31 provider IDs using their exact upstream identities:

| Group | Added provider IDs |
| --- | --- |
| Chinese model APIs, including regional endpoints | `alibaba`, `alibaba-cn`, `minimax`, `minimax-cn`, `moonshotai`, `moonshotai-cn`, `zai`, `zhipuai`, `volcengine`, `stepfun`, `stepfun-ai`, `xiaomi`, `tencent-tokenhub` |
| Other model APIs | `xai`, `mistral`, `meta`, `llama`, `perplexity` |
| Inference platforms and gateways | `groq`, `cerebras`, `togetherai`, `fireworks-ai`, `deepinfra`, `siliconflow`, `siliconflow-cn`, `nvidia`, `huggingface`, `modelscope`, `nebius`, `novita-ai`, `vercel` |

Use the provider records from a single captured
[models.dev catalog](https://models.dev/catalog.json). The proposed IDs, npm
routes, and presence of eligible text models were checked on 2026-09-24.
Record the implementation snapshot's capture date and SHA-256 in the PR body.
Preserve upstream `id`, `name`, `npm`, `api`, `env`, and `doc`; regional providers
remain separate records, including any shared credential variable names.

For each new provider, select up to three models using this fixed policy:

1. Require text input and text output. Exclude deprecated models.
2. Rank models with tool calls before those without, then stable models before
   alpha/beta/preview/experimental models. Treat an absent status as stable
   unless the model ID explicitly identifies a preview or experimental model.
3. Within each group, sort by descending `release_date` (missing dates last),
   then ascending model ID. Keep the first three, or all if fewer exist.
4. Copy complete upstream model records without inventing prices, limits,
   reasoning controls, capabilities, or endpoint overrides. Validate them with
   Toolang's parser and resolver before including them. An incompatible record
   is an implementation blocker, not grounds to silently omit a provider.

The resulting catalog contains 46–108 models and remains below 256 KiB.
The size bound replaces the existing test's 64 KiB bound. Sort provider and
model keys for reproducible review; keep the existing compact JSON field style.

This change covers catalog data, focused acceptance tests, and a short coverage
note in `docs/models.md`. It adds no adapters, route mappings, credential
behavior, network access at startup, update command, or provider ordering rule.
It does not refresh the existing model records or include the complete upstream
catalog. Subscription/coding-plan variants are outside this initial expansion.
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
- `docs/models.md`: describe representative bundled coverage, regional entries,
  readiness requirements, and the full-catalog replacement option.

## Acceptance Tests

1. The packaged file parses offline and contains exactly the 36 approved IDs;
   every provider owns at least one model, identities are unique, and the file
   meets the size bound. All original 15 model records remain unchanged.
2. Every added model has text input/output and is not deprecated. Selection
   follows the policy above against the captured source, verified during data
   preparation without downloading anything during tests.
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
- Representative models limit package size while omitting much of each
  provider's inventory. Users can still select a full replacement catalog.
- Existing credential variables can make newly bundled providers eligible.
  Provider ordering is unchanged, but automatic model/compaction selection can
  change when a newly present provider ranks earlier. Explicit model selection
  remains the way to pin behavior.
- Shared keys can enable both regional entries; preserve upstream identity and
  endpoints and document this rather than inferring a user's region.
- Perplexity's eligible upstream models currently lack tool calls. Preserve
  that capability metadata; inclusion must not make them eligible for workflows
  that require tools.

## Open Questions

No technical design questions remain. Human approval is required for this
provider set, representative-model policy, exclusions, and automatic-selection
impact before implementation.
