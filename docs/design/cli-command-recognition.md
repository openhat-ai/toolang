# CLI Command Recognition and Agent Parameters

Treat AGENT as an ordinary command parameter whose value may appear before the
command. Recognize the command first, then use its parameter contract for
binding, validation, and usage. Script execution participates in the same
recognition flow and supplies its signature dynamically.

This design covers command lookup, agent parameters, script entry selection,
help, diagnostics, and completion. It leaves runtime acquisition, source
providers, instance identity, and authored-language semantics with their current
owners. [CLI](cli.md#routing) describes the command surface;
[Scripts](scripts.md#runnable-input-and-help) defines script input syntax.

## Agent parameter declaration

Declare agent capabilities on the parameter, using a small `AgentArgument`
factory compatible with Typer's `Annotated` parameter declarations:

```python
def top(
    agent: Annotated[
        str | None,
        AgentArgument("resident,before,after"),
    ] = None,
): ...

def info(
    agent: Annotated[
        str,
        AgentArgument("resident,visiting,roaming,after,before"),
    ],
): ...
```

A command has at most one AGENT parameter. Without one, it does not accept a
selected agent; operands such as a new agent's NAME remain ordinary arguments.

| Information | Authority |
| --- | --- |
| Required or optional | The native parameter's `required` and `default` values |
| Accepted positions | `before`, `after`, or both tags |
| Recommended position for usage | The first position tag in declaration order |
| Accepted placements | One or more of `resident`, `visiting`, `roaming` |
| Selector syntax and conversion | The agent parameter type and existing source/identity owners |

Parse tags once when building the command into immutable metadata: sets of
accepted placements and positions, plus the first position tag for usage.
Reject unknown or empty tags, missing positions, missing placements, and multiple
AGENT parameters as declaration errors. Ignore whitespace and collapse duplicates
while preserving the first occurrence. Write placement tags first, followed by
position tags in recommendation order: `resident,before,after` recommends prefix
AGENT; `resident,after,before` recommends suffix AGENT. Both accept either position.
Placement order has no behavioral effect; omitted tags grant no capability.
Requiredness is not another tag.

An optional parameter that accepts `after` must be the last positional argument
and cannot coexist with a variadic positional argument or belong to a command
group, where it would consume an omitted AGENT's subcommand. Reject declarations
that would require guessing whether a value belongs to AGENT or another parameter.
Before-only optional AGENT does not impose this restriction.

Static and script-generated commands expose the same parameter metadata.
Recognition and rendering read those objects, without re-reading Python
signatures or maintaining another command-policy table.

## Unified command recognition

Let native Typer parsing consume root options first. Global options belong
before the command or agent; their values and root `--` handling remain native.
Use the complete registered command collection, including hidden commands and
aliases, independently of help visibility or agent capabilities.

Apply these rules to the remaining tokens, in order:

1. If the first token names a registered command, select it. Selecting `run`
   explicitly enters script recognition.
2. Otherwise, if the second token names a registered command, select that
   command and record the first token as its raw prefix AGENT.
3. Otherwise, if the first token is a local `.too` path candidate, recognize an
   implicit `run` invocation. File existence is not a routing condition.
4. Otherwise, if the first token has explicit agent syntax or names a known
   resident, diagnose the second token as the missing or unknown command.
5. Otherwise, diagnose the first token as an unknown command.

Only the command position is examined. Never search later tokens for a command,
extract options from messages, or retry another interpretation after selection.
After selecting a command, reject a prefix if it has no AGENT parameter or does
not allow `before`. Use native `NoArgsIsHelpError` with the selected command's
Context to show its help and usage with exit status 2; do not add a separate
AGENT rejection message or fall back to script execution.

Paths, `.too` suffixes, source schemes, URLs, `agent:NAME`, and instance-shaped
`NAME@...` tokens are candidate evidence, not proof of valid selectors. Syntax
conversion follows command selection. A registered command wins a name collision;
`agent:NAME` explicitly selects the resident interpretation.
Local script candidates are non-URI operands ending in `.too`; explicit source
schemes, including `agent:`, take precedence over that suffix.

A known resident or explicit ref without a command reports `Missing command`;
following it with `--help` or `-h` displays native root help. A bare unregistered
name reports `No such command`. With two unknown bare words, the first is treated
as the command; `agent:NAME` removes that ambiguity. No agent-specific command
tree is created.

## One script command with two entry forms

Register `run` once. A leading local `.too` path can supply the implicit form of
that command; the path is not another registered command. Both forms use the same
script resolver, signature builder, parameter handling, and execution callback.
The examples assume `work.too` declares flows `research`, `chat`, and `list`,
plus an unnamed entry.

| Invocation | Selected operation |
| --- | --- |
| `too run work.too research` | Script runnable `research` |
| `too work.too research` when `research` is not registered | The same script runnable |
| `too work.too chat` | Built-in `chat` with AGENT `work.too` |
| `too run work.too chat` | The script's runnable named `chat` |
| `too work.too flow:chat` | The script's explicitly typed runnable |
| `too work.too list` | Reject AGENT for built-in `list`, even if the script defines `list` |
| `too work.too -- chat` | The unnamed entry with literal primary input `chat` |

Explicit `run` commits to script interpretation. Retain typed selectors
`agic:NAME`, `flow:NAME`, `runnable:NAME`, and unnamed-entry selectors from the
script contract. Remote refs and URLs do not become implicit scripts because
their text ends in `.too`; this design retains local-file script execution.

The unified recognizer delegates script header and runnable selection to the
script module. Its existing option/input boundary rules determine where a
runnable can occur. Read and parse Program once, resolve the runnable, and pass
both forward. Build only the selected runnable's dynamic signature; file help
needs its runnable names/descriptions, not every runnable's complete signature.
Both stages derive requiredness, types, and help from the same authored
declaration. Preserve assignment, Content, stdin, and coercion semantics in the
script input collector rather than duplicating them in agent routing.

| Help or omission | Behavior |
| --- | --- |
| `too run --help` | Static script-entry help; no file read |
| `too work.too --help`, `too run work.too --help` | File help with runnables and common options |
| Either script form with `research --help` | The selected runnable's dynamic help |
| Either script form without a runnable | Execute the unnamed entry, or show file help if absent |

Preserve the original entry spelling for usage. File help may read/parse the
file, but never reads stdin, prepares State, creates execution records, or starts
a runtime. Missing files, directories, and invalid source retain their own file
or source diagnostics after script selection.

## Parameter binding and native parsing

Keep the registered command identity at cmd. Record the raw prefix, unconsumed
tail, and original entry form in its invocation Context; resolved script data
includes the already parsed Program and selected runnable. Neither the agent
nor the file shorthand becomes another registered command.

Install the AGENT parameter adapter once through the existing command factories:

- For `before`, source AGENT from Context and omit its positional parser slot.
- For `after`, use the native positional parameter.
- With both tags, a supplied prefix takes that parameter slot; otherwise use the
  native position. Remaining words are command operands, not a second agent guess.
- With an omitted optional prefix, use the native default without consuming a
  command operand. For example, `too search hello` with optional prefix AGENT and
  required QUERY leaves `hello` for QUERY.

Use native parameter processing for conversion, defaults, requiredness,
callbacks, and `ctx.params["agent"]`; see [Typer Context](https://typer.tiangolo.com/reference/context/).
Mark a prefix as command-line input, not an empty-string placeholder or a
`default_map` default. Selector conversion and placement checks do not acquire
sources. Current optional declarations default to `None` for root/global scope.
A supplied prefix counts as input for a leaf command's native no-arguments
handling, so a complete `too AGENT start` invocation executes normally.

Nested command groups bind only invocation data in their callback and pass it
through Context. Executable leaf callbacks own existence checks, source
preparation, and runtime work after native parsing succeeds. Shared command and
parameter objects remain unchanged across calls, including concurrent calls.
Ordinary callbacks receive the converted argument; descendants of an owning
group read that selection from Context. Neither path uses process-global target
state or repairs callback defaults between invocations.

Existing instance syntax remains governed by [Instance selection](cli.md#instance-selection),
including `--instance`/`-i` and canonical IDs. Recognizing `A@I` as an agent
candidate does not make this currently rejected spelling valid. Unsupported
instance spellings receive an AGENT diagnostic rather than a command error.
Instances retain their underlying placement instead of adding a placement tag.

## Usage and diagnostics

Generate required/optional notation from native parameters and position from
the AGENT tags. Describe accepted forms as `Agent name`, `Agent name, ref, URL`,
or `Agent name, path, ref, URL`, without internal placement labels.
Usage shows one recommended form, using the first position tag in the declaration.
When both are accepted, parsing still supports both. This recommendation is
derived from the tags, without another setting. Keep existing help panels,
rendering, and command-specific no-arguments behavior.

| Condition | Diagnostic |
| --- | --- |
| Unknown command | Native `No such command 'NAME'.`, with ordinary typo suggestions |
| Prefix supplied to a command without AGENT or to an after-only command | Native help and usage for the selected command, exit 2 |
| Malformed selector | Native `Invalid value for 'AGENT': ...` from its owner |
| Unsupported placement | `Invalid value for 'AGENT': remote agents are not supported` |
| Invalid command options/operands | Native option, conversion, missing-argument, or extra-argument diagnostics |

Presence/position routing errors happen after command selection and before its
argument parsing. On an allowed form, native eager help precedes AGENT syntax
and placement validation. Unknown root commands never trigger selector
conversion; source errors never receive command typo suggestions. Lookup, form, and native
parameter failures exit 2; explicit help exits 0. Preserve operational failure
and interruption codes, including failures from either script entry form.

Root help/completion uses registry names without loading every command or
placement-filtering unknown signatures. Selected-command help and AGENT
completion read its parameter contract; completing agent names may enumerate
residents. Static `run` help is declared at the entry point and does not load the
script runtime.

## Command capabilities

The following groups summarize parameter declarations; they are not another
runtime registry. Dual positions suit commands where AGENT is the only positional
operand; the declaration still determines the recommended spelling.

| Commands | AGENT tags | Required |
| --- | --- | --- |
| `chat` | `resident,visiting,roaming,before,after` | Yes |
| `workspace`, `inspect`, `steer`, `cancel`, `rerun`, `rewind`, `fork` | `resident,visiting,roaming,before` | Yes |
| `chore` | `resident,before` | Yes |
| `cd` | `resident,visiting,roaming,before,after` | No |
| `caps`, `psyche`, `skill`, `service`, `prompt`, `models`, `providers`, `tools` | `resident,visiting,roaming,before` | No |
| `top` | `resident,before,after` | No |
| `info` | `resident,visiting,roaming,after,before` | Yes |
| `start`, `stop` | `resident,visiting,after,before` | Yes |
| `serve` | `resident,visiting,after` | Yes |
| `remove`, internal `_serve` | `resident,after` | Yes |
| Other current commands, including `new`, `clone`, `list`, and `run` | No AGENT parameter | Not applicable |

`serve` and `remove` keep the system-level command first. Inspection commands
such as `caps`, `models`, `providers`, and `tools` keep AGENT before the command.
Runtime placement rules, including the lack of roaming lifecycle commands, do
not change. Intentional compatibility changes are native root-option positioning
and missing-command errors for standalone agent selectors instead of agent-group
help.

## Performance and maintenance

Command-name recognition uses one or two registry lookups. Load only the selected
command before reading its parameter contract. Check a possible resident only
on the diagnostic fallback path, using a single-name lookup or an already
available snapshot; do not scan the resident directory on ordinary invocations.
Tag checks are set membership operations. Placement checks do not fetch sources.
Selecting an agent command with a `.too` operand never parses that file just to
look for runnable names.

Keep Program and dynamic commands scoped to the invocation. Measure root help,
agent command/help, script help, and script execution separately; record cold
startup time, command-module imports, and Program parse counts. No latency
improvement is assumed before those measurements.

Integration stays in the existing CLI factories and modules: `toolang/main.py`
and `routing.py` for recognition; CLI-owned `common/agent_argument.py`,
`context.py`, and `help.py` for binding/presentation; `commands/script.py` and
`common/runnable_parameters.py` for dynamic signatures. Replace superseded
Toolang policy tables and virtual arguments while retaining helpers used by
other callers. Use existing [group extension points](https://click.palletsprojects.com/en/stable/extending-click/).
Parameter construction is the version-sensitive boundary and needs focused
tests against the pinned Typer/Click versions. No additional command registry,
signature schema, error taxonomy, or runtime layer is introduced.

## Acceptance

Offline CLI tests should establish these observable guarantees:

- All required/optional, before/after/both, absent-AGENT, and placement cases
  follow one parameter declaration; invalid declarations fail during construction.
  Usage shows only the first declared position; reversing `before,after` to
  `after,before` changes usage without changing the accepted forms.
- Known-command precedence, explicit resident escapes, malformed refs, unknown
  names, and missing commands select the intended operation and error context.
- Unsupported prefix forms show the selected command's native help with status 2
  and no preparation, callback execution, or separate rejection diagnostic.
- Optional prefix AGENT never consumes a business operand; prefix binding
  preserves native parameter-source, conversion, callback, and requiredness behavior.
- Both script entries resolve the same runnable and produce equivalent dynamic
  help, validation, execution results, and exit codes, including command collisions
  and unnamed entries; input boundaries remain literal.
- Help and invalid native parameters do not acquire agents or create runtime
  artifacts; static agent-command recognition does not parse local script files.
- Reusing one command tree across targets, forms, roots, and concurrent calls
  preserves command/parameter identity and keeps Context values isolated.
- CLI script recognition parses Program once and builds only the selected signature;
  root help and ordinary routing do not import unrelated command implementations.

The repository's default checks and CLI/script documentation are part of
implementation acceptance.
