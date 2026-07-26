# Overview

This document defines the runtime terms used across Toolang.


## Agent

An agent is one local runnable program and its owned state.

Each agent has:

- one source program
- one cap set
- one job set
- one runtime room


## Agent Sources

Toolang accepts these agent source terms:

| Term | Meaning |
| --- | --- |
| `name` | A local managed agent name |
| `shorthand` | A short selector that expands by convention |
| `ref` | A canonical remote agent program reference |
| `selector` | Any accepted input form: `name`, `shorthand`, or `ref` |

Examples:

| Input | Form |
| --- | --- |
| `alice` | `name` |
| `brice/alice` | `shorthand` |
| `toolang.ai/alice` | `shorthand` |
| `github://brice/agents/alice.too@main` | `ref` |
| `https://toolang.ai/alice.too` | `ref` |

Current shorthand expansion rules are:

| Shorthand | Expanded refs |
| --- | --- |
| `owner/name` | probes `github://owner/agents/agents/name.too@<default-branch>`, then `github://owner/agents/name.too@<default-branch>` |
| `owner/repo/name` | probes `github://owner/repo/agents/name.too@<default-branch>`, then `github://owner/repo/name.too@<default-branch>` |
| `host/name` | `https://host/name.too` |

Three-part shorthand specifies the repository exactly. It does not probe other
repository names.

GitHub refs must include one revision suffix:

- `github://owner/repo/path/to/agent.too@rev`

Toolang treats `rev` as one git revision token. It does not distinguish branch,
tag, and commit in selector syntax.


## Agent Placement

Agent placement describes how an agent source is materialized into one local
runnable Toolang root before execution.

Current placements are:

| Placement | Meaning |
| --- | --- |
| `resident` | A local managed agent already living in an agent home |
| `visiting` | A remote agent materialized into a stable visiting root |
| `roaming` | A local `.too` source materialized into a source-local `.toolang` root |

Placement determines which root, home, source, and config files participate in
runtime assembly. It does not define the semantic shape of one run.


## Agent Hosting

Agent hosting describes where and how the agent API process is launched after
an agent target has been materialized. It is separate from source placement.

The public CLI calls these choices sandboxes. Internally, each sandbox plugin
implements the `Hosting` lifecycle. Current implementations are:

| Driver | Meaning |
| --- | --- |
| `none` | Launch `too serve` as a local child process |
| `docker` | Launch a container whose primary workload is `too serve` |

Selectors use `name[:spec]`. Generic orchestration selects the plugin by name
and passes the remaining spec unchanged to that implementation. Future drivers
may use a cloud host. `RunExecutor` receives an `AgentSetup` and an immutable
`AgentState` and does not know where its process is hosted.

`HostingState` persists only the control-side workload reference required by a
later `stop` command. AgentServer status and execution data remain separate.
The materialized root and home remain authoritative in every environment.

Both `run` and `start` launch the same AgentServer entrypoint. `run` waits for
the hosted workload and releases it on exit; `start` returns after readiness.
One-shot scripts and the chat TUI continue to use the execution core directly.


## Caps

Caps are reusable agent primitives that shape behavior and available tools.

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`

Caps are definitions. They are not runs.

Caps are assembled from the materialized root and source:

| Placement | Cap sources |
| --- | --- |
| `resident` | Local agent root caps, inline caps, and referenced caps |
| `visiting` | Inline caps and referenced caps from the materialized remote source |
| `roaming` | Source-local `toolang.toml`, inline caps, and referenced caps from the local `.too` source |

Caps do not have a separate placement allowlist. A program cap or reference
makes a cap available to that program, but it does not make the cap effective
for every agic by itself.


## Executable

An executable is a named program entrypoint. Toolang has two executable kinds:

- `agic`: a dynamic model/tool loop
- `flow`: an ordered set of static statements

Agics and flows share one program namespace and the same parameter and output
signature rules. A flow may start either kind as a child run.


## Jobs

Jobs are durable authored work definitions.

Current built-in job kinds are:

- `task`
- `chore`

Jobs are definitions. They are not runs.


## Thread

A thread is a durable execution context.

A thread groups related runs under one stable topic or work item.

Toolang-owned local threads may use one short generated id family. External
thread ids remain opaque. See [ids.md](./ids.md).


## Run

A run is one concrete handling attempt inside one thread.

A run has:

- one globally unique run id
- one thread
- one runnable name
- one start-control input
- one status
- zero or more steps

The runnable name resolves uniquely to an agic or flow in the captured program.
Thread metadata describes the conversation or work context; execution does not
carry a separate run-origin switch.

Toolang-owned run ids may also use one dedicated short generated id family. See
[ids.md](./ids.md).


## Activation Sets

Runtime resources such as tools, models, and program-scoped caps are selected
through ordered sets.

Placement provides a default resource set. CLI options may override or adjust
that default for one activation. The result is the activation set.

Programs may also define inline caps or reference resources. Those items form
a program set. Program resources are not automatically effective for every
agic.

Agic directives compute the effective set from the activation set:

```text
current_set = activation_set

items += operand  => current_set = current_set union operand
items -= operand  => current_set = current_set minus operand
items = operand   => current_set = current_set intersect operand
```

`+=` operands must name resources from the program set. `-=` and `=` operands
may be arbitrary selectors, for example a selector that removes all local
models. `=` is a keep-only filter, not a traditional assignment.


## Step

A step is one execution unit inside one run.

Current step kinds are:

- `run`
- `agent`
- `human`
- `model`
- `tool`
- `par`
- `loop`
- `system`

Steps record execution truth. They do not define transport behavior.


## Local

A local is one runtime value inside a run. It contains a value and one shape:

```text
none | item | list
```

`_` is the primary local. Run input initializes it, ordinary flow statements
replace it, and run output reads it. Named parameters and `let` bindings use
other local names. Durable input and output refs are persistence metadata, not
part of a local.


## Perceiving And Coercion

Toolang uses three operations at runnable boundaries:

- input perceiving interprets supported input as one ordered `Percept`
- input coercion converts that percept to the runnable's declared primary type
- output coercion converts the runnable's final value to its declared output
  type

Input perceiving applies equally to plain text, authored bodies with runtime
values, and multimodal caller input. Input and output coercion are
language-owned type operations; they do not define transport serialization.


## Message

A message is the canonical model and projection unit used across:

- command input projection
- model calls
- projected thread history
- streaming chat responses

Each message has:

- one role
- ordered `parts`

Toolang separates authored and executable content from message-only protocol
parts:

```text
PerceptPart = TextPart | ImagePart | AudioPart | DocumentPart
Percept     = PerceptPart[]
MessagePart = PerceptPart | ToolCallPart | ToolResultPart
Message     = { role: MessageRole, parts: MessagePart[] }
```

At the language boundary, `Part` maps to one `PerceptPart` and `Part[]` maps to
one `Percept`. The `lang` AST keeps those concise source type names; other
packages use `PerceptPart` and `Percept`. Messages use ordered `MessagePart`
values so model and tool interaction can add tool calls and results. `Message`
and `MessagePart` are not Toolang language value types.

User messages contain only `PerceptPart` values. Assistant messages may
additionally contain `ToolCallPart` values, and tool messages contain only
`ToolResultPart` values.


## Relationships

Toolang uses these ownership rules:

- one agent owns caps and jobs
- jobs and chat inputs create runs
- runs belong to threads
- runs contain steps
- step output projects to caller-facing messages

This keeps authored state, execution truth, and transport output separate.
