# Collection Queries

Collection queries select an ordered subset of a typed collection. Models,
providers, adapters, tools, caps, runnables, runtime policy, and authored
resource directives use the same language.

## Terms

| Term | Meaning |
| --- | --- |
| collection | Ordered items with unique stable keys. |
| scope | Context that fixes the collection boundary before querying. |
| identity | Canonical public item identifier. |
| query field | Typed public attribute addressable by a dotted path. |
| predicate | One typed condition on a query field. |
| selector | Identity pattern plus zero or more predicates. |
| query | One or more alternative selectors. |
| singular query | Query required to match exactly one item. |

## Syntax

```text
query      := selector ("," selector)*
selector   := identity-pattern? ("[" predicate (";" predicate)* "]")?
predicate  := bool-field | "!" bool-field
            | field comparator literal
            | field ("in" | "not in") "(" literal ("," literal)* ")"
comparator := "=" | "!=" | "~=" | "!~=" | "<" | "<=" | ">" | ">="
field      := identifier ("." identifier)*
```

- A top-level comma means selector OR.
- A selector's identity and predicates are ANDed.
- A semicolon means predicate AND.
- `in (...)` means value OR within one predicate.
- Repeating a sequence predicate means “contains all”.
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

## Identities and scope

Common identities are:

| Collection | Identity |
| --- | --- |
| Catalog or runtime model | `provider/model` |
| Tool | `toolset/tool` |
| Cap | `kind/cap` |
| Runnable | `kind:runnable` |
| Provider, plugin inventory, ID, URI, Pointer | One canonical component |

An unqualified multi-component pattern matches the final component. A
qualified pattern matches the complete identity. The final component may
contain the separator. In a one-component identity, `/`, `:`, `.`, `@`, and
`#` are ordinary data.

Kind-specific cap commands and directives bind the cap kind as scope, so their
queries use local cap names:

```text
reviewer[scope=home]
*[form=authored]
```

## Ordering and set operations

Query results always retain base-collection order. Reordering selectors or
repeated `--query` options never changes result order, and overlapping
alternatives are deduplicated by stable item key.

Resource directives evaluate every query against the same immutable base:

```text
=   active = active intersect matches   # restrict
+=  active = active union matches       # include
-=  active = active difference matches  # exclude
```

`include` cannot add an item outside the inherited resource base. Query order
does not express model priority. `[models].default` is an ordered list of
independent singular queries: zero matches advances, ambiguity fails, and the
first unique match binds.

## CLI and discovery

Query-enabled list commands expose:

```text
--query QUERY, -q QUERY   Repeat to add alternatives.
--query-help             Show identity, fields, operators, and table columns.
--query-schema           Emit the same schema as deterministic JSON.
```

Current commands are `toolang models`, `providers`, `adapters`, `tools`, and
the combined or kind-specific `caps ... list` commands. Selection occurs
before presentation limits.

Each table column declares the public query fields that back it. Composite
display cells, such as availability ratios or price pairs, are not themselves
query fields.

## Runtime policy and authored directives

`[allow]`, `TOOLANG_ALLOW_*`, `--allow`, policy commands, model and runnable
bindings, and authored resource directives accept collection queries.

```bash
toolang run alice \
  --allow 'models=*[streaming;tools]' \
  --allow 'tools=filesystem/*'
```

`all` and `none` are case-insensitive policy-layer sentinels only when they are
the complete value. They cannot be mixed with a query. Use a JSON-quoted
identity such as `"all"` to query an item literally named `all`.

Legacy `--filter`, `--select`, colon predicates, comma-separated predicate
blocks, and enum shorthand are invalid.
