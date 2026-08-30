# Collection Query Discovery

## Status

Approved by the human on 2026-08-30. Implementation is tracked separately.

## Goal

Keep resource-list output optimized for browsing and copying identities, while
making advanced collection-query syntax discoverable through one dedicated CLI
help command. Query is for collections configured by directives or allow
policy, not for every inventory command.

## Success Criteria

- Code and documentation consistently model a query as a `MatchUnion` of
  `Match` values.
- Existing table headers, column groupings, cell formats, and column order stay
  unchanged.
- Only real base collections own schemas; combined caps query their union.
- Per-list schema options are replaced by the hidden, data-independent
  `too query` help command.
- Singular `model`, plugin inventories, and provider diagnostics do not acquire
  collection-query semantics.

## Terms

```text
query := match ("," match)*
match := identity-pattern? predicate-block?
```

A `match` combines one identity pattern with zero or more AND predicates and
describes one subset of the base collection. A `query` returns the stable,
deduplicated union of its matches.

Collection-query APIs, docs, help, and errors use `query`, `match`, and
`predicate`, never `selector` or `alternative`. Code follows the same terms:
the parsed query value is `MatchUnion`, its members are `Match`, and parsing
uses `_parse_match()`. `MatchUnion.matches` preserves syntax order; evaluation
still returns results in base-collection order.

## `models` And `model`

| Name | Contract |
| --- | --- |
| `models` | A collection query selecting zero or more model resources. |
| `model` | One exact `ModelRequest` ref with optional typed parameters. |

Plural `models` is used by directives, allow policy, `[models]` resource
configuration, and `too models --query`. Singular `model` is used by
`[default].model`, `--default model=...`, `:model`, Chat, and run/API requests.
It does not accept globs, predicates, comma-separated matches, or invoke
`CollectionSchema`. There is no singular `model` collection schema.

## Query Surfaces

The base collections are:

```text
models  tools  psyches  skills  services  prompts
```

Each has one `CollectionDefinition` shared by its list command, directives,
allow policy, and query help. Different scopes may bind different items,
but field names and types stay identical. Catalog and runtime model datasets,
for example, share one public query schema.

Query-enabled lists are `too models`, `too tools`, the four kind-specific cap
lists, and the combined `too caps` / `caps list` view. `providers` is a
diagnostic list. `catalogs`, `adapters`, `toolsets`, `sandboxes`, and `channels`
are plugin inventories. They expose no query; users use `grep` or JSON tools.

## List Output

Existing table headers, column groupings, cell formats, and column order remain
unchanged. Tables are presentation views, not query schemas: a header need not
be a predicate field, and a composite column may continue to summarize several
attributes. Identity cells remain copyable for the common identity-list case.

`--query/-q` filters before display limits and export, but advanced predicates
are discovered through `too query`. This feature does not add query fields to
mirror composite columns or split existing display columns into operands.

## Combined Caps

`caps` is an umbrella, not a base collection or schema. The four base schemas
bind singular cap-kind identity prefixes, producing identities such as
`skill/reviewer`. Unqualified names still match the final component.

`too caps --query QUERY` applies the query independently to `psyches`,
`skills`, `services`, and `prompts`, then concatenates matches in established
aggregate order. A qualified identity selects one collection; an unqualified
identity or common predicate spans all four. Query syntax never contains a
`caps` collection name.

## Allow Policy

Public allow fields are exactly the six base collection names. Remove `caps`
from config, `TOOLANG_ALLOW_CAPS`, `--allow`, `:allow`, and HTTP policy input;
it is rejected as unknown with no alias. `AgentCeiling` keeps the four cap-kind
queries separate until resolution. Concrete `AgentResources.caps` may remain an
aggregate because it stores results, not queries.

## Query Help

Remove `--query-help` and `--query-schema` from list commands. Replace them
with the hidden top-level command:

```text
too query --help
too query COLLECTION
too query COLLECTION --json
```

