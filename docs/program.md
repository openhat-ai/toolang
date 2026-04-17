# Program Syntax

This document defines the source-level program model for `.too` files.


## Core Constructs

Current program-level constructs are:

| Construct | Meaning |
| --- | --- |
| `agent` | Program header declaring the agent name |
| `use` | One external cap reference |
| `struct` | One named structured type |
| `prompt` | One reusable prompt definition |
| `thunk` | One callable program entrypoint |


## Thunk

A thunk is one callable entrypoint.

Each thunk has three parts:

| Part | Meaning |
| --- | --- |
| `signature` | Name, params, and return contract |
| `directives` | Model and capability selection for that thunk |
| `body` | Thunk-specific instructions |

Recommended shape:

```toolang
thunk [name](params...) [-> ReturnType]
  directives...
  ...
```


## Signature

### Name

Thunk names follow these rules:

| Rule | Meaning |
| --- | --- |
| explicit name | One stable callable name |
| omitted name | Canonical name is `main` |
| `main` | Reserved default entrypoint name |

Examples:

```toolang
thunk main(input)
  Help the user directly.
```

```toolang
thunk(input)
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
thunk chat(input)
  Reply to the current user message.
```

```toolang
thunk diagnose(input, target?, mode?)
  Use the message together with named params.
```

### Named Params

All non-`input` params are structured named arguments.

Parameter rules:

| Rule | Meaning |
| --- | --- |
| no type annotation | default type is `string` |
| `?` suffix | optional param |
| explicit type | overrides the default `string` type |

Examples:

```toolang
thunk deploy(env, version?)
  Deploy the current project.
```

```toolang
thunk deploy(env, dry_run?: boolean, retries?: number)
  Deploy the current project.
```


### Param Types

Built-in parameter types are:

| Type | Meaning |
| --- | --- |
| `string` | One string value |
| `number` | One JSON number |
| `boolean` | One boolean value |
| `json` | One arbitrary JSON-compatible value |
| `path` | One filesystem path |
| `artifact` | One saved artifact descriptor |
| `T[]` | One array of `T` |
| `StructName` | One structured value validated against a declared `struct` |

`input` is not typed with these forms. It always means one canonical
`Message`.


### Returns

The default thunk return type is `message`.

That means:

```toolang
thunk chat(input)
  Reply directly.
```

is equivalent to:

```toolang
thunk chat(input) -> message
  Reply directly.
```

Recommended return contracts are:

| Return Type | Meaning |
| --- | --- |
| `message` | One full canonical message |
| `text` | One text projection |
| `json` | One arbitrary JSON value |
| `artifact` | One saved artifact descriptor |
| `StructName` | One structured output value |


## Struct

`struct` defines one named structured type.

Example:

```toolang
struct ReviewFinding
  path: string
  line: number?
  severity: string
  message: string

struct ReviewResult
  summary: string
  findings: ReviewFinding[]
  patch: artifact?
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
| `name: Type?` | Optional field |


## Thunk Directives

Thunk directives configure one thunk without changing its input or output
contract.

Recommended directives are:

| Directive | Meaning |
| --- | --- |
| `model = ...` | Declare one ordered model preference list |
| `psyches = ...` | Select one effective psyche set |
| `skills = ...` | Select one effective skill set |
| `services = ...` | Select one effective service set |
| `tools = ...` | Select one effective tool set |

Directive values use names, not refs.

Example:

```toolang
thunk review(input, path?: path) -> ReviewResult
  model = gpt-5
  psyches = rigorous
  skills = review, patch
  services = github
  tools = shell, service_use

  Review the target and return actionable findings.
