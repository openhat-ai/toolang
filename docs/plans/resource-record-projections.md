# Canonical and Inspection Resource Records

Approved for implementation. This replaces the record shapes, provenance fields, and tag
placement in `tq-json-collection-queries.md`. Native TQ parsing, validation,
union ordering, bounded selection, and exact runnable references are unchanged.

## Goal and scope

Use normalized model/provider records throughout setup, and small inspection
projections for CLI output and resource queries. Keep providers and models
separate. Distinguish installed package identity from a cap's content location.

- Models, tools, and caps retain built-in queries and existing policy surfaces.
- Providers retain inspection only; plugins retain no built-in query interface.
- Plugin inventories use `name` and `package` in their default tables, without
  `--json`, `--human`, or query flags. This includes toolsets, adapters, catalogs,
  channels, and sandboxes; remove the existing adapter `--json` exception.
  Preserve toolset `--all`.
- HTTP/Chat response envelopes, plugin loading authority, execution policy,
  persisted resource identities, and runnable routing are not redesigned.

## Canonical model and provider records

The baseline is the flat output of `openhat-ai/models`, verified against its
`scripts/build.sh`, not the original nested models.dev export:

```text
providers: [{id, name, env, npm?, api?, doc?, ...}]
models:    [{id, provider: string, override?: object, name, ...}]
```

Setup adds `ref` and `_toolang` to each model; providers gain only `_toolang`.
`model.provider` joins to `provider.id`. Connection overrides remain `override`.
Providers contain neither nested model records nor a list of model IDs. Consumers
needing associated models query the model collection by `model.provider`.

Canonical model metadata contains `_toolang.tags` and `_toolang.route`.
Canonical provider metadata contains only `_toolang.model_count`,
`_toolang.ready_count`, and `_toolang.route`. Providers have no tags or
model-adapter aggregate; readiness is `ready_count > 0`. The route describes
the provider's default connection, not its models' individual overrides.
When setup materializes a model generation, it computes and stores both counts
on each canonical provider record, after route and allow resolution. Counts
cover all models owned by that provider in the captured setup: `model_count` is
the total and `ready_count` counts ready-and-allowed models. Default/`--all`
changes provider visibility, never either stored count.
No canonical top-level `tags`, duplicate `_toolang.provider`, package name,
plugin identity, or built-in/external label is added.

Normalize ownership and override names in the owning model types and serializers,
not only in the CLI. Update route/adapter consumers to read the renamed fields;
do not change how connections execute. Translate neutral catalog-plugin inputs
at the existing setup boundary. Remove nested provider export reconstruction
from `Provider.to_data()` and `ModelCatalogSnapshot.to_data()`; snapshot export
uses separate providers/models arrays of source declarations, without host
`ref`/`_toolang` additions, preserving catalog reimport. Preserve existing restrictions on which
catalog snapshots can be exported; this does not add local snapshot export.

Preserve the catalog's field nesting and values, including costs and reasoning
options, under existing catalog validation. Source connection declarations stay
under `override`; resolved credentials and runtime headers/body/options are not
part of public `_toolang` metadata. This does not require a new catalog importer
or changes to the models repository.

## Inspection records

**Canonical record** is setup's standard model/provider shape.
**Inspection record** retains the canonical record, including `_toolang`, and
adds the fields below. Provider `api` and `env` are deliberately overwritten
with resolved route values in this projection; the canonical record is unchanged.
There is no separately configurable field registry or query translation layer.

| Collection | Added or overwritten inspection field | Canonical source |
| --- | --- | --- |
| Models | `tags` | `_toolang.tags` |
| Models | `context` | `limit.context` |
| Models | `max_output` | `limit.output` |
| Models | `price` | Formatted `cost.input / cost.output` |
| Models | `input` | `modalities.input` |
| Models | `output` | `modalities.output` |
| Models | `features` | Names of enabled display capabilities |
| Providers | `models` | Format the stored `_toolang.ready_count` and `_toolang.model_count` as `"3/5"` |
| Providers | `adapter` | `_toolang.route.adapter` |
| Providers | `api` | `_toolang.route.api` (overwrites catalog `api`) |
| Providers | `env` | `_toolang.route.env` (overwrites catalog `env`) |

Numeric limits remain numeric and modalities/tags remain arrays. `price` uses
`f"{input_price:6.2f} /{output_price:6.2f}"`, in catalog per-million-token units.
Each number has a minimum width of six characters, two decimal places, and
space padding on the left, never zero padding. There is one literal space before
`/`; any spaces after it are the output number's own left padding. Larger values
expand without truncation. An unknown side is `-`, right-aligned to width six.
Keep this exact string, including padding, in inspection JSON and human output.

