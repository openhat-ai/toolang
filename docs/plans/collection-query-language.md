# Define a Collection Query Language

## Status

Approved by the human on 2026-08-30 and implemented. Its collection boundaries,
terminology, and discovery surface are superseded by
`collection-query-discovery.md`.

## Goal

Replace the domain-specific selector-list implementations with one precise,
typed language for selecting an ordered subset of any configured collection.

The common implementation owns parsing, validation, matching, set operations,
ordering, errors, formatting, and schema discovery. An application supplies an
ordered typed dataset and declarative identity, field, key, and column
configuration. It does not implement a matcher, filter switch, selection loop,
or deduplication policy.

Success requires:

- one grammar for models, providers, tools, caps, runnables, IDs, Pointers, and
  future collections;
- query fields and operators derived from an explicitly public typed view;
- errors for unsupported fields, values, and operators before matching;
- explicit OR, AND, intersection, union, and difference semantics;
- stable base ordering independent of query order;
- generated human and machine-readable query schemas; and
- no domain matcher outside the common implementation.

## Verified Current Problems

- The common parser hard-codes model, tool, and cap fields while each owner also
  implements matching and selection.
- Model syntax accepts fields such as `streaming`, `alias`, and `tag` that the
  catalog matcher does not implement, producing silent empty results.
- Tool selector order can reorder output even though selectors represent union.
- Cap filtering has a scalar CLI option and a legacy CSV heuristic with different
  semantics from the common parser.
- Allow, binding, runtime, and list surfaces repeat splitting, qualification,
  cardinality, and error rules.
- Tables combine or hide query dimensions without declaring their backing keys.

## Terms

| Term | Definition |
| --- | --- |
| collection | An ordered sequence of items with unique item keys. |
| scope | Context that fixes the collection boundary before querying, such as an agent, thread, run, cap kind, or plugin family. |
| base collection | The complete ordered collection visible within the resolved scope and authority. |
| public query view | The explicitly public typed representation used for querying and table selection. |
| collection schema | The item key, identity, fields, types, operators, and identity rules. |
| collection definition | A query view and schema plus optional human column declarations. |
| dataset | A collection definition paired with one base-collection snapshot. |
| item key | The opaque stable value used for uniqueness and set operations. |
| identity | The public canonical item identifier, made from one or more components. It need not be unique. |
| identity pattern | A case-sensitive `*`/`?` glob over canonical identity. |
| query field | A typed public attribute addressable by a stable dotted path. |
| predicate | One typed condition on one query field. |
| selector | An optional identity pattern plus conjunctive predicates. |
| query | One or more alternative selectors. |
| matched set | Items in the base collection accepted by a query. |
| active set | The current resource subset updated by directives. |
| singular query | A query required to match exactly one item. |

Use **alternative** for selector OR, **conjunction** for predicate AND,
**restrict/intersection** for `=`, **include/union** for `+=`, and
**exclude/difference** for `-=`. Do not use generic `family` for an identity
component; `family` remains ordinary model metadata when exposed.

`selector list`, `filter key`, `--filter`, and `--select` are replaced by
`query`, `query field`, and `--query` on migrated surfaces.

## Query Syntax

```text
query            := selector ("," selector)*
selector         := identity-pattern? ("[" predicate-list "]")?
identity-pattern := bare-identity-pattern | json-string
predicate-list   := predicate (";" predicate)*
predicate        := bool-field | "!" bool-field
                  | field comparator literal
                  | field "in" "(" literal-list ")"
                  | field "not" "in" "(" literal-list ")"
literal-list     := literal ("," literal)*
comparator       := "=" | "!=" | "~=" | "!~=" | "<" | "<=" | ">" | ">="
field            := identifier ("." identifier)*
literal          := bare-literal | json-string | number
                  | "true" | "false" | "null"
```

Fixed meanings:

- top-level comma separates alternative selectors: OR;
- identity and predicates within one selector are conjunctive: AND;
- semicolon separates predicates;
- `in (...)` is value OR within one predicate;
- repeated predicates, including the same field, are AND;
- omitted identity means `*`;
- empty queries, selectors, predicate blocks, and value lists are invalid.

Examples:

```text
openai/gpt-5
openai/*,anthropic/*
*[scope in (root, home); origin=remote]
*[reasoning; modalities.input=image; limit.context>=200000]
*[modalities.input=image; modalities.input=pdf]
gpt-5[route.provider=openrouter; streaming]
agic:*[module=agent]
```

The repeated modality predicates require both values. Using
`modalities.input in (image, pdf)` requires either value.

### Matching And Types

