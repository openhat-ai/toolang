# Architecture

Toolang parses authored `.too` Programs, prepares immutable State and installed
Setup, then executes agics and flows as durable Runs inside Threads. This guide
maps the implementation for maintainers; focused guides own the detailed contracts.

## Package ownership

| Package | Owns |
| --- | --- |
| `lang` | CST consumption, semantic AST/lowering, validation, Content, coercion, and formatting |
| `base` | Plugin-facing protocols and shared model/tool/message values |
| `common` | Package-neutral helpers such as IDs, immutable values, files, and layout vocabulary |
| `catalog` | Authored agents, cap/chore formats, configured refs, and source acquisition |
| `setup` | Installed plugins, environment, model routes, policy defaults, and immutable Setup views |
| `state` | Parsed source layers, immutable revisions, effective modules/caps/workspaces, and publication |
| `execution` | Admission, Run/Step lifecycle, model/tool execution, controls, records, history, and observation |
| `driver` | Talk consumption and Chore activation/scheduling over the executor |
| `teaming` | Hub protocol, membership, conversations/rules, presence, and event transport |
| `stats` | Derived usage cache, archival, and time/scope queries |
| `up` | AgentCore/AgentServer composition, sandbox processes, lifecycle, discovery, and Top observation |
| `api` | HTTP validation, request conversion, dependencies, and SSE transport |
| `cli` | Target/command routing, local defaults and paths, terminal clients, and presentation |
| `plugin` | Reusable toolsets, model catalogs/adapters, and sandbox implementations |

Source parsing stays with its format owner. Runtime execution consumes concrete
values and snapshots; CLI and hosting choose placement, paths, environment, and
process lifecycle. Plugin entry points and factories are documented in
[Plugins](plugins.md).

## Agent

An agent has a source identity and concrete layout. Its Programs, caps,
[workspace grants](workspaces.md), and chore definitions form [State](state.md). Installed tools, models,
environment, and execution defaults form [Setup](setup.md). [Drivers](drivers.md) accept
interactive, conversation, and scheduled triggers; their work uses the same
executor. Source placement and sandbox hosting are independent choices.

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

`SandboxState` in `up/instance.py` is a view of the resource reference inside
the authoritative agent `status.json`, alongside workload state. It has no
separate state file. `AgentProcess` owns control/query orchestration;
`AgentCore` and server lifecycle hooks own workload execution and status.
`RuntimeRegistration` publishes lightweight references in the Toolang root.

Host `serve` uses one Toolang process; host `start` has a controller and child
during startup, then leaves one workload. Docker uses a host controller and one
guest workload; entry scripts use `exec`. `serve` waits and releases resources
on handled exit; `start` returns after readiness. Host scripts execute directly;
guest scripts use temporary servers. Resident Chat retains its shared server;
visiting Chat uses a shared automatic server that waits for clients and accepted
work to finish before exiting. Explicit start retains either placement. See
[server lifetime](hosting.md#server-lifetime-and-client-leases) for lifecycle details.


## Execution vocabulary

| Concept | Meaning | Detailed owner |
| --- | --- | --- |
| Cap | Composable agent primitive: psyche, skill, service, or prompt | [Caps](caps.md) |
| Runnable | Agic model/tool loop or Flow statement evaluator with an input/output contract | [Program](program.md), [Agic](agic.md), [Flow](flow.md) |
| Chore | Named one-off or scheduled definition with driver-owned progress | [Chores](chores.md), [Drivers](drivers.md) |
| Thread | Durable context and logical history over physical Run records | [Execution](execution.md#threads) |
| Run | One accepted invocation; exec can replace its runnable without changing its ID | [Execution](execution.md) |
| Step | A model/tool call, flow operation, or other execution unit within a Run | [Executor](executor.md) |
| Local | A typed in-memory value with provenance under `_`, an argument, or a flow binding | [Call Input](call-input.md), [Flow](flow.md) |
| Record | Persisted execution fact with strict typed references | [Records](records.md) |
| Event | Value-only notification for subscribers after committed execution changes | [Events](events.md) |

`_` is the primary local. Value statements normally replace it; `let NAME = ...`
binds another name and nameless `let` discards a value statement's result without
changing `_`. Arrays preserve their complete type and nesting. Map/reduce/keep/drop/
sort consume only outer items; there is no separate item/list dimension.

## Preparation and resource authority

`SetupWatcher` captures configuration and builds independently lazy, memoized
model, provider, tool, and plugin views. `StateWatcher` publishes parsed root/home
layers and their composition. Publication can isolate invalid source units while
keeping valid siblings; [State](state.md) owns acceptance and diagnostics.

Root admission captures Setup, State, effective resource ceilings, limits,
workspace grants, and the selected runnable contract. Setup and workspace grants
remain bound to that root tree. Accepted code and types stay fixed. New named
calls select the latest valid State and check compatibility; inline bodies remain
part of their accepted plan. Before model calls, resource selectors apply to
current State within module scope. See [State capture](execution.md#state-capture).

Resource directives operate inside the inherited base: `=` intersects, `+=`
restores allowed matches, and `-=` removes matches. No directive expands ancestor
or external authority. Model/tool call limits apply per agic; token, cost, and time
limits are shared by the recursive root tree. [Program](program.md#directives)
and [Execution](execution.md) own their exact rules.

## Input and messages

Text surfaces parse a `RunOverride` and flat `CallInput[str]`. Resolution evaluates
Content and coerces it to the runnable signature; direct-value calls bind native
values without interpreting strings as Content again. The resulting `RunSpec`
contains typed inputs. CLI/HTTP/session state stays outside the execution core.

Canonical model messages are `{role, parts}` from
[message.py](../../src/toolang/base/types/message.py). `Part` covers text, reasoning,
image, audio, document, tool-call, and tool-result values. Roles constrain the
allowed variants: user input excludes reasoning and tool protocol parts;
assistant output can include reasoning and tool calls; tool messages contain
results. Message/provenance projections do not introduce a second durable
transcript or event journal. See [Chat](chat.md), [Agic](agic.md), and
[Records](records.md).

## Observation and derived views

Execution writes records directly and publishes subscriber notifications only
after commit. In-memory LiveTree samples, cursor-based event streams, historical
record projections, and usage statistics serve different reads. Reconnection can
replace structural state from records, but cannot replay every historical Part
Delta. None of these views determines whether accepted work executes.

[Events](events.md) owns delivery and recovery, [Executor](executor.md#resident-livetree)
owns resident live state, [Execution statistics](execution-statistics.md) owns the
derived cache, and [Top](top.md) combines current samples and recent durable facts.
