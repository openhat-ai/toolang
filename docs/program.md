# Program Syntax

This document defines the source-level program model for `.too` files.


## Core Constructs

Current program-level constructs are:

| Construct | Meaning |
| --- | --- |
| `agent` | Program header declaring the agent name |
| `use` | One external cap reference |
| `struct` | One named structured type |
| `context` | One context prompt template |
| `instruct` | One agent instruction profile |
| `prompt` | One reusable prompt definition |
| `thunk` | One callable program entrypoint |


## Use Statements

`use` declares one external cap reference that belongs to the authored program.
During prepare, Toolang resolves the reference and materializes it into the
agent-specific prepared cap set, even when no thunk directive selects it yet.
The prepared cap has `form=ref`, `scope=here`, and `origin=remote`.

```toolang
use skill https://github.com/coinbase/agentic-wallet-skills/tree/main/skills/fund
```

After prepare, the cap is ready for runtime selection by name:

```toolang
thunk pay:
  skills += fund

  Help with the requested wallet funding task.
```


## Embedded Caps

Program-level `psyche`, `service`, and `prompt` blocks are embedded inline caps.
During prepare, Toolang materializes them into the agent-specific prepared cap
set under `.caps/inline`, preserving the `.too` file as the definition
source. This makes embedded caps visible through cap APIs and WebUI
surfaces before any thunk selects them. The prepared cap has `form=inline`,
`scope=here`, and `origin=local`.


## Instruct Declarations

`instruct` defines an agent instruction profile. It belongs to Toolang's
provider-neutral prompt model, not to one provider's `system` or `developer`
role. Runtime renders the selected instruct with the current model, tool, cap,
sandbox, agent, program, thunk, and job context, then the model adapter maps the
assembled prompt frame to the target provider API.

Toolang has one built-in default agent instruct, so an empty program still has
useful behavior. The built-in default includes selected psyches, skills,
and services in XML-delimited sections so their
boundaries stay explicit. Each selected capability family is split into an
`instruction` part and an `available` list, and the whole family section is
omitted when the selected set is empty. A program may customize that default
with one unnamed declaration:

```toolang
instruct:
  You are {{runtime.agent.name}}.
  Use the selected tools only when they materially help.
```

Named instructs define reusable alternatives:

```toolang
instruct strict-json:
  Return only JSON matching {{runtime.thunk.output}}.
```

`default` and `none` are reserved instruct names because thunk-level
`instruct:` blocks use them as control values.

`psyche` files are persistent agent-instruction fragments. To change default
agent behavior without editing `agent.too`, add Markdown files under
`${TOOLANG_ROOT}/psyches/` or `${TOOLANG_ROOT}/agents/<agent>/psyches/`.
Those psyche prompts are exposed in the run template context whenever they are
selected by the current activation and thunk cap directives. The built-in
default instruct renders them in the agent instruction layer.


## Context Declarations

`context` defines a reusable context prompt template. Runtime renders the
selected context with the same run context used for instruct rendering, then
prepends it to the final user message as data. The built-in default context
uses an XML-delimited `<context>` section and includes agent name, agent home,
sandbox, and model identity.

An unnamed declaration is the program default:

```toolang
context:
  Current agent: {{runtime.agent.name}}
```

Named contexts define reusable alternatives:

```toolang
context report:
  Include report constraints before the current request.
```

`default` and `none` are reserved context names because thunk-level `context:`
blocks use them as control values.


## Thunk

A thunk is one callable entrypoint.

Each thunk has three parts:

| Part | Meaning |
| --- | --- |
| `signature` | Name, params, and return contract |
| `directives` | Model and capability selection for that thunk |
| `blocks` | Instruct, context, user, assistant, and tool blocks |

Recommended shape:

```toolang
thunk [name](params...) [-> ReturnType]:
  directives...
  context: default
  instruct: default
  user:
    ...
```


## Signature

### Name

Thunk names follow these rules:

| Rule | Meaning |
| --- | --- |
| explicit name | One stable callable name |
| omitted name | Canonical name is `default` |
| `default` | Reserved default implementation name |

Examples:

```toolang
thunk default(input: Message):
  user:
    Help the user directly.
```

