# Selector Syntax

This document defines the shared selector syntax used by list filters, runtime
allow lists, and runnable directives.


## Grammar

All selector-taking surfaces accept a selector list:

```text
selector-list := selector ("," selector)*
selector      := pattern? ("[" filter-list "]")?
filter-list   := filter ("," filter)*
filter        := key ":" value | shorthand
```

Repeated CLI flags append to the same selector list. For example:

```bash
too run alice --allow "tools=shell/*,filesystem/read" \
  --allow "tools=service_use/*"
```

is equivalent to one selector list:

```text
shell/*,filesystem/read,service_use/*
```

The top-level selector list is a union. Inside one selector, the pattern and
filters are intersected. Different filter keys are intersected; repeated values
for the same filter key are unioned.

```text
reviewer[scope:here],patch[scope:root]
```

means:

```text
matches(reviewer AND scope=here) OR matches(patch AND scope=root)
```

```text
*[scope:root,scope:home,origin:remote]
```

means:

```text
(scope=root OR scope=home) AND origin=remote
```

An omitted pattern is `*`, so pure-filter selectors are valid:

```text
[scope:here]
[provider:openrouter]
[streaming]
```


## Pattern

The pattern matches identity only. It does not match metadata.

```text
family/name
family/*
name
*
```

Both family and name patterns support glob wildcards such as `*` and `?`.
Bare name patterns also support glob wildcards:

```text
gpt5-*
shell/*
skill/review-?
```

The meaning of `family` is domain-specific:

| Domain | Family | Name | Example |
| --- | --- | --- | --- |
| `model` | model family | model name | `openai/gpt-5` |
| `tool` | tool set | tool name | `shell/execute` |
| `cap` | cap kind | cap name | `skill/reviewer` |

Filters must not use identity keys such as `family`, `kind`, `name`,
`namespace`, or `ref`. Identity belongs in the pattern.


## Shorthand

Filter shorthand is domain-scoped. A shorthand is accepted only when the current
domain can translate it to exactly one `key:value` pair. Unknown or ambiguous
shorthand values are errors.

Model shorthand:

| Shorthand | Normalized filter |
| --- | --- |
| `local` | `scope:local` |
| `remote` | `scope:remote` |
| `tools` | `tools:true` |
| `streaming` | `streaming:true` |

Cap shorthand:

| Shorthand | Normalized filter |
| --- | --- |
| `root` | `scope:root` |
| `home` | `scope:home` |
| `here` | `scope:here` |
| `inline` | `form:inline` |
| `ref` | `form:ref` |
| `wired` | `form:wired` |
| `file` | `form:file` |

Tool shorthand should remain minimal. Open-ended properties such as plugin names
should use explicit `key:value` filters unless the runtime can prove the
shorthand is unambiguous.


## Domains

### Models

Model selectors use the `model` domain.

Pattern examples:

```text
openai/gpt-5
openai/*
gpt-5
*
```

Allowed filter keys:

```text
provider, adapter, scope, tools, streaming, alias, tag
```

Examples:

```text
openai/gpt-5[provider:openrouter]
*[remote,streaming]
gpt-5,o3[provider:openai]
```

`provider` is a route filter. For example, `openai/gpt-5[provider:openrouter]`
selects the OpenAI-family `gpt-5` model through the OpenRouter provider.


### Tools

Tool selectors use the `tool` domain.

Pattern examples:

```text
shell/execute
shell/*
execute
*
```

The pattern family is the tool set. CLI table output should call this column
`SET`.

Allowed filter keys:

```text
plugin
```

Examples:

```text
shell/*
filesystem/read,filesystem/write
*[plugin:core]
```


### Caps

Cap selectors use the `cap` domain.

Pattern examples:

```text
skill/reviewer
service/github
skill/*
reviewer
*
```

The pattern family is the cap kind.

Cap scopes:

| Scope | Meaning |
| --- | --- |
| `root` | Caps from the current Toolang root |
| `home` | Caps from the current agent home |
| `here` | Caps from the current source file |

Cap forms:

| Form | Meaning |
| --- | --- |
| `inline` | Defined inline in the current `.too` source |
| `ref` | Referenced by `use ...` in the current `.too` source |
| `wired` | Connected through config |
| `file` | File-backed cap from root or home cap directories |

