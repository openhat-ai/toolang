# CLI command recognition and agent parameters

The grammar, native leaf declarations, instance selectors, place layout, and bound
execution are implemented. Expanded `top`/`chore` scope and home-based `cd` remain
separate follow-up surfaces.
[CLI](cli.md) describes current behavior;
[agent addresses](agent-addresses.md) defines home lookup and runtime ownership.

## Grammar

Use one command tree with leaf-owned agent positions:

```text
too [OPTIONS] AGENT COMMAND [ARGUMENTS]
too [OPTIONS] COMMAND AGENT [ARGUMENTS]
too [OPTIONS] COMMAND [ARGUMENTS]
```

`info`, `chat`, `cd`, `start`, `stop`, and `serve` accept both explicit agent
positions. Other agent-scoped commands require the prefix, including inspection,
Run/Thread controls, and resource groups. A command-first agent immediately
follows the command, before any business operands. Commands with optional agent
scope retain omission: `too top`, `too models`, or `too skill list` use root scope.
Catalog commands such as `too new NAME`, `too clone SOURCE [NAME]`, and
`too remove NAME` have ordinary Typer operands and no agent-prefix slot.
`talk` takes a conversation target and has no agent-prefix form.
Global options precede the agent or command.

After native Typer parsing of global options, inspect only the command header:

1. A registered first token selects that command, including hidden commands.
2. Otherwise, a registered second token selects that command and binds the first
   token as its raw agent prefix. A command without an agent slot rejects it.
3. Otherwise, a plain local `.too` path selects implicit Script entry. File
   existence is not a routing condition; explicit source schemes take precedence.
4. Otherwise, report a command error. Explicit selector syntax identifies the
   second token as the missing/unknown command; unknown bare words use native
   root-command diagnostics. Do not read homes to guess the intended command.

A registered command wins a name collision; `agent:NAME` selects the agent.
Selection is final: never retry another interpretation after a parameter error,
search later words for commands, or extract options from message/input text.
Forward the selected command's tail unchanged.

## One native agent parameter

Extend the existing `AgentContract`, `AgentArgument`, `AgentParameter`, and
`CliCommand`/`CliGroup` factories. One immutable leaf contract declares:

| Property | Meaning |
| --- | --- |
| Role | `source`, `home`, or `execute`; determines lookup. |
| Forms | Accepted name, path, ref, URL, and explicit instance forms. |
| Placements | Accepted resident, visiting, and roaming targets. |
| Both orders | Whether AGENT may also follow the command; defaults to prefix-only. |
| Requiredness | Whether omitted selection uses root scope; represented by the native parameter default/required value. |
| Runtime requirement | Whether the action requires a live owner and a particular driver. |

All agent-prefix parameters use `AGENT`; descriptions list accepted input forms
and indicate live targets with `Running`. Generate parameter types, concise
descriptions, completion eligibility, and lazy routing metadata from the same
declaration. Command callbacks declare their native argument through
`AgentArgument`; declaration checks reject inconsistent roles,
forms, defaults, and multiple agent parameters. Ordinary catalog operands may
reuse lexical name/source types without accepting a prefix.

A group carries the raw prefix into its selected subtree. It has no agent
parameter or agent-resolving callback. Each agent-scoped leaf owns its native
agent parameter and contract; group metadata summarizes its children. A leaf
without an agent parameter rejects an inherited prefix before help or execution.
Thus `too a skill list` accepts source inspection while `too a skill edit x` accepts
resident authoring. Parent callbacks cannot prematurely restrict either leaf.

`AgentParameter` sources an inherited prefix from invocation-local Context, or
registers its native positional input when the leaf permits both orders and no
prefix is present. Both supplied forms are `COMMANDLINE` input.
Native Typer owns defaults, requiredness, conversion,
callbacks, remaining parameters, eager help, errors, and completion. Agent-aware
leaves disable `no_args_is_help`: native requiredness diagnoses missing inputs,
and a bound prefix can satisfy an otherwise empty command tail. Groups may show
help when no child command is selected. Do not insert placeholder argv tokens.

Conversion parses selector syntax and validates forms/placements without source
fetching, active lookup, home creation, or runtime acquisition. After all native
parameters validate, invocation binding resolves the source/home and owning
records once. Actions receive concrete bindings; required runtime/driver checks
happen before their effects. No second callback dispatch, process-global target,
parameter mutation, or callback-default repair is permitted. Cached command trees
remain safe across invocations.

`up.source` owns selector parsing; `up.identity` verifies retained ownership.
CLI context holds invocation data, while CLI execution resolves record-bound
homes and opens their stores. Callbacks pass Run/Thread selectors explicitly;
shared helpers never infer them from native parameter names.