```toolang
thunk(input: Message):
  user:
    Help the user directly.
```

These two forms are equivalent.


### Params

Thunk parameters use two categories:

| Category | Meaning |
| --- | --- |
| `input` | Reserved message parameter |
| other params | Structured named arguments |

### `input`

`input` is a reserved parameter name.

When present, it means the thunk accepts one canonical `Message`.

Examples:

```toolang
thunk chat(input: Message):
  user:
    Reply to the current user message.
```

```toolang
thunk diagnose(input: Message, target?: Text, mode?: Text):
  user:
    Use the message together with named params.
```

### Named Params

All non-`input` params are structured named arguments.

Parameter rules:

| Rule | Meaning |
| --- | --- |
| `?` suffix | optional param |
| explicit type | required for every parameter |

Examples:

```toolang
thunk deploy(env: Text, version?: Text):
  user:
    Deploy the current project.
```

```toolang
thunk deploy(env: Text, dry_run?: Boolean, retries?: Number):
  user:
    Deploy the current project.
```


### Param Types

Built-in parameter types are:

| Type | Meaning |
| --- | --- |
| `Text` | One string value |
| `Number` | One JSON number |
| `Boolean` | One boolean value |
| `Json` | One arbitrary JSON-compatible value |
| `Path` | One filesystem path |
| `Artifact` | One saved artifact descriptor |
| `Message` | One full canonical message |
| `T[]` | One array of `T` |
| `StructName` | One structured value validated against a declared `struct` |

`input` must be typed as `Message`.


### Returns

The default thunk return type is `Message`.

That means:

```toolang
thunk chat(input: Message):
  user:
    Reply directly.
```

is equivalent to:

```toolang
thunk chat(input: Message) -> Message:
  user:
    Reply directly.
```

Recommended return contracts are:

| Return Type | Meaning |
| --- | --- |
| `Message` | One full canonical message |
| `Text` | One text projection |
| `Json` | One arbitrary JSON value |
| `Artifact` | One saved artifact descriptor |
| `StructName` | One structured output value |


## Struct

`struct` defines one named structured type.

Example:

```toolang
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

Structs are used by:

| Use | Meaning |
| --- | --- |
| thunk params | Structured named input |
| thunk returns | Structured output contract |

Struct field rules:

| Rule | Meaning |
| --- | --- |
| `name: Type` | Required field |
| `name?: Type` | Optional field |


## Thunk Directives

Thunk directives configure one thunk without changing its input or output
contract.

Recommended directives are:

| Directive | Meaning |
| --- | --- |
| `models = ...` | Keep only matching models from the current activation set |
| `psyches = ...` | Keep only matching psyches from the current activation set |
| `skills = ...` | Keep only matching skills from the current activation set |
| `services = ...` | Keep only matching services from the current activation set |
| `tools = ...` | Keep only matching tools from the current activation set |
| `hands = ...` | Keep only matching sub-thunks this thunk may call |
| `handoffs = ...` | Keep only matching thunks this thunk may transfer control to |
| `recall = ...` | Select retrieved message sources for model-call assembly |

Capability directives use names. `models = ...` uses model selectors such as
route-neutral refs, aliases, globs, and bracket filters.
Routing directives use thunk names.

Example:

```toolang
thunk review(input: Message, path?: Path) -> ReviewResult:
  models = gpt-5
  psyches += rigorous
  skills += review, patch
  services += github
  tools = shell, service_use
  hands += summarize_findings
  recall = history, memory

  user:
    Review the target and return actionable findings.