`too query --help` (and bare `too query`) explains the grammar, identity
patterns, AND predicates, comma union, operators by field type, repeated
`--query` behavior, and the six supported collection names. `too query
COLLECTION` shows the fields, types, operators, and finite choices for one base
collection; `--json` emits the same schema deterministically for tooling.

`caps`, `model`, and unknown collection names fail with the supported set.
Query help reads definitions only and does not prepare agents, load catalogs,
discover plugins, or materialize items. The command exists on `too` / `toolang`,
not standalone `caps`. Help for each query-enabled list points to `too query
COLLECTION`.

`--query/-q` remains visible on query-enabled lists. Removed options are not
hidden aliases and fail as unknown.

## Scope

Included:

- query/match vocabulary across the collection-query implementation and docs;
- one shared definition for every base collection;
- preservation of existing list headers, composite columns, and formatting;
- exact singular `model` versus plural `models` query boundaries;
- combined cap query fan-out without a `caps` schema;
- removal of public `allow.caps` across config, CLI, Chat, API, and policy;
- removal of plugin/provider queries and per-list discovery options;
- the top-level `too query` help command, concise docs, and deterministic tests.

Excluded:

- query grammar, operators, ordering, or set-semantics changes;
- table header, column, cell-format, or existing JSON export changes;
- new query-enabled collections or shell completion;
- public website documentation, which may be added separately;
- renaming unrelated agent, sandbox, or runtime selector concepts;
- exposing query help through standalone `caps`.

## Design Touchpoints

- `src/toolang/common/query.py`: `MatchUnion` / `Match`, parsing, formatting,
  and schema discovery.
- `src/toolang/plugin/models/{collections,resolution}.py`,
  `src/toolang/plugin/toolsets/collections.py`, and
  `src/toolang/state/collections.py`: shared base definitions and projections.
- `src/toolang/base/types/policy.py`, `src/toolang/setup/config.py`,
  `src/toolang/execution/policy.py`, and `src/toolang/api/schemas.py`: exact
  `model` boundaries and cap-kind allow fields.
- CLI query helpers, root/caps registration, and model/tool/cap/provider/plugin
  list commands: option removal, cap fan-out, query-help routing, and unchanged
  display behavior.
- `docs/queries.md`, `docs/models.md`, `docs/caps.md`, `docs/api.md`, and their
  existing common-query, policy, API, and CLI tests.

## Acceptance Tests

1. Query parsing and formatting use `MatchUnion` containing `Match` values and
   preserve stable union, deduplication, base order, predicates, and repeated
   `--query` behavior.
2. Existing table and JSON snapshots remain unchanged for model, provider,
   plugin, tool, combined-cap, and cap-kind lists.
3. Singular `model` surfaces accept exact request refs and reject collection
   query syntax without invoking `CollectionSchema`.
4. Model catalog and runtime datasets expose identical query names/types;
   display columns remain independent of that schema.
5. `too caps --query` fans out over four schemas; qualified and unqualified
   identities behave as defined, and no `caps` schema exists.
6. Every allow surface accepts the six base names, rejects `caps`, and preserves
   cap-kind boundaries until concrete resources are assembled.
7. Resource-list help exposes `--query/-q` only where defined. Plugin/provider
   lists expose no query. Query-enabled help points to the matching `too query`
   collection. Removed discovery options fail as unknown.
8. Root help hides `query`; generic help documents the grammar and supported
   collections, and human and JSON collection help work for all six names
   without runtime loading while rejecting `caps` and `model`.
9. Existing JSON exports remain unchanged; ruff, formatting, ty, and the
   default offline pytest suite pass.

## Risks

- Table headers no longer advertise predicate names, so `too query` help must
  stay concise and easy to reach.
- Removing discovery flags and `allow.caps` is intentionally breaking; errors
  and docs must name the replacements.

## Open Questions

None.
