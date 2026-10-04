# Resource Queries

Models, tools, and caps use native `tq-json` queries over the same records emitted
by `--json`. Use repeatable `-q/--query` options for a union of selections.
Providers support JSON inspection and external TQ; plugins and runnables have no
collection query interface. Runnables use exact language references.

## Records and output

Human tables are the default; `--human` and `--json` are mutually exclusive.
JSON output is an array, including `[]` for an empty selection. Headers uppercase
the record keys without renaming them; JSON and query keys remain lowercase.
`--all` includes unavailable or excluded records; the default view contains
effective resources. Column order is fixed in both views.

| Collection | Identity | Human columns, in order |
| --- | --- | --- |
| Models | `ref`: `provider/id` | `REF`, `CONTEXT`, `MAX_OUTPUT`, `PRICE`, `INPUT`, `OUTPUT`, `FEATURES`, `TAGS` |
| Providers | `id` (external TQ only) | `ID`, `MODELS`, `ADAPTER`, `API`, `ENV` |
| Tools | `ref`: `toolset/name` | `REF`, `DESCRIPTION`, `TAGS` |
| Caps | `ref`: `kind/name` | `REF`, `DESCRIPTION`, `LOCATION`, `TAGS` |

`REF` is at most 40 display cells wide; longer refs wrap, preferably after `/`,
without losing characters. Arrays display comma-separated values; missing values
and empty arrays display `-`. Locations remain complete for copying.
Model REF continuation lines are right-aligned; other fields start on the second
line when the ref wraps. Model `CONTEXT` and `MAX_OUTPUT` display comma thousands
separators, while JSON and query values remain integers.
Model `MAX_OUTPUT`, `PRICE`, and `OUTPUT` columns are right-aligned.

### Canonical and inspection records

Setup's canonical models/providers retain the supported flat models-repository
shape. Model `provider` is a string joining to provider `id`; optional `override`
contains catalog connection declarations. Models add `ref` and `_toolang`;
providers add only `_toolang`. Model metadata contains `tags` and `route`.
Provider metadata contains `model_count`, `ready_count`, and `route`.

Providers never store model records or model-ID lists. Setup computes their
counts after resolving routes and allow policy, across all owned models.
Default/`--all` views do not change these stored counts. Consumers needing models
query the model collection by `provider`. Providers have no tags or aggregate
adapter list. Their route describes the default connection; model overrides
may choose different routes. Public routes expose `adapter`, `api`, and `env`
requirement names/groups, never credential values or runtime headers/options.

Inspection records retain canonical fields and add these shortcuts:

| Record | Added or overwritten fields | Source |
| --- | --- | --- |
| Model | `tags` | `_toolang.tags` |
| Model | `context`, `max_output` | `limit.context`, `limit.output` |
| Model | `input`, `output` | `modalities.input`, `modalities.output` |
| Model | `price` | Formatted `cost.input / cost.output` |
| Model | `features` | True `reasoning`, `tool_call`, `temperature`, `structured_output`, in that order |
| Provider | `models` | Stored `ready_count/model_count`, such as `"3/5"` |
| Provider | `adapter`, `api`, `env` | `_toolang.route` values; overwrite catalog `api`/`env` in this projection |

Price uses `f"{input_price:6.2f} /{output_price:6.2f}"` in per-million-token units:
`"  1.00 /  2.00"`. Each side is space-padded to a minimum width of six;
an unknown side uses `-` at that width. If both sides are unknown, `price` is a
single `"-"`. JSON and human output retain the same string and padding.
Original numeric cost/limit fields remain queryable without display rounding.
Missing scalar shortcuts are null; missing lists are empty arrays. `price` and
`models` always remain formatted strings.

CLI JSON and every model query surface use the inspection record. Both
`*[context>=200000]` and `*[limit.context>=200000]` work, as do `tags` and
`_toolang.tags`. Human output selects the columns above; it does not limit the
queryable fields. These inventories are documentation, never filter allowlists.
Optional catalog fields may be absent; inspect `--json` for captured values.

Tools and caps use their records directly:

| Record | Fields |
| --- | --- |
| Tool | `ref`, `toolset`, `name`, `description`, `parameters`, `tags` |
| Cap | `ref`, `name`, `description`, `location`, `tags` |

Tool `parameters` lists parameter names. Package identity belongs to plugin
inventories, not tool records. Cap `location` points to actual content: an
absolute root-resolved authored file (a skill's `SKILL.md`), inline `file:line`,
or a configured/referenced GitHub HTTPS blob/tree URL. Only inline locations
include a line. Source refs remain internal for identity/deduplication.
Cap kinds are `psyche`, `skill`, `service`, and `prompt`; combined and kind-specific
lists require the full identity pattern. HTTP and Chat inspection keep their
existing response fields and envelopes; their queries match these CLI records.

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
Inspection shortcuts are real JSON fields; there is no query rewriting, field
registry, or date conversion.
Missing fields and operator/type compatibility follow TQ, so unknown fields
normally produce no matches rather than setup validation errors.

Every built-in consumer parses and validates with `{"key": "ref"}`, without a
filter list. TQ validation rejects predicates on the key: select refs through
identity syntax. Its optional CLI currently omits this validation; external
parity therefore applies to validated expressions.

With the optional `tq-json[cli]` extra:

```sh
too models --json | tq -r -k ref '*[tags has all (ready,local)]'
too providers --all --json | tq -k id '*[_toolang.ready_count=0]'
```

TQ CLI emits individual objects. `-r` preserves model query branch priority.
See the [TQ syntax reference](https://pypi.org/project/tq-json/).

## Tags

Tags have one meaning across collections. Group names describe the vocabulary;
the inspection field is a flat `tags` array. Canonical models store it under
`_toolang.tags`; tools and caps use `tags` directly. Providers have no tags.

| Group | Tags | Records |
| --- | --- | --- |
| Availability | `ready` | Models, tools, caps |
| Access | `not_allowed` | Models, tools, caps |
| Configuration | `no_env`, `no_api`, `no_adapter` | Models |
| Origin | `local`, `remote` | Models, caps |
| Scope | `root`, `home`, `here` | Caps |
| Form | `authored`, `inline`, `configured`, `referenced` | Caps |

`ready` excludes all blockers. Independent blockers may coexist. Missing API
template variables produce `no_env`; a missing endpoint produces `no_api`.
Tags use captured setup facts, with no live probes. Tools/caps are `ready` when
allowed and `not_allowed` when excluded. Provider readiness is expressed by
`_toolang.ready_count > 0`, not tags.

Model Origin follows the source catalog's local-runtime declaration, not the
catalog file location. Cap Origin preserves provenance even for cached remote
caps. Each cap has one Form tag. No kind, shape, editability, or action tags are added.

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

## Implementation and verification

[Resource records](../src/toolang/plugin/models/records.py) and
[policy queries](../src/toolang/common/policy.py) define projections and
selection. [Record tests](../tests/unit/plugin/test_resource_records.py) and
[CLI query tests](../tests/integration/cli/test_resource_queries.py) verify native
TQ matching, output columns and default/full views.