```

### Scalar and Set Directives

Directives are ordered set operations on the activation set. The activation set is
computed before the program runs from placement defaults and CLI options.
Program caps and references form the program set.

| Form | Meaning |
| --- | --- |
| `models = gpt-5, o3` | Keep only matching models from the current set |
| `skills = review, patch` | Keep only matching skills from the current set |
| `skills += review, patch` | Add program-scoped skills to the current set |
| `skills -= review, patch` | Remove matching skills from the current set |

Rules:

| Rule | Meaning |
| --- | --- |
| initial current set | start from the activation set |
| no directive | use the activation set unchanged |
| `+=` | union the current set with named program-set items |
| `-=` | subtract matching selectors from the current set |
| `=` | intersect the current set with matching selectors |
| ordered CSV | preserve source order |
| no `default` keyword | `default` is treated like any other selector text |
| model selectors | `models = ...` may use refs, aliases, globs, and bracket filters |
| program-scoped additions | `+=` operands must name items from the program set |
| filter selectors | `=` and `-=` operands may be arbitrary selectors |

`=` is a keep-only filter. It does not add resources that were not already in
the current set.

`models` and `recall` are scalar directives and support only `=`.

`recall` values are:

| Value | Meaning |
| --- | --- |
| `default` | Use runtime recall policy |
| `none` | Disable retrieved history and memory for this thunk |
| `history` | Include thread history |
| `memory` | Include memory retrieval |
| `history, memory` | Include both explicit sources |


## Thunk Instruct Blocks

Each thunk may declare at most one `instruct:` block. If it omits the block,
Toolang behaves as if the thunk declared `instruct: default`.

Supported forms are:

| Form | Meaning |
| --- | --- |
| `instruct: default` | Use the program default agent instruct, or the runtime built-in default if the program has no unnamed instruct. The runtime built-in default includes selected psyches, skills, and services in XML-delimited sections |
| `instruct: none` | Do not apply an agent instruct profile for this thunk; runtime instructions, messages, and context still apply |
| `instruct: name` | Use the named program instruct as this thunk's agent instruct profile |
| indented or fenced `instruct:` block | Use this block text as a thunk-local agent instruct profile |

Examples:

```toolang
thunk review(input: Message):
  instruct: strict-json

  user:
    Review the target carefully.
```

```toolang
thunk quiet(input: Message):
  instruct: none

  user:
    Reply without an agent instruct profile.
```

```toolang
thunk summarize(input: Message, audience?: Text):
  instruct:
    You are {{runtime.agent.name}}.
    Summarize for {{audience}}.

  user:
    Summarize the input.
```

`system:` is intentionally not a Toolang block. Use `instruct:` for agent
instructions and `context:` for data. Adapters decide whether assembled
instructions map to OpenAI `developer`, Anthropic `system`, Gemini
`system_instruction`, Mistral `system`, or another provider-specific shape.
`user:` is a user-message template, not an instruction layer.


## Thunk Context Blocks

Each thunk may declare at most one `context:` block. If it omits the block,
Toolang behaves as if the thunk declared `context: default`.

Supported forms are:

| Form | Meaning |
| --- | --- |
| `context: default` | Use the program default context, or the runtime built-in default context if the program has no unnamed context |
| `context: none` | Do not prepend a context prompt for this thunk |
| `context: name` | Use the named program context |
| indented or fenced `context:` block | Use this block text as a thunk-local context prompt |

Example:

```toolang
thunk report(input: Message):
  context: report
  user:
    Write the report.
```


## Thunk Message Blocks

Message blocks are appended after recall-selected history in declaration order.
A thunk may declare any number of `user:`, `assistant:`, and `tool:` blocks.

```toolang
thunk simulate(input: Message):
  recall = none
  user: hello
  assistant: hi
  tool: cached result