```

### Scalar and Set Directives

`model` is one ordered CSV list and uses only `=`. Capability directives are set-valued.

| Form | Meaning |
| --- | --- |
| `model = gpt-5, o3` | Declare one ordered model preference list |
| `skills = review, patch` | Use one exact capability set |
| `skills += review, patch` | Add items to the inherited set |
| `skills -= review, patch` | Remove items from the inherited set |

Rules:

| Rule | Meaning |
| --- | --- |
| no directive | inherit activation defaults |
| `=` | replace with the declared ordered list |
| ordered CSV | preserve declaration order |
| no `default` keyword | `default` is treated like any other selector text |
| `+=` | add to the inherited set for capability directives |
| `-=` | remove from the inherited set for capability directives |
| names only | directives do not resolve shorthand or refs |


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


## Service Declarations

`service` declarations use fenced markdown bodies. Service metadata lives in
frontmatter inside that markdown.

`service` frontmatter uses one closed schema per transport.

| Transport | Required fields | Optional fields |
| --- | --- | --- |
| `http` | `transport`, `url` | `headers` |
| `stdio` | `transport`, `command` | `args`, `env`, `cwd` |

Example HTTP service:

```toolang
service github: ```md
---
transport: http
url: https://mcp.github.com/mcp
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
transport: stdio
command: npx
args:
  - -y
  - mcp-remote
  - https://mcp.linear.app/sse
env: LINEAR_API_KEY, API_KEY=NOT_THE_SAME_NAME
cwd: /work/tools
---

Use this service when the agent needs Linear access.
```
```

`env` uses one compact mapping form:

| Form | Meaning |
| --- | --- |
| `FOO` | Forward `FOO` from the host environment |
| `BAR=FOO` | Set child env `BAR` from host env `FOO` |

`headers` is one string map. Header values may reference host env vars with
`$NAME`.


## Surface Rules

Different run surfaces use thunk contracts differently.

| Surface | `input` | Named params |
| --- | --- | --- |
| `invoke` | optional | optional |
| `chat` | required | forbidden |
| `task` | required | forbidden |
| `chore` | required | forbidden |

This means:

- `invoke` may call thunks with only params, only `input`, or both
- `chat`, `task`, and `chore` must target thunks that declare `input`
- `chat`, `task`, and `chore` must not target thunks with extra named params
- `invoke` CLI usage should be derived from the selected thunk signature

Startup should validate these surface-specific thunk requirements.

### Default Entry Thunks

Recommended default thunk resolution is:

| Surface | Default thunk |
| --- | --- |
| `invoke` | explicit thunk, else `main` |
| `chat` | `chat`, else `main` |
| `task` | `task`, else `main` |
| `chore` | `chore`, else `main` |


## Invoke Context

`invoke` runs with one execution context as well as one thunk input.

Current execution-context terms are:

| Term | Meaning |
| --- | --- |
| `cwd` | Current working directory for the invoke call |
| `home` | Agent home used by the runtime |
| `root` | Toolang root used by the runtime |

`cwd` belongs to execution context, not to thunk params.

For `invoke`, the default `cwd` is the caller's current directory.


## Instruction Layers

Toolang uses these contract and payload layers:

| Layer | Role |
| --- | --- |
| `runtime instructions` | Execution protocol and hard constraints |
| `psyche text` | Long-lived default behavior |
| `thunk body` | Entrypoint-specific execution behavior |
| `user message` | Current chat objective |
| `task body` | Current task objective |
| `chore body` | Current chore objective |
| `assembled instructions` | Final model-facing instruction text |

Recommended precedence:

| Rule | Meaning |
| --- | --- |
| runtime over thunk | Runtime protocol cannot be overridden by one thunk |
| thunk directives over activation defaults | One thunk may narrow or override inherited execution config |
| thunk over psyche | One thunk may be more specific than one psyche |
| objective payload is separate | `user message`, `task body`, and `chore body` define the current objective, not the execution protocol |

This gives these practical roles:

| Surface | Objective payload | Thunk body role |
| --- | --- | --- |
| `chat` | current `user message` | conversation mode |
| `task` | current `task body` | task executor mode |
| `chore` | current `chore body` | chore executor mode |
| `invoke` | `input` and named params | operation contract |


## Examples

One conversational thunk:

```toolang
thunk chat(input)
  Help the user in an ongoing conversation.
```

One structured invoke thunk:

```toolang
struct DeployResult
  url: string
  version: string
  notes: string[]

thunk deploy(env, version?, dry_run?: boolean) -> DeployResult
  model = gpt-5
  tools = shell

  Deploy the current project.
```

One mixed thunk:

```toolang
thunk diagnose(input, target?: path, mode?) -> json
  skills += review

  Use the current message and named params together.
```