```json
[
  {"price": "  1.00 /  2.00"},
  {"price": " 12.00 / 34.00"},
  {"price": "123.00 /456.00"}
]
```

Small prices round to two decimal places in this display field. Original numeric
`cost.input` and `cost.output` retain their precision for comparisons.
`models` is a display count string, never a nested collection; numeric comparisons
use `_toolang.ready_count` and `_toolang.model_count`.
Provider inspection accepts the canonical provider record alone. It only
reorganizes and formats existing values; it does not receive a model collection,
traverse models, or recompute counts, readiness, or routes.

The `features` vocabulary is `reasoning`, `tool_call`, `temperature`, and
`structured_output`, in that order, including only fields whose value is true.
These replace the previous four capability columns; all original boolean fields
remain queryable. No additional top-level `route`, count, or adapter-list aliases
are added. Missing `context`, `max_output`, `adapter`, or `api` becomes null;
missing `tags`, `features`, `input`, `output`, or `env` becomes an empty array.
`price` always uses the format above, including unknown-side placeholders;
`models` always formats the stored counts, including `"0/0"`.

Every model query surface matches the inspection record, including allow policy,
directives, HTTP, and Chat. `--json` emits that exact record; human output selects
columns from it. Both `*[tags has ready]` and `*[_toolang.tags has ready]` work;
both `*[context>=200000]` and `*[limit.context>=200000]` work. Policy still evaluates
input tags once before publishing updated tags. No projection or matcher is
precomputed merely to discover available filter fields.

## Tools, caps, and plugin inventories

These records are designed directly for inspection; they need no second shape.

| Record | Fields |
| --- | --- |
| Tool | `ref`, `toolset`, `name`, `description`, `parameters`, `tags` |
| Cap | `ref`, `name`, `description`, `location`, `tags` |
| Installed plugin | `name`, `package` |

Tools retain `toolset/name` refs and parameter-name arrays. They expose neither
`plugin` nor `source`; package provenance belongs to the installed toolset list.
Internal runtime identities and plugin ownership checks remain unchanged.

`package` is the installed distribution's name, such as `toolang`, obtained from
entry-point distribution metadata without loading a factory. Missing metadata
produces null, never an invented package or a `built-in`/`external` placeholder.
Keep those internal authority classifications where loading/security uses them.

`location` is a content address suitable for opening or copying:

- Authored caps: the actual file path resolved from Toolang root; skills point
  to their `SKILL.md`. Emit an absolute path so it works outside the root cwd.
  Do not append a line number.
- Inline caps: the containing `.too` file resolved from Toolang root, with
  the declaration line appended as `path:line`.
- Configured/referenced caps: the actual content target, not the config or
  `.too` file that links it. GitHub targets become HTTPS blob/tree URLs with
  the revision already captured in the source reference. Do not append line
  numbers or generate line anchors. Keep the existing supported source forms:
  this projection does not add local configured/referenced caps, arbitrary URL
  loaders, or revision-resolution requests.
- Only inline caps use `file:line`. Other forms address independent content
  files or URLs and carry no declaration/reference line in their location.
- Never use `inline://`, `root://`, `home://`, or `github://` as display locations.
  Keep canonical source identities internal for persistence and deduplication;
  do not use the formatted location as the resource identity.

Form and Scope tags describe how the cap is attached and its owning scope;
`location` addresses only the content. Remove cap `source`, `definition`, and `line`
from this public projection, and add no separate `declared_at` or reference-site
field. Location resolution receives explicit root/source context at the call
site; it does not read global environment or perform network probes during
matching.
Keep `location` complete in JSON and human cells; exempt it from the current
120-character string truncation so copied addresses remain usable.

## Tags

Tags remain ordered and unique. Models store them under canonical
`_toolang.tags`; inspection records expose `tags`. Tools/caps use `tags` directly.
Providers have no tags in either shape.
Form tags are restored for caps by this revised design.

| Group | Tags | Records |
| --- | --- | --- |
| Availability | `ready` | Models, tools, caps |
| Access | `not_allowed` | Models, tools, caps |
| Configuration | `no_env`, `no_api`, `no_adapter` | Models |
| Origin | `local`, `remote` | Models, caps |
| Scope | `root`, `home`, `here` | Caps |
| Form | `authored`, `inline`, `configured`, `referenced` | Caps |

Model readiness/blocker and origin semantics remain unchanged.
Exactly one cap Form is emitted. No kind, editability, or action tags are added.

## Human headers and column order

The following columns are fixed, from left to right. Default output and
`--human` use the same columns; `--all` and queries change rows, never columns.
Plugin inventories have only default table output.

