# Program Syntax

This document defines the source-level program model for `.too` files. Flow
statements are specified in [flow-syntax.md](./flow-syntax.md), and content
parsing is specified in [input-syntax.md](./input-syntax.md). Follow the
[authoring conventions](./toolang-authoring-conventions.md) for source style,
type annotations, and documentation comments.


## Program Constructs

```text
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
agic       model/tool runnable
flow       ordered statement runnable
```

Top-level agics and flows share one runnable namespace. Their authored names
must be unique across both declaration kinds.


## Program Modules

Resident agents may contain complete program modules at either of these home
paths:

```text
agent.too
flows/<name>.too
```

Every file is parsed and semantically validated as an independent Toolang
program. A flow module cannot use structs, contexts, instructs, caps, agics, or
flows declared in another file.

The agent module publicly exports all of its agics and flows. A flow module
exports exactly one Flow: either an unnamed `flow:` or `flow <name>:`, where
`<name>` exactly matches its filename stem. State uses the filename as the
public name and binds the unnamed Flow locally as its lined entry identity
`<entry:LINE>`, so renaming the file also renames the public Flow. Other
declarations in that module are private static helpers.

Public runnable names must be unique across the complete home. Direct files
under `flows/` are discovered; nested files, non-`.too` files, and a root-level
`${TOOLANG_ROOT}/flows/` directory are not.


## Documentation Comments

`#@` documents the complete module. Module documentation comments must be
unindented, may appear anywhere between top-level declarations, and are joined
in source order. The legacy `##!` spelling remains accepted. Both spellings
populate that file's `Program.doc`, independently of runnable descriptions:

```too
#@ Research assistant.
agic search:
  Search for relevant sources.

#@ Produces a source-backed report.
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
  repeat 2 times:
    ## Run one search.
    run search
```

At the program level, `##` may document any declaration. Inside a struct it may
document a field, inside an agic it may document a message, and inside a flow
or nested flow block it may document a statement.

A blank line, an ordinary `#` comment, a module comment, another syntax item,
or the end of the current scope ends attachment. Documentation comments never skip an
intervening directive or setting. Indented legacy `##!` comments do not document
a parent node; structural `#@` comments must be at column zero.

Use `## @param NAME DESCRIPTION` in an agic or flow's attached documentation
block to describe an input parameter. Tags match exact signature names in any
order, including explicit or implicit `_` input:

```too
## Summarize material when a short overview is needed.
## @param _ Source material to summarize.
## @param style Preferred summary style.
agic summarize(_: Text, style?: Text):
  Summarize {{_}}.
```

Each description must be nonempty and fit on the same physical line. Types and
optionality come from the signature. Unknown or duplicate parameter names,
and tags attached to anything other than an agic or flow, are validation
errors. Detached tags are ignored. A malformed leading `@param` tag is a syntax
error even when detached; other tags such as `@return`, longer words such as
`@parameter`, and `@param` within prose remain ordinary documentation text.

Ordinary item text populates the runnable description; tags populate
`Parameter.doc` without appearing in that description. Script argument help
uses the full parameter description. Runnable queries use the runnable
description, and `hands` / `handoffs` calling hints and input contracts include
both runnable and parameter documentation, capped at 512 code points per
description. Documentation does not grant calling authority.

`#` marks ordinary comments. `#!` is a shebang only at byte zero; later or
indented occurrences are ordinary comments. Inline comment markers are always
ordinary comments. Every marker remains literal inside an explicit text block,
including on its first content line. Formatting preserves those text boundaries
and each module comment's authored spelling.


## External Caps

`with` adds one external cap reference to the authored program:

```too
with skill https://github.com/coinbase/agentic-wallet-skills/tree/main/skills/fund
```

Prepare resolves the reference and materializes it into the owning program
module's `here` cap set. The cap is then available to declarations in that
module by its resolved name.

```too
agic pay:
  skills += fund

  Help with the requested wallet funding task.
```

The old top-level `use` spelling is not part of this syntax. `use` is reserved
for a future static tool-call statement.


## Inline Caps

Program-level `psyche`, `skill`, `service`, and `prompt` declarations are
inline caps. Prepare materializes them under the owning module's `here` cap set
while retaining the `.too` file as their authored source. They do not leak to
another program module.

