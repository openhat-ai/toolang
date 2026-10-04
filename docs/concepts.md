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

Inspection commands (`info`, `models`, `providers`, `tools`, `caps`,
`psyche list`, `skill list`, `service list`, `prompt list`, `workspace list`,
and `inspect`) use the selected placement. Cap mutations target root scope when
no agent is selected, or the selected resident agent's home. Visiting and roaming
caps are read-only through these commands; clone a shared agent to modify its caps.


## Agent Sandbox

Agent sandboxing describes where and how the agent API process is launched after
an agent target has been materialized. It is separate from source placement.

The public CLI calls these choices sandboxes. Internally, each sandbox plugin
implements the `Sandbox` lifecycle. Current implementations are:

| Driver | Meaning |
| --- | --- |
| `host` | Launch `too _serve` as a local child process |
| `docker` | Launch a container whose primary workload is `too _serve` |

Selectors use `name[:spec]`. Generic orchestration selects the plugin by name
and passes the remaining spec unchanged to that implementation. Future drivers
may use a cloud host. `RunExecutor` receives an `AgentSetup` and an immutable
`AgentState` and does not know where its process is hosted.

`SandboxState` persists only the control-side workload reference required by a
later `stop` command. AgentServer status and execution data remain separate.
The materialized root and home remain authoritative in every environment.

Both `serve` and `start` launch the same AgentServer entrypoint. `serve` waits for
the hosted workload and releases it on exit; `start` returns after readiness.
Scripts and the chat TUI select embedded host execution or a compatible hosted
runtime through the local/remote run-client boundary.


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


## Runnable

A runnable is a program entrypoint, named explicitly or bound by State from an
unnamed declaration. Toolang has two runnable kinds:

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

A ready task or chore has one scheduler checkpoint and one stable derived
thread. Each concrete handling attempt is a run. Chat and direct script calls
create runs directly and have no job or scheduler record. See
[work.md](./work.md).


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
- one run-control input
- one status
- zero or more steps

The runnable name resolves uniquely to an agic or flow in the captured program.
Thread metadata describes the conversation or work context; execution does not
carry a separate run-origin switch.

Toolang-owned run ids may also use one dedicated short generated id family. See
[ids.md](./ids.md).


## Resource Preparation

Runtime resources such as models, tools, and caps are selected through ordered
sets. `SetupWatcher` applies root and agent `allow.models` and `allow.tools`
before publishing an immutable `AgentSetup` revision. Its `models()`,
`providers()`, and `tools()` accessors materialize their data lazily and memoize
it in that setup instance; plugin-family accessors load independently.
`StateWatcher` publishes an `AgentState` containing captured workspace grants
and precomputed effective caps for every module after cap-kind allow policy.
Each completed snapshot is stable; the next root run observes the latest valid pair.

At root-run start, the executor captures those effective collections and then
intersects every session or request ceiling in `RunSpec.ceilings`. The
resulting `AgentResources` is the concrete resource set for that recursive run
tree. Request ceilings can narrow the published base but cannot expand it.

Flow and agic directives compute runnable resources from a selected base:

```text
current_set = base_resources

items += operand  => current_set = current_set union operand
items -= operand  => current_set = current_set minus operand
items = operand   => current_set = current_set intersect operand
```

All operands are evaluated inside the base resources, so `+=` cannot grant
resources outside the agent resources. `-=` and `=` operands may use arbitrary
queries, for example a query that removes all local
models. `=` is a keep-only filter, not a traditional assignment.


## Run Limits

`RunLimits` bound execution quantity and duration rather than resource
selection. Model/tool call limits apply to each agic invocation; token, USD
cost, and time limits apply to the complete recursive root run tree. The
captured `AgentSetup` supplies defaults; session and run policy resolve them
into `RunSpec.limits` without changing the setup snapshot. Defaults resolve
from root `[limit]`, agent `[limit]`, and frozen environment/CLI fields before
the per-session and per-run layers.

`RunBindings` provides optional `model` and `runnable` values. `AgentSetup`
captures `RunDefaults` through `[default]`; policy resolution then
produces the effective bindings stored in `RunSpec`. Surface selections,
session policy, and run policy overlay in that order.


## Step

A step is one execution unit inside one run.

Current step kinds are:

- `exec`
- `run`
- `agent`
- `human`
- `model`
- `tool`
- `par`
- `loop`
- `value`

Steps record execution truth. They do not define transport behavior.


## Local

A local is one value inside a run. The executor's internal `Local` carries a
flow shape (`none | item | list`), type information, and optional provenance.
The durable `execution.types.Local` instead contains a typed value or reference
and a dimension: `dim=0` treats the complete value as one item; `dim=1` iterates
an array as a collection. An array-valued item can therefore have `dim=0`.

`_` is the primary local. Run input initializes it, ordinary bound flow
statements replace it, and run output reads it. Named parameters and `let`
bindings use other local names. `Output` pairs a durable Local with its binding;
`None` leaves the result unbound. See [run-step-records.md](run-step-records.md).


## Content Evaluation And Coercion

Toolang uses six operations at runnable boundaries:

- override parsing produces one aggregate `RunOverride`
- runnable-input parsing produces `CallInput[str]`
- setting and runnable resolution materialize one `RunRequest`, then one
  immutable `RunSpec` containing a resolved `RunnableInput`
- content evaluation produces ordered canonical `Part` values
- input coercion converts those parts to the runnable's declared primary type
- output coercion converts the runnable's final value to its declared output
  type

Content evaluation applies equally to plain text, authored bodies with runtime
values, and multimodal caller input. Runnable-input parsing, input coercion,
and output coercion are language-owned operations. Override parsing and
`RunSpec` resolution are execution-owned. None defines transport
serialization.


## Message

A message is the canonical model and projection unit used across:

- command input projection
- model calls
- projected thread history
- streaming chat responses

Each message has:

- one role
- ordered `parts`

The canonical `Part` union includes:

```text
TextPart | ReasoningPart | ImagePart | AudioPart | DocumentPart
         | ToolCallPart | ToolResultPart
Message = { role: MessageRole, parts: Part[] }
```

User messages accept text, image, audio, and document parts. Assistant messages
also accept reasoning and tool calls. Tool messages require tool results.
The language uses `Part` and typed arrays such as `Part[]`; `Message` is a model
protocol container, not a language value type. Concrete parts, typed arrays and
structs retain their types through runtime values and persistence. See
[message types](../src/toolang/base/types/message.py) and
[language values](../src/toolang/lang/types.py).


## Relationships

Toolang uses these ownership rules:

- one agent owns caps and jobs
- jobs and chat inputs create runs
- runs belong to threads
- runs contain steps
- step output projects to caller-facing messages

This keeps authored state, execution truth, and transport output separate.
