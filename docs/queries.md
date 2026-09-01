# Collection Queries

Collection queries select an ordered subset of one typed base collection.

## Terms

| Term | Meaning |
| --- | --- |
| collection | Ordered items with unique stable keys. |
| identity | Canonical public item identifier. |
| query field | Typed public attribute addressable by a dotted path. |
| predicate | One typed condition on a query field. |
| match | An optional identity pattern plus zero or more AND predicates. |
| query | The stable, deduplicated union of one or more matches. |

The parsed query value is a `MatchUnion`; its ordered members are `Match`
values.

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

Terminal Chat exposes the same effective collections through `/models [QUERY]`,
`/tools [QUERY]`, and `/caps [QUERY]`. The complete command tail is one query;
omitting it lists all effective items. These inspection commands do not apply
or change the session's `/allow` ceiling.

## Ordering and Set Operations

Results retain base-collection order. Reordering matches or repeated `--query`
options does not reorder results, and overlapping matches are deduplicated by
stable item key.

Resource directives evaluate against one immutable base:

```text
=   active = active intersect matches
+=  active = active union matches
-=  active = active difference matches
```

An include cannot add an item outside the inherited resource base. Query order
does not express model priority.

## CLI Help

Query-enabled lists expose repeatable `--query/-q`. The hidden `too query`
command documents the language without loading collection data:

```text
too query --help
too query models
too query skills --json
```

The collection form shows its identity, fields, types, operators, and finite
choices. Tables remain compact presentation views; their headers and composite
cells do not define query fields. Providers and plugin inventories do not
support queries.

## Policy and Directives

The six allow fields are `models`, `tools`, `psyches`, `skills`, `services`,
and `prompts`. `[allow]`, `TOOLANG_ALLOW_*`, `--allow`, Chat `/allow` settings,
one-run `:allow` overrides, and authored resource directives use collection
queries. Singular `model` bindings instead accept one exact `ModelRequest` ref
and never use this grammar.

```bash
toolang run alice \
  --allow 'models=*[streaming;tool_call]' \
  --allow 'tools=filesystem/*' \
  --allow 'skills=reviewer'
```

`all` and `none` are case-insensitive policy-layer sentinels only when they are
the complete value. They cannot be mixed with a query. Legacy `--filter`,
`--select`, colon predicates, empty matches, and empty predicate blocks are
invalid.