| Collection | Record used | Headers, in order |
| --- | --- | --- |
| Models | Inspection | `REF`, `CONTEXT`, `MAX_OUTPUT`, `PRICE`, `INPUT`, `OUTPUT`, `FEATURES`, `TAGS` |
| Providers | Inspection | `ID`, `MODELS`, `ADAPTER`, `API`, `ENV` |
| Tools | Direct record | `REF`, `DESCRIPTION`, `TAGS` |
| Caps | Direct record | `REF`, `DESCRIPTION`, `LOCATION`, `TAGS` |
| Toolsets, adapters, catalogs, channels, sandboxes | Plugin inventory | `NAME`, `PACKAGE` |

Headers uppercase the record keys, preserving underscores: `max_output` becomes
`MAX_OUTPUT`. Record keys, JSON, and query field names remain lowercase.
This is a uniform presentation rule, with no separate name mapping or aliases
such as `MODEL`, `CTX`, `MAX_OUT`, `SOURCE`, or `STATUS`.
The renderer selects keys from the record; it does not derive new fields.

- Models: `ref` comes from the canonical record; every remaining column is an
  inspection addition. Do not repeat `id`, `name`, or `provider`: `ref` already
  identifies the model and provider. `input`/`output` are modality lists;
  `max_output` is the numeric token limit.
- Providers: `id` comes from the canonical record; the remaining columns use
  the inspection values, including resolved `api`/`env`. `models` displays the
  stored ready/total counts. Do not display provider tags or `_toolang` columns.
- Tools/caps: `ref` identifies the resource, so omit the separate `name` and
  tool `toolset` columns. Tool `parameters` remains available in JSON and queries.
- Fields omitted from resource tables remain in inspection JSON and available
  to supported query surfaces; human column selection does not restrict queries.

`REF` columns have a maximum width of 40 display cells. Wrap long values,
preferably after `/`, without truncating or inserting visible characters. Short
refs stay on one line; JSON and query values remain unchanged.

Human cells render arrays as comma-separated values in record order, and null
or empty arrays as `-`. Numeric limits remain unscaled integers; `price` and
`models` use their already formatted inspection strings unchanged.

Resource lists keep JSON arrays,
empty `[]`, default human output, mutually exclusive output flags, and human-only
summaries. Plugin inventories expose only their default human tables.

## Implementation and acceptance

Touchpoints: base model types/serialization; setup assembly, routes, revisions,
and model views; adapter override access; model/tool/cap record projections;
cap location call sites; plugin discovery records; CLI inventory factories;
existing query consumers; focused docs and tests. No unrelated cleanup.

1. Setup canonical records retain normalized `provider`/`override` fields and
   add only the specified host fields. No provider export nests models; only
   the inspection record adds the `models` count string. Verify
   remote and local catalogs, routes, revisions, and snapshot serialization.
   Assert identical stored provider counts across default and `--all` views.
   Verify provider inspection using only a canonical provider record, without
   access to models or setup services.
2. Original model paths and equivalent shortcuts select identical records across
   CLI, policy, directives, and inspection APIs. Preserve TQ validation on empty inputs,
   union ordering/deduplication, and inherited ceilings.
3. Tools/models contain no plugin/package provenance. Plugin inventories show
   distribution names without loading factories and reject query/output-mode flags.
4. Cover all four cap forms: authored paths, inline `path:line`, configured remote
   content, and referenced remote content. Preserve internal source refs,
   source deduplication, and origin/scope tags independently of location text.
   Assert that cap JSON and human output have no separate `line` field/column.
   Assert that only inline locations append a line number.
   Verify long locations remain complete in JSON and human output.
5. Assert the exact human headers and order above for default and explicit human
   output, including filtered and `--all` views. Assert each resource human header
   equals its JSON field name uppercased, while JSON and queries retain lowercase
   keys. Cover long refs, slash boundaries, and Unicode display widths without
   losing characters. Keep original numeric
   values queryable alongside formatted price/count strings. Verify model output
   modalities versus max output tokens, feature derivation, provider route
   overrides, and absence of provider tags/adapter aggregates. Validate original
   and shortcut queries against emitted JSON.
   Cover exact price padding for one-, two-, and three-digit integer parts,
   two-decimal rounding, unknown prices, and values wider than six characters.
6. Update active documentation and superseded record inventories together.
   Run ruff, format, ty, full offline pytest, and diff checks before code commits.

Risks: model ownership/override field renaming has multiple runtime consumers;
test connection payload equivalence. Canonical path queries are stable, while
inspection shortcuts are explicit public additions. Form/location and plugin
package output are intentional compatibility changes. Inspection aliases take
precedence if a future catalog field collides; canonical records remain unchanged.
Implementation is limited to this approved definition.