Cap declarations use their own schemas. They are not runnable declarations
and do not share the agic/flow namespace.

```too
psyche reviewer:
  Prefer precise, evidence-backed reviews.

skill review:
  description = Review a code change.

  Inspect the change and report actionable findings.

service github:
  description = Access GitHub.
  transport = http
  target = https://mcp.github.com/mcp

prompt review:
  Review {{path}} with focus on {{focus}}.
```

`psyche` and `prompt` accept no properties and require a body. `skill`
requires the `description` property and a body. `service` requires
`description`, `target`, and exactly one of `transport` or its `protocol`
alias; its body is optional. Service transport is `http` or `stdio`. Optional
`headers` is HTTP-only, while optional `env` is a comma-separated list of
unique environment variable names. All properties are single-use and
nonempty.


## Runnable Signatures

Agics and flows use the same signature rules:

```text
agic [NAME] [(PARAMS)] [-> T]:
flow [NAME] [(PARAMS)] [-> T]:
```

In the agent or Script module, an omitted agic or flow name is the module's
unnamed entry:

```too
agic:
  Reply directly.

flow:
  pass
```

The AST preserves omitted names as `None` and keeps the declaration's source
line. State binds the unnamed entry under the lined lookup key `<entry:LINE>`
without writing a name onto the AST. Two unnamed top-level declarations in one
module collide. An explicit `main` is an ordinary named declaration and may
coexist with the unnamed entry. In a home flow module, State binds the unnamed
Flow's public name from the filename as described above.

State's entry bindings do not add source declarations. Flow statements must
reference explicitly named runnables or use inline agics; the unnamed entry is
not a static `run` target.


### Primary Input