Lazy command metadata references the same contract used to construct the native
command. Root help and recognition do not load unrelated implementations or parse
local scripts. Prefix usage renders `AGENT` before the top-level command/group;
dual-order leaves show a second Usage line with AGENT after the command.
The selected leaf determines whether it is required, for example:

```text
too AGENT info [OPTIONS]
too info [OPTIONS] AGENT
too AGENT workspace add [OPTIONS] PATH
too AGENT chore run [OPTIONS] CHORE
```

## Command contracts and descriptions

The parameter description is a short sentence-case phrase. Use `instance` for the
user-facing home selection; keep placement names, internal address terminology,
and driver requirements in validation rather than help. Root scope never means an
implicit `default` agent. Record binding follows the address design.

| Commands | Agent parameter description | Effect |
| --- | --- | --- |
| `new` | `New agent name` | Create an unused resident name; ordinary command operand. |
| `remove` | `Agent name` | Remove a resident agent; ordinary command operand. |
| `clone` | `Agent name, ref or URL` | Clone authored data into a new resident name; ordinary source/target operands. |
| `workspace list/add/remove` | `Agent name or path` | Inspect/edit resident or project configuration; reject visiting and decorated operands. |
| `{psyche,skill,service,prompt} new/edit/delete/add/remove`, `chore new/edit/delete` | `Agent name` | Edit resident authored data; omitted cap selection uses root scope. |
| `chat` | `Agent name, path, ref, URL or instance` | Source-only roaming execution allocates a home; instance/Thread execution stays bound. |
| `start`, `serve` | `Agent name, ref, URL or instance` | Start resident/visiting sources or their existing homes; reject roaming. |
| `stop` | `Agent name, ref, URL or instance` | Stop the resident/visiting runtime; reject roaming instances. |
| `info`, `inspect` | `Agent name, path, ref, URL or instance` | Read existing storage without source preparation. |
| `top` | `Agent name` | Observe active runtimes; omission uses root scope. |
| `cd` | `Agent name, path, ref or URL` | Open the source environment; omission uses root scope. |
| `steer`, `cancel` | `Running agent name, path, ref, URL or instance` | Control an active Run; keep runtime lifetime with its owner. |
| `rerun`, `fork`, `rewind` | `Agent name, path, ref, URL or instance` | Continue/edit records in that home; `--chat` preserves the binding. |
| `models`, `providers`, `tools`, `caps`, cap-kind `list` | `Agent name, path, ref or URL` | Inspect configured sources; reject decorated addresses. Omission uses root configuration. |
| `chore list` | `Agent name` | Read resident definitions and scheduling state. |
| `chore run/cancel` | `Running agent name` | Control a live resident chore driver without enabling a missing driver. |

Expanded `top`/`chore` scope and home-based `cd` remain follow-ups. Instance
selection is part of `AGENT`; no instance flag remains.

In resource commands, `name` refers to configuration even when its resident agent
is running. Source queries may prepare inputs/caches but never allocate a retained
roaming place. `info` supplies live resource summaries or retained offline data;
missing offline fields are unavailable, without rebuilding from current sources.

`run FILE` uses `Path to a .too file`. `init`, `fmt`, `parse`, and `highlight`
retain directory/file/stdin operands. `list`, plugin inventories, `hub`, `talk`,
and `convo` have no agent selector; conversation shorthands belong to hub messaging.

## Script entry, help, and completion

Register `run` once. `too FILE RUNNABLE` and `too run FILE RUNNABLE` use the same
Script resolver, parsed Program, dynamic signature, and native invocation path.
A registered second token wins: `too work.too chat` selects built-in Chat;
`too run work.too chat` or a typed runnable selects the script's `chat`.
A registered command without an agent slot rejects a prefix, even when the script
has a runnable with that name. [Scripts](scripts.md#runnable-input-and-help) owns
assignment, Content, stdin, literal boundaries, coercion, and typed selectors.

Static help never resolves/fetches a home. File help may read/parse its Program;
runnable help builds only the selected signature. Neither prepares State, reads
stdin, allocates execution records, or starts a runtime. Agent-command help with a
local path does not parse that file. Preserve native validation and operational
exit codes; explicit help succeeds, malformed command parameters fail normally.

Register Typer's shell completion backends when building the root command.
Completion reads the same contracts. It may enumerate scoped resident/active
entries or an explicitly supplied source path's retained places, but never fetches
sources, probes runtimes, or searches for offline roaming homes by short name.
Root command completion stays registry-based; lexical selector suggestions use
shared metadata. Tail completion excludes a bound prefix or a prefix-only agent
parameter; dual-order leaves otherwise complete the native agent operand.
Help/completion never invoke command actions.

Remove public `--instance`/`-i`. Private bootstrap receives concrete root/home,
placement, identity, and runtime-generation bindings. There are no old-syntax
aliases, dual format readers, or migration paths. Only the six dual-order leaves
accept an agent after the command.