```


## Prompts

A prompt is one reusable input expansion.

Recommended shape:

```toolang
prompt review: ```md
---
params: path, focus?
---

Review {{path}} carefully.
{{focus}}
```
```

Prompts are invoked as:

```text
/review src/app.py "only errors"
```

The runtime expands the prompt before normal run-input assembly. The slash-style
`/name ...` invocation remains the user-facing call shape.


## Service Caps

`service` caps use fenced markdown bodies. Service metadata lives in
frontmatter inside that markdown.

`service` frontmatter uses one minimal schema. `description` is the progressive
loading trigger summary; write it as a short natural-language hint for when the
agent should consider the service.

| Required fields | Optional fields |
| --- | --- |
| `description`, `transport`, `target` | `headers`, `env` |

Example HTTP service:

```toolang
service github: ```md
---
description: Trigger this service when the agent needs GitHub MCP access.
transport: http
target: https://mcp.github.com/mcp
headers:
  Authorization: Bearer $GITHUB_TOKEN
---

Use this service when the agent needs GitHub access.
```
```

Example stdio service:

```toolang
service linear: ```md
---
description: Trigger this service when the agent needs Linear MCP access.
transport: stdio
target: npx -y mcp-remote https://mcp.linear.app/sse
env: LINEAR_API_KEY, API_KEY
---

Use this service when the agent needs Linear access.
```
```

`target` is transport-specific. For `http`, it is the MCP endpoint URL. For
`stdio`, it is one argv command line parsed by Toolang and executed without a
shell.

`headers` is a string map for HTTP services. Header values may reference host
env vars with `$NAME`; those references declare the required host variables.
`env` lists required environment variable names for services that need process
environment values, for example `env: API_TOKEN, ANOTHER_ENV_VAR`.


## Surface Rules

Different run surfaces use thunk contracts differently.

| Surface | `input` | Named params |
| --- | --- | --- |
| `script` | optional | optional |
| `chat` | required | forbidden |
| `task` | required | forbidden |
| `chore` | required | forbidden |

This means:

- `script` may call thunks with only params, only `input`, or both
- `chat`, `task`, and `chore` must target thunks that declare `input`
- `chat`, `task`, and `chore` must not target thunks with extra named params
- script CLI usage should be derived from the selected thunk signature

Startup should validate these surface-specific thunk requirements.

### Default Entry Thunks

Recommended default thunk resolution is:

| Surface | Default thunk |
| --- | --- |
| `script` | explicit thunk, else `default` |
| `chat` | `chat`, else `default` |
| `task` | `task`, else `default` |
| `chore` | `chore`, else `default` |
| `file` | `file`, else `default` |


## Script Context

`script` runs with one execution context as well as one thunk input.

Current execution-context terms are:

| Term | Meaning |
| --- | --- |
| `cwd` | Current working directory for the script call |
| `home` | Agent home used by the runtime |
| `root` | Toolang root used by the runtime |

`cwd` belongs to execution context, not to thunk params.

For `script`, the default `cwd` is the caller's current directory.


## Instruction Layers

Toolang uses these contract and payload layers:

| Layer | Role |
| --- | --- |
| `runtime instructions` | Execution protocol and hard constraints |
| `agent instructions` | Built-in defaults or the selected instruct profile. The built-in default consumes selected psyche, skill, and service instruction fragments from the run template context |
| `tool definitions` | Selected tool names, descriptions, and JSON schemas passed separately through the model API |
| `context blocks` | Files, retrieved docs, prior tool results, and other data; untrusted by default |
| `user message` | Current chat objective |
| `task body` | Current task objective |
| `chore body` | Current chore objective |
| `assembled prompt frame` | Provider-neutral runtime payload before adapter mapping |

Recommended precedence:

| Rule | Meaning |
| --- | --- |
| runtime over thunk | Runtime protocol cannot be overridden by one thunk |
| thunk directives over activation defaults | One thunk may narrow or override inherited execution config |
| instruct over built-in default | A program or thunk instruct customizes the default agent profile |
| template controls selected caps | Selected psyches, skills, and services are available in the run template context; the selected instruct or context template decides whether to render them |
| context is data | Context blocks must not be treated as executable instructions |
| objective payload is separate | `user message`, `task body`, and `chore body` define the current objective, not the execution protocol |

This gives these practical roles:

| Surface | Objective payload | Thunk body role |
| --- | --- | --- |
| `chat` | current `user message` | conversation mode |
| `task` | current `task body` | task executor mode |
| `chore` | current `chore body` | chore executor mode |
| `script` | `input` and named params | operation contract |


## Examples

One conversational thunk:

```toolang
thunk chat(input: Message):
  user:
    Help the user in an ongoing conversation.
```

One structured script thunk:

```toolang
struct DeployResult:
  url: Text
  version: Text
  notes: Text[]

thunk deploy(env: Text, version?: Text, dry_run?: Boolean) -> DeployResult:
  models = gpt-5
  tools = shell

  user:
    Deploy the current project.
```

One mixed thunk:

```toolang
thunk diagnose(input: Message, target?: Path, mode?: Text) -> Json:
  skills += review

  user:
    Use the current message and named params together.
```