- Identity patterns and `~=` use case-sensitive globs with only `*` and `?`.
- A JSON-quoted identity is exact and treats glob characters literally.
- `=` and `!=` are exact typed comparisons; they never become globs.
- `true`, `false`, and `null` are the only Boolean/null spellings.
- Values containing query punctuation or whitespace use JSON string quoting.

| Field type | Legal forms |
| --- | --- |
| `bool` | flag, `!flag`, `=`, `!=`, `in`, `not in` |
| text | `=`, `!=`, `~=`, `!~=`, `in`, `not in` |
| enum or `Literal` | `=`, `!=`, `in`, `not in` |
| integer, float, `Decimal` | equality, ordering, `in`, `not in` |
| date or datetime | equality and ordering with ISO literals |
| optional supported type | underlying operators plus `null` |
| sequence of supported scalar | element operators with sequence cardinality |

A positive predicate succeeds when any field value matches. A negative
predicate requires the field to be present and no value to match. Repeated
positive predicates on a sequence therefore express “contains all”.

Nested dataclasses and typed mappings flatten to dotted fields. Dynamic mappings
need explicitly declared finite paths. Arbitrary objects, bytes, callables,
heterogeneous unions, and untyped mappings are not queryable.

## Collection Definition

`IdentitySpec`, `QueryField`, `CollectionSchema[T]`, `CollectionDefinition[T]`,
`QueryDataset[T]`, and `CollectionQuery` are the required concepts; exact Python
names may change.

A definition declares:

- an explicitly public dataclass, `TypedDict`, typed record, or allowlisted
  subset of an existing typed record;
- an item-key path or path tuple;
- ordered identity paths, labels, and an optional separator;
- included, excluded, renamed, nested, finite, overlay, and display-only fields;
- optional table columns and their backing query-view fields.

Applications pass typed items plus typed overlay values keyed by item key. They
cannot register custom predicates. Derived facts such as model availability or
step count must be materialized as typed data before querying.

Dataset construction validates item-key uniqueness and field values. Public
identity may be shared, for example by multiple routes to one model.

Identity examples:

| Collection | Identity | Separator |
| --- | --- | --- |
| catalog model | `provider_id`, `id` | `/` |
| runtime model route | canonical provider, model | `/` |
| tool | `toolset`, `name` | `/` |
| cap | `kind`, `name` | `/` |
| runnable | `kind`, `name` | `:` |
| thread, run, StepPath, Control Pointer, URI | one canonical component | none |

For multi-component identity, the schema splits only the declared leading
components; the final component may contain the separator. An unqualified
pattern matches the final component. A qualified pattern matches the full
structure. One-component identities treat `/`, `:`, `.`, `@`, and `#` as data.

Kind-specific or run-scoped views may bind leading identity context or expose a
local identity while retaining the same global item key. Scope never becomes an
implicit predicate and a query never traverses into another collection.

Human columns consume the same public view. A single-source column uses one
canonical field. A composite column declares every backing field and cannot be
queried as formatted text. Generic formatters cover Boolean labels, joins,
ratios, numeric/date formatting, truncation, and pairs.

## Selection And Set Semantics

The processing order is:

1. resolve authority and scope;
2. materialize the ordered base snapshot;
3. validate and evaluate the query;
4. apply output pagination or limit.

Presentation limits never truncate the base before matching.

```text
matched(Q, B) = items in B accepted by any selector in Q
```

The result is always a stable subsequence of `B`, independent of selector or
repeated `--query` order. Item keys deduplicate overlapping alternatives.

Resource directives evaluate each query against immutable base `B`:

```text
matches = matched(query, B)
=       active = active intersect matches    # restrict
+=      active = active union matches        # include
-=      active = active difference matches   # exclude
```

Every result returns to base order and `include` cannot add outside `B`.

Query order never expresses model priority. `[models].default` remains an
ordered fallback list of independent singular queries: zero matches advances,
ambiguity fails, and the first unique result binds. Scalar CLI, environment, or
interactive bindings reject both zero and multiple matches.

## Known Collection Review

The design was applied to every named list command/API, stable runtime resource
set, durable inspection collection, chat selection collection, service-list
operation, and the filesystem directory listing.

