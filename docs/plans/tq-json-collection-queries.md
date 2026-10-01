# Dynamic tq-json Resource Queries

Approved on 2026-10-01. Implementation follows this definition.

## Scope and outcome

Use TQ as the sole parser, validator, and matcher for resource selection.
Remove `too query`, Toolang's query schemas/`MatchUnion`/matcher datasets,
filter registries, and compatibility rewrites. Runnables use exact references.

| Collection | Built-in query | Config/directive selection | CLI inspection |
| --- | --- | --- | --- |
| Models, tools, caps | Repeatable `-q/--query` | Same TQ expressions at existing resource-selection surfaces | `--all`, `--human`, `--json` |
| Providers | None | No provider selection layer; select models by provider-qualified ref | `--all`, `--human`, `--json` |
| Plugins | None | Existing named configuration | Unchanged |
| Runnables | None | Exact references | No new list/query command |

All four cap kinds share this contract, including standalone cap-kind lists.
Human output remains the default; `--human` and `--json` are mutually exclusive.
JSON is an array of records, with no prose and `[]` for empty results. Models
and providers intentionally replace their current catalog-envelope/provider-map
outputs. Providers retain catalog order and support external TQ via JSON only.
These output changes apply to CLI resource lists. HTTP and Chat inspection
keep their existing response fields/envelopes while matching the same public
records. Internal cap forms, source refs, and model status bits are unchanged.

## Query semantics

Every resource-query consumer, including setup and policy validation, uses:

```python
query = Query.parse(expressions).validate({"key": "ref"})
branch = query.match(record)
```

- Validate before iteration, even on empty inputs. Supply no `filter` list;
  do not infer paths/types or reject predicates outside TQ. Setup may add error
  context but has no alternate validator or status-field restrictions.
- Match the exact public JSON projection. TQ owns missing/null/type/operator
  behavior. No identity-prefix, field-alias, sequence-operator, or date rewrite;
  preserve catalog dates and use native `has`/`has no` membership operators.
  A bare name is not expanded to `*/name`.
- Repeated options and comma branches form a union without duplicate results.
  Model inspection/allow ranking uses first matching branch then source order;
  tools/caps and resource set operations preserve base order.
- Keep inherited ceilings and `=`, `+=`, `-=` bounded by the inherited base.
  Preserve config shape checks, standalone `all`/`none` policy sentinels,
  required-match checks, and exact singular model references. These govern
  configuration/results, not query grammar.
- Policy queries see the input snapshot's tags; publish updated tags once
  afterward. No fixed-point evaluation or special rejection of status queries.
- TQ 0.1.0 validation rejects predicates on the identity key; its CLI currently
  skips that validation. Retain TQ's validator unchanged; select keys through
  identity syntax. External/internal parity applies to validated expressions.

## Records and human columns

Each owner produces one public JSON-compatible projection for matching and
rendering; original runtime objects remain separate. Field inventories below
are documentation, never query allowlists. Headers use JSON paths verbatim;
formatting may join arrays or align/truncate values but introduces no aliases.

