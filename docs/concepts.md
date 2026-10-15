# Overview

This document defines the runtime terms used across Toolang.


## Agent

An agent combines a main Mod with its owned State and runtime.

Each agent has:

- one main Mod
- one cap set
- one job set
- one runtime room


## Agent Sources

| Term | Meaning |
| --- | --- |
| Display Name | Non-unique human-facing label, such as `Brice Yan` or `Gus`; not an identity or path. |
| Agent Name | Local agent identifier, such as `gus` or `daily_report`. |
| Handle | Future public participant identity, such as `by3` or `gus-by3`; recognition and routing are not implemented here. |
| Mod (Module) | A `.too` module, including `agent.too` and runnable modules; its parsed representation is `Program`. |
| Cap | A skill, psyche, service, or prompt. |
| Source | A provider of Mod or Cap content. |
| Source Ref | Content location, such as `gh:owner/name`, `github://owner/repo/path@rev`, or an HTTP(S) URL; Cap kind is supplied separately. |
| Cap Ref | The effective `kind/name` identity, such as `skill/reviewer`, independent of its source. |
| Target | Command input interpreted as an Agent Name, file path, Source Ref, or Cap Ref according to the command. |

Agent Names use `[a-z][a-z0-9_]{0,63}` (1–64 ASCII characters). Explicit names
are validated unchanged. A filename-derived name preserves a valid stem;
otherwise it lowercases the stem, replaces runs outside `[a-z0-9_]` with `_`,
trims surrounding underscores, and prefixes a digit-leading result with
`script_`. `Daily-Report.v2.too` becomes `daily_report_v2`. The source filename
stays unchanged. Empty or overlong results are rejected without truncation or
hash suffixes; `clone SOURCE NAME` can supply a valid name explicitly. Different
source files that normalize to the same agent home cannot share or replace it.
Source-only tools such as `fmt` and `parse` accept arbitrary filenames.

Resident Targets are local Agent Names (`alice` or explicit `agent:alice`).
Local `.too` paths select roaming Mods. Remote sources must be explicit:

| Source Ref | Resolution |
| --- | --- |
| `gh:owner/name`, `github:owner/name` | Probe `agents/name.too`, then `name.too`, in the owner's `agents` repository. |
| `gh:owner/repo/name`, `github:owner/repo/name` | The same path probes in the specified repository. |
| `gh:owner/repo/path.too@rev`, `github:owner/repo/path.too@rev`, `github://owner/repo/path.too@rev` | One exact canonical `github://` Source Ref; no shorthand probing. |
| `https://example.com/agent.too` | Complete HTTP(S) Mod URL. |

`rev` may identify a branch, tag, or commit. Cap repository/path probing is
[kind-specific](caps.md#source-refs). `with skill SOURCE-REF` supplies the kind
separately from the source. Cap records expose `cap: skill/reviewer` and
`source: RESOLVED-SOURCE-REF`; queries default to `cap`.
Remote Mods require a `.too` source path. URL paths are decoded once before
suffix validation; canonical `github://` paths are already literal paths.

Unprefixed remote refs (`owner/name`), `oh:`, and `openhat:` are unsupported and
fail before network access. Use `gh:owner/name` for old implicit GitHub refs and
complete HTTP(S) URLs instead of domain/path shorthand. Names containing dots,
hyphens, uppercase letters, or more than 64 characters must be renamed manually;
state and histories are not moved automatically. Inspect/stop old workloads
with the previous version before changing their names or paths.


## Agent Placement

Agent placement resolves an agent root from its source and the user-selected
Toolang root. Resident roots coincide; visiting roots depend on Toolang root
and source; roaming roots depend on the source directory.

Current placements are:

| Placement | Meaning |
| --- | --- |
| `resident` | A local managed agent already living in an agent home |
| `visiting` | A remote agent materialized into a stable visiting root |
| `roaming` | A local `.too` source materialized into a source-local `.toolang` root |

Placement determines which root, home, source, and config files participate in
runtime assembly. The control boundary passes concrete inputs to `_serve`,
which makes no placement decisions. Placement does not define one run's semantics.

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
| `host` | Run foreground `serve` in-process; detached `start` launches `too _serve` |
| `docker` | Launch a container whose primary workload is `too _serve` |

Selectors use `name[:spec]`. Generic orchestration selects the plugin by name
and passes the remaining spec unchanged to that implementation. Future drivers
may use a cloud host. `RunExecutor` receives an `AgentSetup` and an immutable
`AgentState` and does not know where its process is hosted.

`SandboxState` in `up/records.py` is a view of the resource reference inside
the authoritative agent `status.json`, alongside workload state. It has no
separate state file. `AgentProcess` owns control/query orchestration;
`AgentCore` and server lifecycle hooks own workload execution and status.
`RuntimeRegistration` publishes lightweight references in the Toolang root.

Host `serve` uses one Toolang process; host `start` has a controller and child
during startup, then leaves one workload. Docker uses a host controller and one
guest workload; entry scripts use `exec`. `serve` waits and releases resources
on handled exit; `start` returns after readiness. Host scripts execute directly;
guest scripts use temporary servers, and Chat acquires a persistent server.


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

Caps do not have a separate placement allowlist. A Mod cap declaration or source reference
makes a Cap available to that Mod, but it does not make the cap effective
for every agic by itself.


## Runnable

A runnable is an execution entry in a Mod. Toolang has two runnable kinds:

- `agic`: a dynamic model/tool loop
- `flow`: an ordered set of static statements

Agics and flows share one program namespace and the same parameter and output
signature rules. A flow may start either kind as a child run.


## Chores

A chore is a one-off or repeated work definition. Its driver maintains an
execution checkpoint in `runs.db`; each handling attempt is a run in its stable
`chore_` thread. Chat and scripts create runs directly. See [work.md](work.md).


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

A local is a named runtime value inside a Run. It retains its complete type;
there is no separate item/list shape. An absent local differs from JSON null.
`T[]` is an array and `T[][]` is an array of arrays. Map/reduce/keep/drop/sort
consume only the outermost array; generate creates an array from repeated calls.

`_` is the primary local. Run input initializes it, ordinary Flow statements
replace it, and Run output reads it. Named parameters and `let` bindings use
other local names. The executor retains reference provenance alongside resolved
values. Persisted outputs use `Output(value, binding)` without a Local wrapper.
See [Flow statement terminology](flow-syntax.md#terminology) and
[execution records](run-step-records.md) for the syntax and storage boundaries.


## Content Evaluation And Coercion

Toolang uses six operations at runnable boundaries:

- override parsing produces one aggregate `RunOverride`
- runnable-input parsing produces `CallInput[str]`
- setting and runnable resolution materialize one `RunRequest`, then one
  immutable `RunSpec` containing a resolved `RunnableInput`
- content evaluation produces one ordered canonical `Percept`
- input coercion converts that percept to the runnable's declared primary type
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

Toolang separates authored runnable content from message-only protocol parts:

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

- one agent owns caps and chores
- chores and chat inputs create runs
- runs belong to threads
- runs contain steps
- step output projects to caller-facing messages

This keeps authored state, execution truth, and transport output separate.
