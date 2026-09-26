# Collection Queries

Collection queries select an ordered subset of a base collection. Non-model
collections use Toolang's typed collection-query grammar below; model queries
use `tq-json` over model records.

## Terms

| Term | Meaning |
| --- | --- |
| collection | Ordered items with unique stable keys. |
| identity | Canonical public item identifier. |
| query field | Typed public attribute addressable by a dotted path. |
| predicate | One typed condition on a query field. |
| match | An optional identity pattern plus zero or more AND predicates. |
| query | The stable, deduplicated union of one or more matches. |

For non-model collections the parsed query is a `MatchUnion` of ordered
`Match` values. Model queries use `tq-json` instead.

## Syntax

```text
query      := match ("," match)*
match      := identity-pattern? ("[" predicate (";" predicate)* "]")?
predicate  := bool-field | "!" bool-field
            | field comparator literal
            | field ("in" | "not in") "(" literal ("," literal)* ")"
comparator := "=" | "!=" | "~=" | "!~=" | "<" | "<=" | ">" | ">="
field      := identifier ("." identifier)*
```

- A top-level comma forms the union of matches.
- A match intersects its identity pattern and predicates.
- A semicolon intersects predicates.
- `in (...)` accepts any listed value within one predicate.
- Repeating a sequence predicate requires every specified element.
- An omitted identity is `*`.

```text
openai/*,anthropic/*
*[scope in (root,home);origin=remote]
*[reasoning;modalities.input=image;limit.context>=200000]
*[modalities.input=image;modalities.input=pdf]
```

Identity patterns and `~=` are case-sensitive globs; only `*` and `?` are
special. A JSON-quoted identity is exact. `=` and `!=` are exact typed
comparisons. Quote values containing whitespace or query punctuation as JSON
strings.

Supported field types determine the operators:

| Type | Operators |
| --- | --- |
| Boolean | flag, `!flag`, `=`, `!=`, `in`, `not in` |
| Text | `=`, `!=`, `~=`, `!~=`, `in`, `not in` |
| Enum | `=`, `!=`, `in`, `not in` |
| Number, date, datetime | equality, ordering, `in`, `not in` |
| Optional | Underlying operators plus `null` |
| Scalar sequence | Element operators |

## Base Collections

The queryable base collections and identities are:

| Collection | Identity |
| --- | --- |
| `models` | `provider/model` |
| `tools` | `toolset/tool` |
| `psyches` | `psyche/psyche` |
| `skills` | `skill/skill` |
| `services` | `service/service` |
| `prompts` | `prompt/prompt` |

An unqualified pattern matches the final identity component. A qualified
pattern matches the complete identity. The final component may contain the
separator.

`caps` is an umbrella, not a base collection. `too caps --query QUERY` applies
the query independently to the four cap collections and concatenates their
results. Use `skill/reviewer` to select one kind or `reviewer` to match that
name across kinds.

Terminal Chat exposes models through `/models [-a] [QUERY]` using
`tq-json`; `/tools [-a] [QUERY]` and `/caps [-a] [QUERY]` use the grammar
above. By default, the base is the
collection selected by the current session's `/allow` ceiling. `-a` changes the
base to all available resources. The remaining complete command tail is one
query and is intersected with that base. These inspection commands do not
apply or change the session ceiling.

## Ordering and Set Operations

For the non-model collections described above, results retain base-collection
order. For model inspection, TQ query branches return matches in branch order and
catalog order within each branch. `allow.models` uses the same branch priority:
matched records are moved ahead of unmatched records, which remain in catalog
order. An unset allow list preserves catalog order exactly. Overlapping matches
are deduplicated by stable ref.
For model sequence fields such as `modalities.input`, `=image` means `has image`;
`!=image` means a nonempty sequence without `image`. Explicit TQ `has no image`
also matches empty sequences.

Resource directives evaluate against one immutable base:

```text
=   active = active intersect matches
+=  active = active union matches
-=  active = active difference matches
```

An include cannot add an item outside the inherited resource base. Runnable
model directives use TQ queries but preserve the inherited base order; the
special query-branch priority applies to `allow.models`, not to directive order.

## CLI Help

Query-enabled lists expose repeatable `--query/-q`. The additional `too query`
command, discoverable through `too more`, documents the language without loading
collection data:

```text
too query --help
too query models
too query skills --json
```

The collection form shows the field contract. `too query models` identifies
`tq-json` and its identity fields; see the [tq-json syntax](https://pypi.org/project/tq-json/).
Other collections show Toolang's query operators. Tables remain compact
presentation views; their headers and composite cells do not define query fields.
Providers and plugin inventories do not support queries.

## Policy and Directives

The six allow fields are `models`, `tools`, `psyches`, `skills`, `services`,
and `prompts`. Model allow rules, model inspection, and authored model
`=`, `+=`, `-=` directives use `tq-json`. Tool/cap allow rules and their
directives use Toolang collection queries. `[allow]`, `TOOLANG_ALLOW_*`,
`--allow`, Chat `/allow` settings, and one-run `:allow` overrides follow those
per-resource query rules. Singular `model` bindings instead accept one exact
`ModelRequest` ref and do not use a collection query.

```bash
toolang serve alice \
  --allow 'models=*[streaming;tool_call]' \
  --allow 'tools=fs/*' \
  --allow 'skills=reviewer'
```

`all` and `none` are case-insensitive policy-layer sentinels only when they are
the complete value. They cannot be mixed with a query. Legacy `--filter`,
`--select`, colon predicates, empty matches, and empty predicate blocks are
invalid.
