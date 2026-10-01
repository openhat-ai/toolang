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

## Records, human columns, and tags

The approved [Canonical and Inspection Resource Records](resource-record-projections.md)
definition supersedes this plan's original field inventory, provider aggregation,
provenance fields, tag placement, and human columns. Use [Resource Queries](../queries.md)
for the current public contract. The native TQ behavior and removals in this plan
remain in effect.

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
   JSON is `[]`, headers uppercase JSON field names, and validated built-in/external queries
   agree. Model/provider shapes and safe metadata follow the record inventory.
3. Cover every tag, concurrent blockers, ready/blocker exclusion, cap
   scope/provenance, and per-model Origin surviving mixed-source merging.
   Provider records and stored counts follow the superseding record definition.
   Tag names remain globally unique.
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
