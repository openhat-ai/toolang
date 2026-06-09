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
makes a cap available to that program, but it does not make the cap
effective for every thunk by itself.


## Jobs

Jobs are durable authored work definitions.

Current built-in job kinds are:

- `task`
- `chore`

The jobs API also exposes one `will` endpoint for a long-horizon definition.
When no will is configured, that endpoint returns `null`.

Jobs are definitions. They are not runs.


## Thread

A thread is a durable execution context.

A thread groups related runs under one stable topic or work item.

Toolang-owned local threads may use one short generated id family. External
thread ids remain opaque. See [ids.md](./ids.md).


## Run

A run is one concrete handling attempt inside one thread.

A run has:

- one origin
- one input message
- one status
- zero or more steps

The run origin names the semantic source of the run. Current origins are:

| Origin | Meaning |
| --- | --- |
| `chat` | A conversational user message |
| `task` | A local task execution |
| `chore` | A scheduled chore execution |
| `script` | A direct CLI thunk execution |

Placement and origin are independent. Placement decides how the agent is
assembled; origin decides how instructions, messages, and execution context are
assembled for one run.

Toolang-owned run ids may also use one dedicated short generated id family. See
[ids.md](./ids.md).


## Activation Sets

Runtime resources such as tools, models, and program-scoped caps are selected
through ordered sets.

Placement provides a default resource set. CLI options may override or adjust
that default for one activation. The result is the activation set.

Programs may also define inline caps or reference resources. Those items form
a program set. Program resources are not automatically effective for every thunk.

Thunk directives compute the effective set from the activation set:

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

- `model_call`
- `tool_call`
- `runtime`

Steps record execution truth. They do not define transport behavior.


## Message

A message is the canonical content unit used across:

- run input
- model calls
- projected thread history
- streaming chat responses

Each message has:

- one role
- ordered `parts`
- optional `meta`

Current core part kinds are:

- `text`
- `tool_call`
- `tool_result`


## Relationships

Toolang uses these ownership rules:

- one agent owns caps and jobs
- jobs and chat inputs create runs
- runs belong to threads
- runs contain steps
- step output projects to caller-facing messages

This keeps authored state, execution truth, and transport output separate.