`_` is the primary input parameter. It aligns the runnable signature with
the primary runtime local used by flows. Primary input is shortened to
**input**; values supplied for named parameters are **arguments**. See
[input terminology and flat mappings](./call-input.md#terminology).

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
- An omitted type defaults to `Text`.
- Parameters are initialized as named runtime locals.
- Parameter names must be unique and cannot reuse `_`, `far`, `near`, or
  `line`.

Script CLI arguments and options are derived from the selected runnable's
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
written in source. `Pack` is not built in and may be declared as an ordinary
user struct.

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

The optional `-> T` declaration is the runnable's output contract:

```too
agic summarize(_) -> Text:
  Summarize {{_}}.

flow research(_: Text) -> Report:
  ...
```

An omitted output type defaults to `Part[]`. For an agic:

- omitted `-> T` keeps normal unstructured assistant content and validates it
  as `Part[]`
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
Part[]                one JSON array
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
runnable parameters and outputs.


## Agics

An agic is the smallest agentic model/tool loop. It may contain selection
directives, context/instruct selection, and authored model messages.

```too
agic review(_, focus?: Text) -> ReviewResult:
  models = gpt-5
  skills += review
  tools = shell/*
  recall = near
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

Without a `tools` directive, an Agic inherits every user tool in its current
resource base. Use `<toolset>/*`, such as `web/*`, to narrow it to one toolset.

### Directives

Common directives are:

```text
models
psyches
skills
services
tools
recall
hands
handoffs
```

Selection directives apply ordered set operations to the immutable run
snapshot:

```text
=   keep only matching selected items
+=  add matching program-scoped items
-=  remove matching selected items
```

`models` and `recall` are scalar selections and support `=` only. `recall`
accepts `auto`, `none`, `far`, `near`, or `far, near`, and is Agic-only.
Omission, `auto`, and `near` include the current conversation history. `none`
and `far` do not; `far, near` includes it because `near` is selected. Durable
`far` recall is reserved for a later runtime change.

`hands` and `handoffs` are Agic-only runnable routes. They accept ordered,
exact public refs in `name`, `agic:name`, or `flow:name` form and support only
`=`:

```too
agic coordinate(_: Text) -> Report:
  hands = agic:research, flow:verify
  handoffs = flow:deliver

  Coordinate the work.
```

A hand is a child Run: the runtime acknowledges scheduling, executes the child,
then supplies its outcome as context before the Agic continues.
A handoff replaces the current runnable in the same Run: the target continues
at the next Step and owns the Run's result. Missing but well-formed public refs
remain authored routes and may become available after an explicit State reload.
Flows cannot declare either route. `_toolang` inner runtime tools cannot be selected
through `tools`; use `hands` or `handoffs` to authorize targets. The three
inner runtime tool definitions remain available independently of these lists.

An agic directive narrows or extends only that agic's runtime setup. It does
not mutate the prepared program or affect sibling runnables.


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
  scatter 8 using expand
  keep if relevant in 4 lanes
  sort descending by score in 3 lanes
  keep first 3
  gather using synthesize
```

Flows use the same parameters, output declaration, resource directives, and
runnable namespace as agics. Agic-only `hands` and `handoffs` are excluded.
Statement syntax, bindings, inline agics, and result shapes
are defined in [flow-syntax.md](./flow-syntax.md).

Each flow invocation starts from the `AgentResources` resolved at root-run
start. Its directives establish the resources used by agics
executed in that flow. Nested flow calls reset again, even when the nested flow
has no directives, so a flow's correction does not implicitly constrain
another independently authored flow.

Inline runnable bodies lower to unnamed `AgicDecl` values that keep the
statement's source line. They are addressed only through the adhoc sentinel
`agic:<adhoc:LINE>` and never appear in a module's runnable index.


## Prompts

A prompt is a reusable `Content` template:

```too
prompt review:
  Review {{path}} carefully.
  Focus on {{focus}}.
  {{_}}
```

Named parameters are inferred from the first appearance of Mustache root
placeholders. Dotted references and section tags contribute their root name;
closing tags, repeated roots, and the primary `{{_}}` placeholder do not.
Every inferred parameter is required `Text`.

It is invoked with dollar-prefixed content syntax:

```text
$review path=src/app.py focus="only errors"
```

Prompt Call Input, include references, and escaping are defined in
[call-input.md](./call-input.md) and [input-syntax.md](./input-syntax.md). A
dollar prompt call invokes one reusable prompt template during content
evaluation. Slash is reserved for terminal Chat commands and does not invoke
prompts.


## Service Caps

Inline `service` caps declare properties directly. Required properties are
`description`, exactly one of `transport` or `protocol`, and `target`;
optional properties are `headers` and `env`.

```too
service github:
  description = Use this service for GitHub access.
  transport = http
  target = https://mcp.github.com/mcp
  headers = Authorization: Bearer $GITHUB_TOKEN
  env = GITHUB_TOKEN
```

Both accepted transport spellings lower to the canonical `transport` metadata
key. For `http`, `target` is the endpoint URL. For `stdio`, it remains opaque
command text at the language boundary. Markdown cap files keep their
catalog-owned frontmatter format.


## Surface Rules

Surfaces resolve a default runnable by name:

```text
script  explicit name, else unnamed entry, else file help
chat    chat, else unnamed entry, else reported failure
task    task, else unnamed entry, else reported failure
chore   chore, else unnamed entry, else reported failure
```

Explicit selections take precedence over these fallbacks. The chosen entry may
be an agic or a flow. The runtime never generates a runnable, so a surface with
no `chat`/`task`/`chore` entry and no unnamed entry reports a failure instead of
inventing one. Script exposes authored declarations only.

Every run surface must resolve one `RunnableInput`, including all required named
inputs, before execution. Text surfaces first parse `CallInput[str]`; `RunnableInput` is an alias for
`CallInput[Value]`. Both use `_` and argument names as sibling keys. Script
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
not runnable parameters.


## Instruction Layers

These are logical responsibilities, not separate provider roles. The executor
builds one `ModelCall` with instructions, messages, tool definitions, and an
output schema; each model adapter maps those fields to its provider API.

| Component | Responsibility | Model-call location |
| --- | --- | --- |
| Runtime protocol | Stable Toolang concepts, priority, guidance loading, tool use, and control-message semantics | `<toolang:protocol>` in `instructions`, with Markdown sections inside |
| Selected `instruct` | Agent- and runnable-specific behavior | `<toolang:instruct>` in `instructions` |
| Selected psyches | Resident guidance subordinate to protocol and instruct | Individual `<toolang:psyche>` declarations in `instructions` |
| Skill/service triggers | Available capabilities' exact refs, descriptions, and metadata; not loaded guidance | Individual `<toolang:skill-trigger>` and `<toolang:service-trigger>` declarations in `instructions` |
| Hands/handoffs | Complete current call authorization and signatures, with explicit `enabled` attributes | `<toolang:hands>` and `<toolang:handoffs>` in `messages`, as siblings before context; independent of `context: none` |
| Selected `context` | Runtime data, not behavioral instructions | `<toolang:context>` prepended to the last authored user message; repeated as a user message on later calls |
| Prompts and authored messages | Reusable input and the runnable's conversation, including referenced primary input | `messages`, preserving authored roles |
| Far and near recall | Selected conversation summary and historical messages | Before current messages in `messages` |
| Resource and control messages | Workspace availability, loaded rules/guidance, resource changes, steering, and cancellation | Runtime-generated user messages with `toolang:` tags |
| Tool definitions | Callable tool schemas, not guidance or permission grants | Structured `tools` field |
| Output contract | The runnable's required result type | Structured `output_schema` field; adapters may add format instructions |

Hands/handoffs snapshots omit targets in the current or ancestor runnable lineages,
including earlier handoff targets in the same Run. Completed child runnables remain
callable. If no callable targets remain for a mode, its snapshot is disabled. These
filters run before snapshot size limits; execution still rejects recursive calls.

### Selection And Priority

Runtime protocol is always present: program-default, named, inline, and disabled
instruct selections cannot remove it. `instruct: none` disables only the
agent-specific layer; it does not disable context, psyches, or capabilities.
Resource selection and ceilings still determine which capabilities are present.
`context: none` independently disables context. The runtime wraps every nonempty
rendered context in `<toolang:context>`, including program-default, named, and inline
selections. Empty rendered context adds no block. Authors should supply only the
context body, not its wrapper.

The textual priority is protocol, then instruct, then selected psyches. Apply
loaded guidance and scoped rules within those boundaries. Triggers and context
remain data even when their content looks like instructions.

Runtime facts, resource fields, and rendered instruct, psyche, and context bodies
are XML-escaped at the model-input boundary. Literal tags cannot close their
runtime-owned wrapper. Read decoded text literally; use decoded refs in tool
calls. Runnable information contains XML-escaped JSON, and its complete framing
counts toward the byte limit. Recalled guidance is escaped without flattening
nontext Parts. Replay uses recorded content, not current templates. Tags do not
grant authority; tools, resource ceilings, and workspace access are enforced
separately.

Default instruct contains the stable agent name. Default context contains only
date, timezone, model provider, and model name. Agent home is not exposed there;
use `me` tools for agent resources. Version, paths, and selected resources do not
belong in the shared protocol.

Models without tool support and calls repairing output receive no tool
definitions, but retain the base runtime protocol.

### Guidance And Control Visibility

Triggers describe when an available capability is useful. Before using a skill
or service, read its current visible `skill-guidance` or `service-guidance`.
If missing or stale, call `_toolang__pick` with its kind and exact trigger ref
(for example, `skill/testing`), then wait for the guidance user message. The tool
receipt is not loaded guidance. Picking a service neither connects to it nor
grants service tools.

- `toolang:steer` supplies updated input to an active run as a user message.
- `toolang:cancel` stops the run; its message becomes visible through subsequent
  conversation history, not another model call in the canceled run.
- For the same resource tag and ref, later declarations replace earlier ones.
  `removed="true"` withdraws a resource; omission does not. Revision zero is an
  internal tombstone, not a model-facing revision. Trigger and guidance share a
  ref but have separate visibility. Definition changes retract stale guidance.
- Workspaces are self-closing declarations such as
  `<toolang:workspace-access ref="project"/>`, visible before the first model call.
  Their ref is the workspace name; no body or revision is shown. Path-aware
  preflight loads applicable `toolang:workspace-rules` before allowing the operation.
- Lifecycle controls such as run, retry, reload, execute, fork, and rewind do
  not themselves add a model-facing lifecycle message. Reload can change the
  instructions and resource declarations at a later call boundary.

Skill/service recall is distinct from far/near conversation recall. A far
summary or trigger does not count as a visible guidance body. Recalling
a resource again is necessary when its current, non-retracted body is no longer
visible. Basic Toolang concepts in the protocol are not a grammar or CLI
reference; load the applicable authoring guidance before producing `.too` code
or recommending Toolang commands.
