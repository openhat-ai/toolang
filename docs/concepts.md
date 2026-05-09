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
| `host/name` | `https://host/name.too` |

Three-part forms such as `owner/repo/name` are not shorthand. Use a
`github://` ref or GitHub URL for explicit repository paths.

GitHub refs must include one revision suffix:

- `github://owner/repo/path/to/agent.too@rev`

Toolang treats `rev` as one git revision token. It does not distinguish branch,
tag, and commit in selector syntax.


## Caps

Caps are reusable agent primitives that shape behavior and available tools.

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`

Caps are definitions. They are not runs.


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

Toolang-owned run ids may also use one dedicated short generated id family. See
[ids.md](./ids.md).


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
