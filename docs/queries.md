# Resource Queries

Models, tools, and caps use native `tq-json` queries over the same records emitted
by `--json`. Use repeatable `-q/--query` options for a union of selections.
Providers support JSON inspection and external TQ; plugins and runnables have no
collection query interface. Runnables use exact language references.

## Records and output

Human tables are the default; `--human` and `--json` are mutually exclusive.
JSON output is an array, including `[]` for an empty selection. Table headers
use JSON field paths verbatim. `--all` includes unavailable or excluded records.
The default view contains effective resources.

| Collection | Identity | Human columns |
| --- | --- | --- |
| Models | `ref`: `provider/id` | `ref`, `limit.context`, `limit.output`, `modalities.input`, `tool_call`, `reasoning`, `temperature`, `structured_output`, `cost.input`, `cost.output`, `tags` |
| Providers | `id` (external TQ only) | `id`, `_toolang.available_models`, `_toolang.model_count`, `_toolang.adapters`, `_toolang.route.api`, `env`, `tags` |
| Tools | `ref`: `toolset/name` | `ref`, `description`, `source`, `tags` |
| Caps | `ref`: `kind/name` | `ref`, `description`, `source`, `tags` |

The public fields below describe the JSON projections, not a filter allowlist.
Optional catalog fields can be absent; inspect `--json` for the captured values.

| Record | Public fields |
| --- | --- |
| Model | `ref`, `id`, `name`, `description`, `family`, `attachment`, `reasoning`, `reasoning_options`, `tool_call`, `interleaved`, `structured_output`, `temperature`, `knowledge`, `release_date`, `last_updated`, `modalities`, `open_weights`, `limit`, `status`, `experimental`, `provider`, `cost`, `tags`, `_toolang.provider`, `_toolang.route.{adapter,api,env}` |
| Provider | `id`, `name`, `npm`, `api`, `doc`, `env`, `models`, `tags`, `_toolang.{model_count,available_models,adapters}`, `_toolang.route.{adapter,api,env}` |
| Tool | `ref`, `toolset`, `name`, `plugin`, `source`, `description`, `parameters`, `tags` |
| Cap | `ref`, `name`, `description`, `source`, `definition`, `line`, `tags` |

Model/provider records retain the supported models.dev nested shapes. Model
`id` is the model ID; optional `provider` is an override object. Owning provider
is `_toolang.provider`. Prices use the catalog's per-million-token units.
Provider `models` is an ID-keyed mapping in the selected effective/all scope.
Safe resolved route metadata appears under `_toolang.route`; environment entries
are requirement names, never values. Headers, bodies, options, and private
payload metadata are excluded.

Tools also expose `toolset`, `name`, `plugin`, and parameter names in `parameters`.
Caps also expose `name`, `definition`, and `line`; `source` is the canonical source
URI. Cap kinds are `psyche`, `skill`, `service`, and `prompt`. Combined and
kind-specific lists both require the full identity pattern.
HTTP and Chat inspection keep their existing response fields and envelopes;
their resource queries match these public CLI records. This does not change
internal cap source refs or metadata.

## Native TQ semantics

```sh
too models -q 'openai/*[tool_call;limit.context>=200000]' --json
too models -q '*[modalities.input has image]' --human
too tools -q '*[parameters has path]' --json
too caps -q 'skill/*[tags has all (home,remote)]' --json
```

A comma or repeated option forms a union; semicolons combine predicates.
A bare name matches only that full key, so use `*/reviewer` to match across cap
kinds. Use native `has`, `has no`, or `has all (...)` for array membership.
There are no field aliases, inferred field restrictions, or date conversions.
Missing fields and operator/type compatibility follow TQ, so unknown fields
normally produce no matches rather than setup validation errors.

Every built-in consumer parses and validates with `{"key": "ref"}`, without a
filter list. TQ validation rejects predicates on the key: select refs through
identity syntax. Its optional CLI currently omits this validation; external
parity therefore applies to validated expressions.

With the optional `tq-json[cli]` extra:

```sh
too models --json | tq -r -k ref '*[tags has all (ready,local)]'
too providers --all --json | tq -k id '*[tags has no_env]'
```

TQ CLI emits individual objects. `-r` preserves model query branch priority.
See the [TQ syntax reference](https://pypi.org/project/tq-json/).

## Tags

Tags have one meaning across collections. Group names describe the vocabulary;
the JSON field is a flat `tags` array.

| Group | Tags | Records |
| --- | --- | --- |
| Availability | `ready` | Models, providers, tools, caps |
| Access | `not_allowed` | Models, providers, tools, caps |
| Configuration | `no_env`, `no_api`, `no_adapter` | Models, providers |
| Origin | `local`, `remote` | Models, caps |
| Scope | `root`, `home`, `here` | Caps |

`ready` excludes all blockers. Independent blockers may coexist. Missing API
template variables produce `no_env`; a missing endpoint produces `no_api`.
Tags use captured setup facts, with no live probes. Providers are ready when any
scoped model is ready; otherwise they expose only blockers shared by every
model in a nonempty set. Mixed failures remain on model records.

Model Origin follows the source catalog's local-runtime declaration, not the
catalog file location. Cap Origin preserves provenance even for cached remote
caps. No kind, form, shape, editability, or action tags are added.

## Policy and directives

`[allow]`, environment overrides, `--allow`, Chat `/allow`, one-run `:allow`,
and resource directives use the same TQ syntax. The allow fields are `models`,
`tools`, `psyches`, `skills`, `services`, and `prompts`. Singular `model`
bindings accept an exact model reference.

```sh
too serve alice \
  --allow 'models=*[tool_call]' \
  --allow 'tools=fs/*' \
  --allow 'skills=skill/reviewer'
```

In allow settings, standalone `all`/`none` are case-insensitive policy sentinels
and cannot mix with queries. Resource directives use `*` for all inherited
resources and `none` for the empty set.
Policy evaluates input tags once, then publishes updated tags.
Setup adds no separate restrictions on query fields or status tags.

Models use first matching branch then source order for inspection and allow
ranking. Tools/caps retain base order. Overlapping matches are deduplicated.
Resource `=`, `+=`, and `-=` operations respectively intersect, include, and
exclude within the inherited base, always preserving base order. Includes
cannot exceed that inherited ceiling. Required-match checks remain in place.