Models/providers reuse supported
[models.dev shapes](https://github.com/anomalyco/models.dev/blob/dev/packages/core/src/schema.ts)
from `ModelFacts.to_data()` / `Provider.to_data()`. Keep nested mappings,
arrays, original dates, and all retained cost keys. Catalog input is unchanged;
unsupported upstream fields are not fabricated.

| Record | Public fields |
| --- | --- |
| Model | `ref`, `id`, `name`, `description`, `family`, `attachment`, `reasoning`, `reasoning_options`, `tool_call`, `interleaved`, `structured_output`, `temperature`, `knowledge`, `release_date`, `last_updated`, `modalities`, `open_weights`, `limit`, `status`, `experimental`, `provider`, `cost`, `tags`, `_toolang.provider`, `_toolang.route.{adapter,api,env}` |
| Provider | `id`, `name`, `npm`, `api`, `doc`, `env`, `models`, `tags`, `_toolang.{model_count,available_models,adapters}`, `_toolang.route.{adapter,api,env}` |
| Tool | `ref`, `toolset`, `name`, `plugin`, `source`, `description`, `parameters`, `tags` |
| Cap | `ref`, `name`, `description`, `source`, `definition`, `line`, `tags` |

- Model/tool `ref` is `provider/id` / `toolset/name`. Model `id` stays the
  model ID; `provider` remains an optional override object, not its owner ID.
  Owner ID is `_toolang.provider`; catalog `status` retains its lifecycle meaning.
- Cap `ref` is `kind/name`, with no `id`; `source` holds the canonical source
  URI formerly exposed as the cap view's `ref`. Deduplicate by `(ref, source)`.
  Internal source references and API response contracts remain unchanged.
- Provider `models` remains an ID-keyed mapping of model records in the same
  effective/all scope. Counts/adapters summarize that set. Catalog `api`/`env`
  stay distinct from resolved `_toolang.route` values. External TQ uses `id`.
- Public route metadata includes environment names/requirement groups, never
  values. Exclude runtime objects, connection headers/body/options, and private
  metadata, including nested provider/experimental payloads. Keep safe model
  override fields `npm`, `api`, `shape`, `mode` where present.
- Remove old synthetic query aliases (`model`, `available`, `allowed`,
  `routable`, `ready`, `catalog`, `streaming`, flattened route fields, and
  `parameters.reasoning.effort`). Preserve source `reasoning_options` instead.
  No custom traversal for paths/arrays unsupported by TQ.

| Existing human columns | Replacement columns |
| --- | --- |
| Models: `MODEL`, `CONTEXT`, `OUTPUT`, `INPUT` | `ref`, `limit.context`, `limit.output`, `modalities.input` |
| Models: `CAPABILITIES` | `tool_call`, `reasoning`, `temperature`, `structured_output` |
| Models: `PRICE ($/1M)`, optional `STATUS` | `cost.input`, `cost.output`, `tags`; explain price units in help |
| Providers: `PROVIDER`, `MODELS`, `ADAPTERS`, `DEFAULT API`, `ENV` | `id`, `_toolang.available_models`, `_toolang.model_count`, `_toolang.adapters`, `_toolang.route.api`, `env`, `tags` |
| Tools: `TOOL`, `DESCRIPTION`, optional `STATUS` | `ref`, `description`, `source`, `tags` |
| Caps: `CAP`, `DESCRIPTION`, `SCOPE`, `FORM`, `SOURCE`, optional `STATUS` | `ref`, `description`, `source`, `tags` |

## Tags

Use one ordered, deduplicated `tags: string[]`, displayed directly as `tags`.
These five groups define ten globally distinct names; shared names keep their
meaning across collections. The groups are neither JSON objects nor validators.
Emit groups and tags in the order shown in the table.

| Group (English) | Tags | Records |
| --- | --- | --- |
| Availability | `ready` | Models, providers, tools, caps |
| Access | `not_allowed` | Models, providers, tools, caps |
| Configuration | `no_env`, `no_api`, `no_adapter` | Models, providers |
| Origin | `local`, `remote` | Models, caps |
| Scope | `root`, `home`, `here` | Caps |

- Models: `ready` means allowed with a usable route. Otherwise emit the
  applicable blockers: policy exclusion, unmet environment requirements,
  missing API endpoint, missing usable adapter. Independent blockers coexist;
  none coexists with `ready`. Missing endpoint-template variables mean
  `no_env`, not an additional `no_api`. Use captured setup facts, not live
  environment reads or network probes; HTTP failures do not mean `no_api`.
- Tools/caps: materialized records are `ready` when allowed, otherwise
  `not_allowed`. This describes configured availability, not execution success.
- Providers: `ready` if any contained model is ready. Otherwise emit a blocker
  only when every model in the nonempty scoped set shares it. Mixed failures
  remain on individual model records; empty providers have no status tags.
  Provider tags are diagnostic aggregates, not a provider permission policy.
- Model Origin comes from each source snapshot's existing `local` declaration:
  local-runtime catalogs such as Ollama/llama.cpp produce `local`; ordinary
  provider catalogs produce `remote`. Capture it per model before merging and
  preserve it through route/allow updates and subsetting. A locally stored
  remote-provider catalog stays remote. This describes declared runtime origin,
  not physical host isolation; a local runtime can have a configured remote URL.
- Cap Origin preserves source provenance, including remote cached caps. Scope
  uses existing root/home/here resolution. Exactly one Origin applies to each
  model/cap, independently of readiness. Providers/tools have no Origin tags;
  nested model records retain theirs.
- No kind, editability, shape, form, or action tags; no positive/negative aliases
  or duplicated scope/origin/status fields. Preserve models.dev booleans and
  lifecycle `status` in their original fields without adding equivalent tags.

Examples using the optional external `tq-json[cli]` extra:

```sh
too models --json | tq -r -k ref '*[tags has all (ready,local)]'
too models --all --json | tq -k ref '*[tags has no_env]'
too caps --json | tq -k ref 'skill/*[tags has all (root,remote)]'
too providers --all --json | tq -k id '*[tags has no_env]'
```

Built-in queries need no CLI extra. TQ CLI emits individual objects; compare
decoded records and use `-r` for model branch ordering.

## Removal and implementation touchpoints

- Remove `too query`, registration/`too more` entry, schema output, and active
  help/doc references. List help points to JSON records and `docs/queries.md`.
- Delete the legacy query engine and retained matcher datasets after migrating
  resource consumers. Keep exact collection lookup and runtime-object isolation;
  use TQ's own branches. Move set-operation vocabulary to `types.py`, policy
  sentinels to their owner, and text formatting to `lang/format.py`.
- Delete runnable query schemas/views/fallbacks. Use the owning reference parser
  and State/program indexes for existing exact name/kind/module/line-qualified
  forms. Preserve missing/ambiguous diagnostics and ordered exact fallbacks.
  `hands`/`handoffs` keep their explicit `*`/`none` route directives and direct
  index enumeration. No runnable input goes through TQ.

Touchpoints: `common/query.py`; model serializers and model/tool collections;
`setup/{catalog,config,models,routes,watcher}.py`; State cap/runnable collections;
language query/format helpers; execution policy/resources/runnable lookup/calls;
CLI lists, registration, script/chat consumers; API cap/runnable consumers;
active docs, affected examples, and tests. Preserve API response envelopes,
plugin commands, catalog input, and routing behavior.

## Acceptance and risks

1. Models/tools/caps share TQ acceptance/errors across CLI, config, directives,
   policy, and runtime, with no field registry or expression rewrite. Cover
   dynamic nested fields, tags/membership, missing/null/type behavior, key
   restrictions, and malformed queries on empty inputs. Providers/plugins
   reject `-q`; runnables use exact lookup and never construct query records.
2. Human/JSON selection and ordering agree across effective/all views; empty
   JSON is `[]`, headers are JSON paths, and validated built-in/external queries
   agree. Model/provider shapes and safe metadata follow the record inventory.
3. Cover every tag, concurrent blockers, ready/blocker exclusion, provider
   mixed/empty aggregates, cap scope/provenance, and per-model Origin surviving
   mixed-source merging and nested provider output. Names remain globally unique.
4. Preserve inherited ceilings, bounded set operations, one-pass policy tags,
   exact/default selection, required matches, duplicate-source caps, runnable
   reference errors and route directives. Remove all old-engine imports and
   `too query` references from active code/help/docs.
5. Code commits require offline ruff check/format, ty, and full pytest per
   `AGENTS.md`. This definition requires source/example verification and
   `git diff --check` only.

Intentional compatibility changes: JSON envelopes, headers/record paths,
cap ref/source naming, native TQ predicates/full-key matching, and removal of
runnable query expressions. Update active examples/tests together. Dynamic
field typos become nonmatches; policy tags describe the input snapshot. No
open design questions remain; historical conflicting plans are superseded.