| Collection | Key / identity | Main typed fields | Result |
| --- | --- | --- | --- |
| catalog models | `(provider_id, id)` / `provider/model` | capabilities, modalities, limits, costs, dates, catalog, availability | Fits; dynamic model payloads stay excluded. |
| runtime model routes | stable route key / `provider/model` | alias, route provider/adapter/scope, tags, tools, streaming, limits/prices, supported model parameters | Fits; secrets, headers, options, and base URLs stay excluded. Parameter predicates select supporting models but never set call parameters. |
| providers | `id` / `id` | name, catalog, local, ready, available/model counts, adapters, API, environment names | Fits; availability ratio uses numeric backing fields. |
| plugin inventories | `name` / `name`, scoped by plugin family | source | Fits catalogs, adapters, toolsets, sandboxes, and channels. |
| tools | model name / `toolset/name` | plugin, source, description, parameter-name summaries | Fits; JSON input schemas remain detail data. |
| caps | `(kind, name, ref)` / `kind/name` | description, scope, origin, form, source, ref, definition, editable, line | Fits combined and kind-scoped views. |
| templates | `(kind, name)` / `kind/name` | title, description, path | Fits agent, cap, task, and chore templates. |
| runnables | stable qualified ref / `kind:name` | module, kind, description, parameter summaries, route actions | Fits listing, binding, `hands`, and `handoffs`. |
| prompt completions | `name` / `name`, scoped by runnable | parameter and required-parameter names | Fits through scalar sequence projection. |
| agents | `name` / `name` | status, sandbox, port, API/WebUI availability | Fits. |
| jobs | `id` / `id`, optionally scoped by kind/stage | kind, stage, status, title, path, schedule, timestamps, runtime facts | Fits; CLI scope flags cannot be broadened by query. |
| threads | `id` / `id` | title, timestamps, origin, channel, status, peer, run count | Fits. |
| runs | `id` / `id`, optionally thread-scoped | thread/root IDs, runnable, status, timestamps, parent, error, step count | Fits; limits apply after matching. |
| steps | global StepPath / local path in a run scope | kind, status, timestamps, index, depth, parent, error | Fits scoped identity projection. |
| controls | `(target, index)` / canonical Control Pointer | scope, target, index, kind, timing, status, timestamps, error | Fits current Agent-level `inspect controls`; payloads stay detail data. |
| MCP tools/resources/templates/prompts | protocol key / name or URI | typed protocol summaries | Fits only after all remote pages form one bounded snapshot. |
| filesystem entries | resolved path / entry name, directory-scoped | path, `is_dir` | Fits; filesystem glob already has its own explicit operation. |

The `_me` task/chore/cap list tools are transports over the job and cap
definitions, not separate schemas. Nested arrays in ASTs, messages, tool
results, and detail payloads are data rather than automatic query surfaces.

Chat history, queued calls, live blocks, progress rows, and event streams are
mutable UI/event state without stable snapshot and identity guarantees. They are
not query surfaces.

MCP pagination is a correctness boundary: querying one returned page would look
complete while omitting later matches. Local query remains disabled unless all
pages are materialized or the remote service supports equivalent semantics.

The review required four changes to the initial proposal: configurable identity
separators, explicit scope, dedicated public typed views, and query-before-limit
ordering. No stable collection requires a custom predicate.

## Discovery, CLI, And Output

Every query-enabled list command exposes:

```text
--query QUERY, -q QUERY     repeatable; repeated values are alternatives
--query-help               human identity/field/operator/value/column schema
--query-schema             deterministic machine-readable schema JSON
```

The core also formats exact identities, predicates, and selectors with canonical
quoting. Help, JSON schema, completion, errors, and formatters consume the same
compiled schema.

Initial migrations:

```text
too models --query QUERY
too providers --query QUERY
too adapters --query QUERY
too tools --query QUERY
too [AGENT] caps --query QUERY
too [AGENT] <kind> list --query QUERY
caps [AGENT] list --query QUERY
caps [AGENT] <kind> list --query QUERY
```

`--query/-q` directly replaces `--filter/-f` and tool `--select`; no aliases or
second glob-only filter remain. `[allow]`, `TOOLANG_ALLOW_*`, `--allow`,
interactive policy, model/runnable bindings, resource directives, `hands`, and
`handoffs` use the same grammar. Standalone `all` and `none` remain policy-layer
sentinels and cannot be mixed with a query.

Chat pickers continue to submit concrete model and runnable refs; they do not
display query syntax. Exact refs are resolved through singular query semantics.
Fields such as `parameters.reasoning.effort` describe supported choices for
selection and never mutate the selected model request.

Legacy colon predicates, comma-separated predicate blocks, bare enum shorthand,
mixed `all`/`none`, and caps CSV rewriting produce migration errors.

Initial table mappings:

| Command | Columns and backing fields |
| --- | --- |
| `too models` | `MODEL` identity; `AVAILABLE` available; `CONTEXT` limit.context; `OUTPUT` limit.output; `INPUT` modalities.input; `CAPABILITIES` capability Booleans; `PRICE` cost.input/cost.output |
| `too providers` | `PROVIDER` identity; `NAME` name; `AVAILABLE` available_models/model_count; `ADAPTERS` adapters; `API` api; `ENV` required_env/missing_env |
| `too adapters` | `ADAPTER` identity; `SOURCE` source |
| `too tools` | `TOOLSET/TOOL` identity; `PLUGIN` plugin; `SOURCE` source; `DESCRIPTION` description |
| combined caps | `KIND/CAP` identity; `ORIGIN` origin; `FORM` form; `SCOPE` scope; `SOURCE` source |
| kind caps | `<KIND>` local identity; `ORIGIN` origin; `FORM` form; `SCOPE` scope; `SOURCE` source |

