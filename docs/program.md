# Program Semantics

This document owns declarations, modules, signatures/defaults, documentation
binding and model instruction composition. Source syntax and CST fields belong
to [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md).
Use [flow evaluation](flow-syntax.md) for statement contracts and
[call input](call-input.md) for Content/coercion. Source style belongs to the
website [Authoring Conventions](https://toolang.ai/docs/toolang-conventions).

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

Agent homes may contain complete program modules at either of these paths:

```text
agent.too
flows/<name>.too
```

Every file has its own declarations and private type namespace. State composition
also validates main-module calls to exported flows. A flow module cannot use
structs, contexts, instructs, caps, agics, or flows declared in another file.

The agent module publicly exports all of its agics and flows. A flow module
exports exactly one Flow: either an unnamed `flow:` or `flow <name>:`, where
`<name>` exactly matches its filename stem. State uses the filename as the
public name and binds the unnamed Flow locally as its lined entry identity
`<entry:LINE>`, so renaming the file also renames the public Flow. Other
declarations in that module are private helpers, available to its flow statements
and model routing.

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
  skills += skill/fund

  Help with the requested wallet funding task.
```


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
nonempty. Both transport spellings lower to canonical `transport` metadata.
For HTTP, `target` is a URL; for stdio it stays opaque command text at the language
boundary. Markdown cap frontmatter belongs to [caps](caps.md#local-cap-frontmatter).

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
```

A flow entry is an alternative declaration, not a second entry in that module:

```too
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
- Parameter names must be unique. Except primary `_`, names cannot start or
  end with `_`; internal underscores remain valid.

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

`Part` is the canonical union of `TextPart`, `ReasoningPart`, `ImagePart`,
`AudioPart`, `DocumentPart`, `ToolCallPart` and `ToolResultPart`; `Part[]` is a typed
array of those values. `Message` is a model protocol container with a role and
ordered Parts, not a language value type. User messages accept text/image/audio/
document Parts; assistant messages additionally accept reasoning and tool calls;
tool messages require tool results. See [message types](../src/toolang/base/types/message.py).

Value type and runtime shape are independent. A `Part[]`, `Text[]`, or other
array value normally occupies one local with `shape=item`. Only flow operations
such as scatter, storm, and map produce `shape=list` collections.


### Output

The optional `-> T` declaration is the runnable's output contract:

```too
agic summarize(_) -> Text:
  Summarize {{_}}.

flow research(_: Text) -> Text:
  run summarize
```

An omitted output type defaults to `Text`. For an agic:

- omitted `-> T` keeps normal unstructured assistant content and validates it
  as `Text`
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

Templates retain structured values until rendering. `{{result.passed}}` reads a
field, `{{result.receipts.0.key}}` indexes an array, and `{{result.receipts}}`
renders compact JSON. Sections can traverse arrays and test Boolean fields
without turning `false` into a truthy string. Lookup reads data keys and array
indexes, never Python attributes or methods. Selected `Part` and `Part[]` values
retain their native message parts, including when nested in a struct. Rendering
a whole struct or ordinary array as JSON represents contained Parts as data.
Sections test native values before output formatting, including empty `Json`
strings. Direct interpolation of a `Json` string retains JSON quoting. Literal
Unicode text remains text even when it resembles an internal Part marker,
including across interpolations and prompt expansion.


## Agics

An agic is the smallest agentic model/tool loop. It may contain selection
directives, context/instruct selection, and authored model messages.

```too
instruct strict:
  Report only findings supported by the supplied change.

agic review(_, focus?: Text) -> Text:
  tools = shell/*
  recall = near
  context = default
  instruct = strict

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

Agics and flows share these directives:

| Type | Directives | Operators | Values |
| --- | --- | --- | --- |
| Q | models, tools, psyches, skills, services, prompts | `=`, `+=`, `-=` | Native TQ query; `none` or `*` |
| L | hands, handoffs | `=` | CSV runnable references; standalone `none` or `*` |
| L | recall | `=` | CSV `far`/`near`; standalone `none`, `default`, or `*` |
| V | lanes | `=` | Positive integer or `default` (4) |
| V | instruct, context | `=` | Declaration name, `none`, or `default` |

Values cannot be empty. Directives precede messages or flow statements.
Configuration directives occur at most once. Resource operations apply in source
order: `=` retains matching selected items, `+=` includes matches from the fixed
base, and `-=` excludes matches. `none` is the empty set, so `= none` clears the
selection and `+= none` / `-= none` leave it unchanged.

Before each model call, the active path's fixed resource selectors filter the
latest State in their declaring modules. Rules exclude only resources visible
in that scope; another module's private caps remain available to its own rules.
Children inherit restrictions, not an earlier list of selected resources.
`+=` restores only items allowed by ancestors and external authority ceilings.
An exec removes the outgoing runnable's rules; waiting ancestors still apply.
Setup and accepted workspace grants stay bound.

Both agics and flows accept `recall`, `hands`, and `handoffs`. Omission inherits
the immediate parent's value within the rule's scope. Root recall defaults to `far, near`; `default`
selects that default and `*` selects all available sources. `none`, `far`, `near`,
and either CSV source order select a view of the full root snapshot. `auto` is
not supported; use `far, near` to explicitly select those two sources.
The runtime exposes `_far` (summary text), `_near` (recent message data), and
`_past` (summary followed by recent messages). Excluded sources are empty.
Only root agics prepend historical messages automatically; child agics reference
these variables explicitly. Compaction updates subsequent frames throughout
the run tree.

`hands` and `handoffs` select exact runnable references with `=`. The main module
can select its own runnables and exported flows; a flow module can select only
its own declarations, including private helpers. `none` disables routes; `*`
selects all visible runnables. They do not accept queries. An explicit
selection replaces inherited routes independently of resource restrictions:

```too
agic coordinate(_: Text) -> Text:
  hands = agic:research, flow:verify
  handoffs = flow:deliver

  Coordinate the work.
```

When a setting is omitted and has no inherited value, all visible targets are
available for user-directed calls. The model may invoke them when the user names
a target; this requested-only rule is enforced by protocol guidance. An explicit
list or `*` also permits autonomous delegation within its scope. Explicit lists
and `none` are enforced by the runtime and cannot be bypassed by a user request.
Inherited lists and `none` remain effective within their module until a child
explicitly replaces them. Additional restrictions stated by the user further constrain model use.

For a named invocation with no requested follow-up, the model uses
`_toolang/exec`. When asked to summarize, compare, or otherwise process the
result afterward, it uses `_toolang/run`. Asking about parameters alone does not
execute the target; missing required input is requested before invocation.
Scope conflicts are reported without silently changing the target or operation.

A hand is a child Run: the runtime acknowledges scheduling, executes the child,
then supplies its outcome as context before the Agic continues.
A handoff replaces the current runnable in the same Run: the target continues
at the next Step and owns the Run's result. Missing but well-formed refs
remain authored routes and become available in the next model-call catalog after
the watcher publishes them. No explicit refresh action is needed.
Flows pass these route defaults to descendants. `_toolang` inner runtime tools cannot be selected
through `tools`; use `hands` or `handoffs` to authorize targets. Runtime tool
definitions remain available independently of these lists.

Each newly accepted named child Run selects the latest published State and checks
its signature against the caller's bound definition or advertised model catalog.
Missing targets and changed signatures reject the call. Accepted Runs retain
their code, types, and directives; inline Agics belong to that same plan.
Collection items select independently when accepted. Main-module
flows can call their own runnables and exported flow modules; a flow module can
call only its own runnables. Same-Run handoffs follow the same resolution rules.

Configuration changes affect newly accepted runnables without mutating active
parents or siblings. Lane defaults are 4 per parallel operation; a statement
`in N lanes` overrides only that operation.


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

Named declarations can be selected by a runnable, as in the complete example below.

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

An agic or flow may select one of each:

```too
context report:
  Include the report constraints.

instruct strict:
  Use only supplied evidence.

agic report(_):
  context = report
  instruct = strict
  Write the report from {{_}}.
```

Selection values are:

```text
default  program default, then runtime built-in default
none     disable this layer
NAME     named declaration
```

Declare prompt content at module level; runnable-local inline definitions are
not supported. An unnamed declaration overrides the module's default. `default`
always exists: absent a module declaration, runtime uses the system fallback.
Only an explicitly selected nonexistent name causes a reference error.

Omission inherits the parent's resolved selection and declaring module; roots
use their module/system default. Explicit `default` selects the current module,
while `none` disables the layer. Inherited templates render using the child's
bound parameters and runtime variables; missing dependencies fail rather than
capturing parent locals. The runtime protocol remains independent of instruct.


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

A flow is an ordered list of static statements. The
[complete flow example](flow-syntax.md#complete-example) demonstrates shaping,
filtering, ordering and iteration with self-contained inline bodies.

Flows use the same declaration defaults, resource selectors, recall, and routing
configuration as agics. Nested flows and public run/exec calls use the same
scoped resource rules, external ceilings, and replacement semantics.

Statement syntax, bindings, inline agics, and result shapes are defined in
[flow-syntax.md](./flow-syntax.md).

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
[call-input.md](./call-input.md). A
dollar prompt call invokes one reusable prompt template during content
evaluation. Slash is reserved for terminal Chat commands and does not invoke
prompts.


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

Content evaluation turns interactive or authored `Content` into `Part[]`
before execution. After resolving the runnable's signature, `RunExecutor` uses
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
| Hands/handoffs | Complete current call authorization and signatures, with `enabled` and `requested_only` attributes | `<toolang:hands>` and `<toolang:handoffs>` in `messages`, as siblings before context; independent of `context = none` |
| Selected `context` | Runtime data, not behavioral instructions | `<toolang:context>` prepended to the last authored user message; repeated as a user message on later calls |
| Prompts and authored messages | Reusable input and the runnable's conversation, including referenced primary input | `messages`, preserving authored roles |
| Far and near recall | Selected conversation summary and historical messages | Before current messages in `messages` |
| Resource and control messages | Workspace availability, loaded rules/guidance, resource changes, steering, and cancellation | Runtime-generated user messages with `toolang:` tags |
| Tool definitions | Callable tool schemas, not guidance or permission grants | Structured `tools` field |
| Output contract | The runnable's required result type | Structured `output_schema` field; adapters may add format instructions |

Hands/handoffs snapshots omit the current runnable and its ancestors on the
calling branch. Earlier handoffs, completed children, and siblings do not block
calls. If no callable targets remain for a mode, its snapshot is disabled. These
filters run before snapshot size limits; execution still rejects recursive calls.

### Selection And Priority

Runtime protocol is always present: program-default, named, and disabled
instruct selections cannot remove it. `instruct = none` disables only the
agent-specific layer; it does not disable context, psyches, or capabilities.
Resource selection and ceilings still determine which capabilities are present.
`context = none` independently disables context. The runtime wraps every nonempty
rendered context in `<toolang:context>`, including program-default and named
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
- Each Model Call receives the usable workspace names in
  `<toolang:workspace list="lab,repo1"/>` and its current workdir in
  `<toolang:workdir path="repo1://src"/>`. The list is refreshed on every call; host
  workspace roots are not exposed.
- Lifecycle controls such as run, retry, execute, fork, and rewind do not
  themselves add a model-facing lifecycle message. Published updates change
  future named calls and model-call resources; accepted code remains bound.

Skill/service recall is distinct from far/near conversation recall. A far
summary or trigger does not count as a visible guidance body. Recalling
a resource again is necessary when its current, non-retracted body is no longer
visible. Basic Toolang concepts in the protocol are not a grammar or CLI
reference; load the applicable authoring guidance before producing `.too` code
or recommending Toolang commands.

## Implementation and verification

[Program](../src/toolang/lang/ast.py) exposes the validated AST;
[lowering](../src/toolang/lang/lower.py) applies language defaults and doc binding;
[validation](../src/toolang/lang/validate.py) checks declarations and flow shapes.
[State composition](../src/toolang/state/state.py) resolves module exports and
prepared resources. [Language tests](../tests/unit/lang/) cover source contracts;
[State tests](../tests/unit/state/) cover cross-module binding.
