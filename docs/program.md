# Program Syntax

This document defines the source-level program model for `.too` files. Flow
statements are specified in [flow-syntax.md](./flow-syntax.md), and content
parsing is specified in [input-syntax.md](./input-syntax.md).


## Program Constructs

```text
agent      optional program header
with       external cap reference
struct     named structured type
context    reusable context prompt
instruct   reusable agent instruction profile
psyche     inline psyche cap
skill      inline skill cap
service    inline service cap
prompt     reusable content expansion
task       authored task
chore      authored recurring work
agic       model/tool executable
flow       ordered statement executable
```

Top-level agics and flows share one executable namespace. Their authored names
must be unique across both declaration kinds.


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

Core value types include:

```text
Text
Number
Boolean
Json
Path
Artifact
Part
Part[]
T[]
StructName
```

`Part` is one canonical content part. `Part[]` is ordered multimodal content.
`Message` is a model-call and chat-projection type, not a Toolang language
value.

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
- `Number`, `Boolean`, `Json`, structs, and `T[]` use structured model output

A flow validates its final primary local against its declared output type.

Script output is rendered predictably:

```text
Text                 raw text
Number               canonical decimal
Boolean              true or false
Json, struct, T[]     compact JSON
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
  patch?: Artifact
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
  Current agent: {{runtime.agent.name}}

instruct:
  Use tools only when they materially help.
```

Named declarations are reusable:

```too
context report:
  Include the report constraints.

instruct strict:
  Return only data matching {{runtime.agic.output}}.
```

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

Agic message roles are:

```text
user
assistant
tool
```

Messages are model-call templates, not Toolang runtime value types. They are
assembled after selected recall in declaration order.

```too
agic simulate():
  recall = none
  user: hello
  assistant: hi
  tool: cached result
```

Message content uses the shared content syntax and may read `_`, named locals,
and documented runtime template variables.


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

Inline runnable bodies lower to generated `AgicDecl` values named
`<agic:LINE>`. The `<...>` prefix cannot be authored as an executable name, so
generated names cannot collide with user declarations.


## Prompts

A prompt is a reusable content expansion:

````too
prompt review: ```md
---
params: path, focus?
---

Review {{path}} carefully.
{{focus}}
```
````

It is invoked with slash-like content syntax:

```text
/review src/app.py "only errors"
```

Prompt invocation, attached content, includes, and escaping are defined in
[input-syntax.md](./input-syntax.md). Prompt slashes expand content; they are
not built-in chat commands.


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

Chat, task, chore, and file surfaces require an executable that accepts primary
input and no additional required named parameters. Script may invoke any valid
signature and derives its CLI from that signature.

Interactive and authored content is parsed into `Part[]` before type coercion.
If the executable expects another primary type, the surface must decode and
validate the content explicitly rather than guessing inside the executor.

Execution context such as `cwd`, agent home, and Toolang root is runtime state,
not executable parameters.


## Instruction Layers

Toolang assembles model calls in these conceptual layers:

```text
runtime protocol
selected instruct and capability instructions
tool definitions
context data
recalled messages
authored agic messages
current primary input
```

Runtime protocol cannot be overridden by an agic. Context remains data rather
than instructions. Tool definitions remain structured model API input rather
than prompt text.
