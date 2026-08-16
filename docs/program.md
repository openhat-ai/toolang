# Program Syntax

This document defines the source-level program model for `.too` files. Flow
statements are specified in [flow-syntax.md](./flow-syntax.md), and content
parsing is specified in [input-syntax.md](./input-syntax.md).


## Program Constructs

```text
agent      optional program header
with       external cap reference
struct     named structured type
context    reusable context template
instruct   reusable instruction template
psyche     inline psyche cap
skill      inline skill cap
service    inline service cap
prompt     reusable Content template
task       authored task
chore      authored recurring work
agic       model/tool executable
flow       ordered statement executable
```

Top-level agics and flows share one executable namespace. Their authored names
must be unique across both declaration kinds.


## Documentation Comments

`##!` documents the complete program. Program documentation comments must be
unindented, may appear anywhere between top-level declarations, and are joined
in source order:

```too
##! Research assistant.
agic search:
  Search for relevant sources.

##! Produces a source-backed report.
flow research:
  run search
```

`##` documents the immediately following semantic node at the same indentation
level. Consecutive `##` lines form one newline-separated document:

```too
## Search the web.
## Return source-backed evidence.
agic search:
  ## The model request body.
  Find relevant sources.

flow research:
  ## Run two searches.
  repeat 2:
    ## Run one search.
    run search
```

At the program level, `##` may document any declaration. Inside a struct it may
document a field, inside an agic it may document a message, and inside a flow
or nested flow block it may document a statement.

A blank line, an ordinary `#` comment, another syntax item, or the end of the
current scope ends attachment. Documentation comments never skip an
intervening directive or setting. Parameters and directives do not currently
accept documentation comments.


## External Caps

`with` adds one external cap reference to the authored program:

```too
with skill https://github.com/coinbase/agentic-wallet-skills/tree/main/skills/fund
```

Prepare resolves the reference and materializes it into the agent's prepared
cap set. The cap is then available to agic directives by its resolved name.

```too
agic pay:
  skills += fund

  Help with the requested wallet funding task.
```

The old top-level `use` spelling is not part of this syntax. `use` is reserved
for a future static tool-call statement.


## Inline Caps

Program-level `psyche`, `skill`, `service`, and `prompt` declarations are
inline caps. Prepare materializes them under the agent's prepared cap set while
retaining the `.too` file as their authored source.

Cap declarations use their own schemas. They are not executable declarations
and do not share the agic/flow namespace.


## Executable Signatures

Agics and flows use the same signature rules:

```text
agic [NAME] [(PARAMS)] [-> T]:
flow [NAME] [(PARAMS)] [-> T]:
```

An omitted name means `default`:

```too
agic:
  Reply directly.

agic default:
  Reply directly.
```

These two declarations have the same executable name and therefore cannot
appear together.


### Primary Input

`_` is the primary input parameter. It aligns the executable signature with
the primary runtime local used by flows.

```too
agic chat:                         # implicit _: Part[]
  Reply to {{_}}.

agic ping():                       # no primary input
  Return "pong".

agic review(_):                    # explicit _: Part[]
  Review {{_}}.

agic parse(_: Json, mode: Text):
  Parse {{_}} using {{mode}} mode.
```

Rules:

- Omitting the complete parameter list implies one required `_ : Part[]`.
- Writing `()` declares no primary input and no named parameters.
- Writing `_` explicitly declares the primary input with default type
  `Part[]`.
- `_` may declare another type explicitly.
- `_` is never optional.

The semantic AST represents `()` with no primary input and an empty named
parameter tuple.


### Named Parameters

Named parameters follow `_` when it is present:

```too
agic rewrite(_, tone: Text, audience?: Text):
  Rewrite {{_}} for {{audience}} in a {{tone}} tone.

agic deploy(env: Text, version?: Text):
  Deploy {{version}} to {{env}}.
```

Rules:

- `name` is required.
- `name?` is optional.
- An omitted type defaults to `Part[]`.
- Parameters are initialized as named runtime locals.
- Parameter names must be unique and cannot reuse `_`.