`SET` becomes `TOOLSET`. Fields omitted from compact tables remain visible in
query help. JSON may retain domain detail fields, but selection always uses the
public query view and export-only details do not become query fields.

Empty base collections print `No <items> found.` Empty matches print
`No <items> matched query.` Validation errors exit unsuccessfully.

## Scope

Included:

- common parser, schema compiler, matcher, set operations, formatting, help, and
  schema JSON;
- model, provider, plugin inventory, tool, cap, and runnable definitions;
- replacement of current selector/filter implementations and duplicate policy
  parsing;
- runtime resource and singular-binding migration;
- model/provider/adapter/tool/cap CLI migration and output alignment;
- representative schema fixtures for every reviewed stable collection; and
- documentation and acceptance tests for the direct breaking migration.

Excluded:

- sorting, ranking, user-selected projection, aggregation, joins, mutation,
  regular expressions, functions, arbitrary Boolean grouping, or compatibility
  parsing;
- query options in this change for currently unfiltered agents, jobs, templates,
  inspect collections, non-adapter plugin inventories, chat lists, or HTTP lists;
- replacement of API scope, field, archive, or pagination parameters;
- querying a partial remote page; and
- changes to resource existence, plugin loading, route resolution, cap
  precedence, persistence, or execution behavior.

Future query surfaces must configure one of the reviewed definitions and cannot
add a local matcher.

## Design Touchpoints

- `common/selectors.py` is replaced by a package-neutral query module.
- Common collection/output helpers own generic definition, column, discovery,
  and rendering adapters; domain packages own their public typed views.
- Model catalog/resolution views own catalog, provider, and route datasets.
- Tool registry/loading views own tool and plugin-inventory datasets.
- State and cap CLI own cap datasets and remove legacy qualification/rewrite
  logic.
- Setup, CLI policy, execution policy/resources, runnable routing, and chat
  selection share parsing, cardinality, and set operations.
- CLI model/plugin/cap commands adopt generic query options and output mappings.
- Selector/model/tool/cap/input/API documentation and tests migrate atomically.

The command inventory from `docs/plans/cli-inspection-commands.md` is unchanged;
this plan supersedes its `tools --filter` behavior only.
The provider-filter compatibility cycle in
`docs/plans/run-model-parameters-and-chat-selection.md` ends when this atomic
query migration lands; generated model refs remain exact `provider/model` refs.

## Acceptance Tests

1. Grammar tests prove selector OR, predicate AND, value OR, and repeated-field
   contains-all semantics.
2. Every field type accepts only its declared literals and operators; invalid or
   legacy syntax fails before iteration with schema guidance.
3. `/`, `:`, implicit, local, one-component URI/ID/Pointer, and nested model-ID
   identities format and match correctly.
4. Public query schemas are deterministic for empty/populated datasets and drive
   help, JSON schema, completion, errors, and column mappings.
5. Model, provider, plugin, tool, cap, and runnable surfaces contain no local
   matcher, filter switch, split wrapper, selection loop, or deduplication rule.
6. Results preserve base order, ignore alternative order, deduplicate overlaps,
   and apply presentation limits after matching.
7. Restrict, include, and exclude use immutable base, cannot widen beyond it,
   and return to base order.
8. Singular bindings reject zero/multiple matches; model fallback skips zero,
   fails on ambiguity, and binds the first unique query.
9. CLI, environment, config, interactive, authored, routing, and list surfaces
   parse the same query and consistently handle `all`/`none`.
10. Human, JSON, and empty outputs select the same identities and declare all
    queryable/composite backing fields.
11. Every reviewed collection fixture passes its representative query without a
    custom predicate; unsafe types and partial MCP pages are rejected.
12. The complete default verification passes.

## Risks

This is an atomic breaking change to CLI flags, authored directives,
configuration, environment values, interactive commands, and Python helpers.

Only explicitly public, allowlisted typed views may generate fields; credentials,
arbitrary mappings, heterogeneous payloads, and observed response keys remain
private. A remote collection that cannot form a complete bounded snapshot cannot
offer local query semantics.

Stable ordering removes accidental selector-order model priority. Existing users
must express fallback through `[models].default` or a singular binding.

Approval confirms `--query/-q`, the flat grammar, typed operators, stable
ordering, direct legacy removal, and the reviewed rollout scope.
