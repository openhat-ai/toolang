# CLI command recognition and agent parameters

Target contract for the next CLI revision. Implementation follows approval of
this definition. [CLI](cli.md) describes current behavior;
[agent addresses](agent-addresses.md) defines home lookup and runtime ownership.

## Grammar

Use one command tree and one agent-prefix form:

```text
too [OPTIONS] AGENT COMMAND [ARGUMENTS]
too [OPTIONS] COMMAND [ARGUMENTS]
```

Agent-scoped commands use the first form. Commands with optional agent scope also
allow the second: `too top`, `too models`, or `too skill list` use root scope.
Catalog commands such as `too new NAME`, `too clone SOURCE [TARGET]`, and
`too remove NAME` have ordinary Typer operands and no agent-prefix slot.
There is no command-suffix agent form: use `too alice info`, not `too info alice`.
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
| Role | `source`, `home`, or `execute`; determines lookup and metavar. |
| Forms | Accepted `name`, `path`, `ref`, `url`, and explicit `address` forms. |
| Placements | Accepted resident, visiting, and roaming targets. |
| Requiredness | Whether omitted selection uses root scope; represented by the native parameter default/required value. |
| Runtime requirement | Whether the action requires a live owner and a particular driver. |

Source/execute parameters use `AGENT`; home parameters use `ADDRESS`. Generate
parameter types, concise descriptions, completion eligibility, and lazy routing
metadata from the same declaration. Command callbacks declare the native agent
argument through `AgentArgument`; declaration checks reject inconsistent roles,
forms, defaults, and multiple agent parameters. Ordinary catalog operands may
reuse lexical name/source types without accepting a prefix.

A group carries the raw prefix into its selected subtree. It has no agent
parameter or agent-resolving callback. Every executable leaf has its own native
agent parameter and contract; group metadata summarizes its children. Thus
`too a skill list` accepts source inspection while `too a skill edit x` accepts
resident authoring. Parent callbacks cannot prematurely restrict either leaf.

`AgentParameter` sources the inherited prefix from invocation-local Context and
marks it `COMMANDLINE` input. Native Typer owns defaults, requiredness, conversion,
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

Lazy command metadata references the same contract used to construct the native
command. Root help and recognition do not load unrelated implementations or parse
local scripts. Prefix usage renders before the top-level command/group using the
selected leaf's role, for example:

```text
too ADDRESS info [OPTIONS]
too AGENT workspace add [OPTIONS] PATH
too ADDRESS chore run [OPTIONS] CHORE
```

## Command contracts and descriptions

The parameter description is a short phrase. Scope/state qualifiers become normal
wording rather than parenthesized tags. Root scope never means an implicit
`default` agent. Record binding and address aliases follow the address design.

| Commands | Agent parameter description | Effect |
| --- | --- | --- |
| `new`, `remove` | `resident agent name` | Create an unused resident name or remove that resident agent; ordinary command operands. |
| `clone` | `agent name, ref or url` | Clone authored data into a new resident name; ordinary source/target operands. |
| `workspace add/remove` | `local agent name or path` | Edit resident/project configuration; no visiting or decorated operands. |
| `{psyche,skill,service,prompt} new/edit/delete/add/remove`, `chore new/edit/delete` | `resident agent name` | Edit resident authored data; omitted cap selection uses root scope. |
| `chat` | `agent name, path, ref, url or address` | New local-source execution allocates a home; address/Thread execution stays bound. |
| `start`, `serve` | `resident or visiting agent by name, ref, url or address` | Start the source or existing home; roaming hosting is unsupported. |
| `stop` | `resident or visiting agent address` | Stop the hosted runtime. |
| `info`, `inspect`, `top`, `cd` | `agent address` | Read/observe/open existing storage; omitted `top`/`cd` selection uses root scope. |
| `steer`, `cancel` | `running agent address` | Control an active Run; keep runtime lifetime with its owner. |
| `rerun`, `fork`, `rewind` | `agent address` | Continue/edit records in that home; `--chat` preserves the binding. |
| `models`, `providers`, `tools`, `caps`, cap-kind `list`, `workspace list` | `agent name, path, ref or url` | Inspect configured sources; reject decorated addresses. Omission uses root configuration except `workspace list`, which requires selection. |
| `chore list` | `agent address` | Read saved definitions/history; live scheduling data is unavailable offline. |
| `chore run/cancel` | `running agent address with the chore driver` | Control its scheduler without enabling a missing driver. |

In resource commands, `name` refers to configuration even when its resident agent
is running. Source queries may prepare inputs/caches but never allocate a retained
roaming root. `info` supplies live resource summaries or retained offline data;
missing offline fields are unavailable, without rebuilding from current sources.

`run FILE` uses `path to a .too file`. `init`, `fmt`, `parse`, and `highlight`
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

Completion reads the same contracts. It may enumerate scoped resident/active
entries or an explicitly supplied source path's retained roots, but never fetches
sources, probes runtimes, or searches for offline roaming homes by short name.
Root command completion stays registry-based; lexical selector suggestions use
shared metadata. Help/completion never invoke command actions.

Remove public `--instance`/`-i`. Private bootstrap receives concrete root/home,
placement, identity, and runtime-generation bindings. There are no old-syntax
aliases, dual format readers, alternate agent positions, or migration paths.