Script CLI arguments and options are derived from the selected executable's
signature. The primary input maps to positional/stdin content rather than a
synthetic `--in` option.


### Value Types

Built-in value types include:

```text
Text
Number
Boolean
Json
Part
Part[]
```

`T[]` denotes an array of any language type `T`. `S` denotes the name of any
declared struct type; neither `T` nor `S` is a literal type name that can be
written in source.

The language names map to package-level protocol values:

```text
Part    = one PerceptPart
Part[]  = one Percept
```

The `lang` AST deliberately preserves the concise `Part` and `Part[]` names.
Packages outside `toolang.lang` use `PerceptPart` and `Percept` for the same
runtime values. `Message` is a model-call and chat-projection type, not a
Toolang language value.

Value type and runtime shape are independent. A `Part[]`, `Text[]`, or other
array value normally occupies one local with `shape=item`. Only flow operations
such as scatter, storm, and map produce `shape=list` collections.


### Output

The optional `-> T` declaration is the executable's output contract:

```too
agic summarize(_) -> Text:
  Summarize {{_}}.

flow research(_: Text) -> Report:
  ...
```

For an agic:

- no declaration keeps normal unstructured assistant content
- `Text`, `Part`, and `Part[]` use content output with type validation
- `Number`, `Boolean`, `Json`, declared structs `S`, and ordinary `T[]` values
  use structured model output

A runnable applies output coercion to its final value. For an agic, this is the
terminal assistant content; for a flow, it is the final primary local.

After output coercion, script serializes the resulting value predictably:

```text
Text                 raw text
Number               canonical decimal
Boolean              true or false
Json, S, T[]          compact JSON
Part                  one JSON object
Part[]                one JSON array when explicitly declared
undeclared agic       human-readable assistant content
```


## Structured Types

`struct` defines one named record type:

```too
struct ReviewFinding:
  path: Text
  line?: Number
  severity: Text
  message: Text

struct ReviewResult:
  summary: Text
  findings: ReviewFinding[]
  patch?: Json
```

`name: Type` is required and `name?: Type` is optional. Structs may be used by
executable parameters and outputs.


## Agics

An agic is the smallest agentic model/tool loop. It may contain selection
directives, context/instruct selection, and authored model messages.

```too
agic review(_, focus?: Text) -> ReviewResult:
  models = gpt-5
  skills += review
  tools = shell
  recall = history
  context: default
  instruct: strict

  user:
    Review {{_}} with focus {{focus}}.
```

Bare authored text is an implicit user message:

```too
agic summarize(_):
  Summarize {{_}}.
```


### Directives

Common directives are:

```text
models
psyches
skills
services
tools
recall
```

Selection directives apply ordered set operations to the immutable run
snapshot:

```text
=   keep only matching selected items
+=  add matching program-scoped items
-=  remove matching selected items
```

`models` and `recall` are scalar selections and support `=` only. `recall`
accepts `default`, `none`, `history`, `memory`, or `history, memory`.

An agic directive narrows or extends only that agic's runtime setup. It does
not mutate the prepared program or affect sibling executables.


### Context And Instruct

`context` defines data prepended to the final user content. `instruct` defines
provider-neutral agent instructions. Model adapters map the assembled frame to
provider-specific roles.

Unnamed declarations define program defaults:

```too
context:
  Current agent: {{agent.name}}

instruct:
  Use tools only when they materially help.
```

Named declarations are reusable:

```too
context report:
  Include the report constraints.

instruct strict:
  Run the {{runnable.name}} runnable in strict mode.
```

The executor supplies `date` and `timezone` as flat runtime variables to both
context and instruct templates:

```too
context:
  Current date: {{date}}
  Timezone: {{timezone}}
```

`date` is the UTC calendar date on which the root run was accepted, and
`timezone` is `UTC`. Both remain fixed for the complete recursive run tree so
child runs and later model calls observe the same temporal context.

An agic may select one of each:

```too
agic report(_):
  context: report
  instruct: strict
  Write the report.
```

Selection values are:

```text
default  program default, then runtime built-in default
none     disable this layer
NAME     named declaration
```

An inline `context:` or `instruct:` body is lowered into a generated top-level
declaration named `<context:LINE>` or `<instruct:LINE>`. Omitting the statement
leaves the AST reference as `None`; runtime policy normally resolves that like
`default`. The string `"none"` explicitly disables the layer.

`system:` is not an agic message block. Use `instruct:` for instructions and
`context:` for data.


### Messages

Authored agic message roles are:

```text
user
assistant
```

Messages are model-call templates, not Toolang runtime value types. They are
assembled after selected recall in declaration order.

```too
agic simulate():
  recall = none
  user: hello
  assistant: hi
```

Message content uses the shared `Content` syntax and may read `_` and the
agic's declared parameters. Tool messages are runtime results paired with tool
calls; they cannot be authored in `Content`.


## Flows

A flow is an ordered list of static statements:

```too
flow research(_: Text) -> Report:
  scatter 8 expand
  keep relevant par 4
  rank score top 3 par 3
  gather synthesize
```

Flows use the same parameters, output declaration, directives, and executable
namespace as agics. Statement syntax, bindings, inline agics, and result shapes
are defined in [flow-syntax.md](./flow-syntax.md).

Each flow invocation starts from the `AgentResources` resolved at root-run
start. Its directives establish the resources used by agics
executed in that flow. Nested flow calls reset again, even when the nested flow
has no directives, so a flow's correction does not implicitly constrain
another independently authored flow.

Inline runnable bodies lower to generated `AgicDecl` values named
`<agic:LINE>`. The `<...>` prefix cannot be authored as an executable name, so
generated names cannot collide with user declarations.


## Prompts

A prompt is a reusable `Content` template:

````too
prompt review: ```md
---
params: path, focus?
---

Review {{path}} carefully.
{{focus}}
```
````

It is invoked with slash-prefixed content syntax:

```text
/review path=src/app.py focus="only errors"
```

`PromptCall` empty, remaining, and fenced input forms, include references, and
escaping are defined in [input-syntax.md](./input-syntax.md). A prompt call
invokes one reusable prompt template during content evaluation.


## Service Caps

`service` caps use fenced Markdown with frontmatter. Required fields are
`description`, `transport`, and `target`; optional fields are `headers` and
`env`.

````too
service github: ```md
---
description: Use this service for GitHub access.
transport: http
target: https://mcp.github.com/mcp
headers:
  Authorization: Bearer $GITHUB_TOKEN
---

Use this service when the current work needs GitHub.
```
````

For `http`, `target` is the endpoint URL. For `stdio`, it is one argv command
line executed without a shell. Header values may reference host variables with
`$NAME`; `env` declares required process environment names.


## Surface Rules

Surfaces resolve a default executable by name:

```text
script  explicit name, else default
chat    chat, else default
task    task, else default
chore   chore, else default
file    file, else default
```

Every run surface must resolve one `RunInput`, including all required named
inputs, before execution. Text surfaces first parse `RunInputText`. Script
derives its CLI from the selected
signature; chat, task, and chore input may begin with `RunOverride` lines,
and runnable shortcuts may carry `name=value` named sources.

Content evaluation turns interactive or authored `Content` into a
protocol-level `Percept` before execution. That value corresponds to language
`Part[]`. After resolving the runnable's signature, `RunExecutor` uses
language-owned input coercion to decode and validate another declared primary
type before accepting the run. The caller does not duplicate signature
parsing.

Execution context such as `cwd`, agent home, and Toolang root is runtime state,
not executable parameters.


## Instruction Layers

Toolang assembles model calls in these conceptual layers:

```text
runtime protocol
selected instruct and capability instructions
tool definitions
recalled messages
authored agic messages and current primary input
```

Runtime protocol cannot be overridden by an agic. Context remains data rather
than instructions and is prepended to the final user content. Tool definitions
remain structured model API input rather than prompt text.