Cap origins:

| Origin | Meaning |
| --- | --- |
| `local` | Authored from local files or inline source |
| `remote` | Fetched or referenced from a remote ref |

Allowed filter keys:

```text
scope, form, origin
```

Examples:

```text
skill/reviewer[here]
service/*[wired,home]
skill/*[scope:root,form:file]
*[scope:here,form:ref]
```


## Implicit Family

Some surfaces already specify a family. In those contexts, selector patterns
must not include `family/`.

Implicit cap family surfaces:

```bash
caps skill list --filter ...
caps service list --filter ...
```

Implicit cap family directives:

```toolang
skills = ...
services += ...
psyches -= ...
```

Valid examples:

```text
reviewer[here]
*[file]
[scope:home]
```

Invalid examples:

```text
skill/reviewer
service/github
```

All-kind cap surfaces do not have an implicit family, so `family/name` is valid:

```bash
caps list --filter "skill/reviewer,service/github[home]"
too run alice --allow "caps=skill/reviewer,service/github[home]"
```


## Runtime Policy Selectors

Runtime commands expose one field-oriented syntax:

```bash
too run alice \
  --allow models=MODEL-SELECTORS \
  --allow tools=TOOL-SELECTORS \
  --allow caps=CAP-SELECTORS \
  --default model=MODEL-SELECTOR \
  --default runnable=agic:chat
```

`--allow DOMAIN=SELECTORS` supports `models`, `tools`, `caps`, `psyches`,
`skills`, `services`, and `prompts`. Repeating one domain accumulates selectors
inside the CLI layer. A later precedence layer replaces the complete field.
`none` means an explicitly empty allowed set; `all` removes that field's
restriction. Empty text is invalid.

Root and agent `[allow]` tables use arrays, including a native empty array for
an empty set. Environment fields use `TOOLANG_ALLOW_<DOMAIN>`. The fixed
precedence is built-in, root config, agent config, process environment, then
CLI. Cap-kind fields are converted to cap selectors only after all field
overlays complete.

`--default FIELD=VALUE` and `TOOLANG_DEFAULT_<FIELD>` set singular `model` or
`runnable` bindings. `none` clears an inherited binding. A surface selection,
chat session policy, or run policy may select a different value; run policy is
the highest binding layer.

At `RunExecutor.start()`, the captured `AgentSetup.resource_filter` resolves to a
private concrete `AgentResources`. Session and run `ResourceFilter`
restrictions are then intersected with it in order and cannot restore
resources excluded by setup policy or an earlier restriction.


## List Filters

List filters use the same selector list grammar.

| Surface | Domain | Implicit family |
| --- | --- | --- |
| `too model list --filter` | `model` | none |
| `too tool list --filter` | `tool` | none |
| `caps list --filter` | `cap` | none |
| `caps skill list --filter` | `cap` | `skill` |
| `caps psyche list --filter` | `cap` | `psyche` |
| `caps service list --filter` | `cap` | `service` |
| `caps prompt list --filter` | `cap` | `prompt` |


## Directive Set Math

At agic start, each directive set starts from the nearest concrete flow
resources, or from the tree-level agent resources when there is no containing
flow:

```text
inherited = nearest flow resources or agent resources
current = inherited
```

Directives apply in source order:

```text
-=  current = current - matches(selector-list)
=   current = current & matches(selector-list)
+=  current = current | matches(selector-list)
```

The canonical operator meanings are:

| Operator | Meaning |
| --- | --- |
| `-=` | set subtraction |
| `=` | set intersection |
| `+=` | set union |

`matches(selector-list)` is always evaluated against `inherited`, not against
all setup resources. Therefore `+=` cannot grant access outside the selected
flow or agent resource base.

Every flow invocation is a resource-base reset boundary:

```text
flow resources = apply(agent resources, flow directives)
```

A flow without directives resets to the agent resources. Flow directives
constrain agics executed in that flow, but do not propagate through nested flow
calls. When a nested flow returns, subsequent sibling agics again use the
caller's immutable flow resources.

Examples:

```toolang
agic review(input):
  models = openai/gpt-5[provider:openrouter]
  tools = shell/*, filesystem/read
  skills = reviewer[here], patch[file]
  services -= github[wired]

  Review the input.
```
