# CLI Agent Groups

Status: approved for implementation by the user on 2026-10-20.

## Goal and scope

Recognize an agent at the root command lookup boundary, then use an ordinary
Typer command group. Success means correct command lookup errors, native
parameter/help handling, and adding agent commands through existing factories.
The implementation and its acceptance tests accompany this plan.

Preserve the commands and placement restrictions in
[CLI](../design/cli.md#routing), source syntax in
[Architecture](../design/architecture.md#agent-sources), lazy loading, and script
execution semantics, except that `start`, `stop`, `serve`, and `info` accept only
the command-first form. Use existing Typer group and factory hooks. Exclude a
separate argument parser, new error taxonomy, new source providers, and runtime
redesign.

## Root command lookup

Extend the existing `_ToolangGroup.get_command(ctx, name)` hook:

1. Return a registered command when one exists; command names take precedence.
2. Otherwise recognize a valid agent name or an explicit agent-ref candidate.
   Return a dynamic group bound to that target, with `name` as its command name.
3. Otherwise return `None`, letting Typer report `No such command`.

Names follow the existing name syntax. A syntactically valid unregistered name
enters agent mode regardless of existence; lookup occurs during execution.
Consequently, a bare valid name can show agent help before the agent exists.
`agent:NAME` escapes collisions with registered commands. Scheme prefixes take
precedence over `.too` suffixes; non-URI `.too` paths are local sources. Other
explicit path/ref syntax remains a target candidate even when malformed.
Delegate validation to existing name/source owners. Format syntax failures with
Typer's `BadParameter` for `AGENT`, then raise native `ClickException` with exit 2
to avoid command typo suggestions during lookup. Never fall back to another
target kind when validation fails. Recognition performs no fetch, write, or
runtime inspection.

Root options are parsed by the root Typer group before this hook. Use native
option spellings, eager help/version, and `--` handling. Global options belong
before the agent/command, for example `too -r ROOT alice chat`; stop hoisting
`--root` out of command arguments or message bodies.

## Agent command lookup

The agent group reuses the registered command objects. Add one registration
field, `agent_placements`, to declare where a command is available after an
agent. An empty set means top-level only. Reuse placement constants for commands
with the same availability; keep this metadata beside the factory registration.
Use one predicate for command lookup, help, and completion.

`AgentGroup.get_command(ctx, name)` handles only availability:

| Lookup result | Action |
| --- | --- |
| Known command available for this target | Return the original lazy Typer command/group. |
| Known command with no agent placements | Fail with `Command 'NAME' is only available at the top level.` |
| Known agent command unavailable for this placement | Fail with `Command 'NAME' is not available for remote agents.` or the corresponding placement. |
| Unknown command | Return `None`; Typer reports `No such command 'NAME'.` |

Use native `ClickException` with exit 2 for availability failures, keeping
Typer typo suggestions for unknown names. Retain the existing CLI renderer.
No custom diagnostic protocol or automatic argument correction is needed.
Help lists only commands available for the selected placement, with the existing
visibility rules. A native child context supplies paths such as
`too alice chat`; remove virtual AGENT usage prefixes on bound commands.

Keep `new`, `remove`, `start`, `stop`, `serve`, and `info` as ordinary root
commands with native positional arguments; their `agent_placements` is empty.
For example, use `too info alice`; reject `too alice info` as top-level only.
Executable commands read their selected agent only from `CliContext`; never
copy, rebuild, or mutate a command or its parameters
to bind an agent. Do not rewrite argv, reset callback parameters, or duplicate
option signatures. Root-only target arguments use the shared context binder
after native parsing; agent commands read the context inherited from their group.

Local `.too` entry points reuse the existing script group and runnable selector
hooks. Expose available built-in agent commands through the same availability
predicate; preserve built-in command precedence, typed runnable escapes,
unnamed entry execution, and literal script tails. Explicit `run` keeps its
current native command and script dispatch. This work does not introduce another
script parser.

## Native parsing and execution

Once a command is returned, Typer owns nested command lookup, option/argument
parsing, parameter conversion, help, and invocation. In particular, use the
native `No such option`, missing-argument, and unexpected-argument errors rather
than examining or translating the argument tail.

Bind the selected target into a child `CliContext` when its group context is
created, preserving the parent's resolved root. Group callbacks do not fetch
sources or prepare agents: they run before child argument parsing. Resolve or
prepare the selected agent only from the executable command callback, after
native parsing succeeds. Existing source/runtime helpers retain ownership of
that work. This also makes command help and invalid arguments independent of
remote availability and cache writes.

Representative results, rendered through existing Typer/CLI facilities:

| Invocation | Error or behavior |
| --- | --- |
| `too alice caht` | `No such command 'caht'.` with agent-group help context. |
| `too alice list` | `Command 'list' is only available at the top level.` |
| `too alice chat --unknown` | Native `No such option: --unknown`. |
| `too alice chat --help` | Native command help without agent preparation. |
| `too gh:owner chat` | `Invalid value for AGENT` with the source parser's GitHub-ref error. |
| `too alice start` | `Command 'start' is only available at the top level.` |
| `too gh:owner/alice top` | `Command 'top' is not available for remote agents.` |

Lookup, availability, source syntax, and parameter errors use Typer usage errors
(exit 2). Preserve existing operational failure and interrupt handling. Native
help and incomplete-input behavior follow the existing CLI group configuration.

## Touchpoints and acceptance

Keep the change within existing modules: `cli/toolang/main.py` for root lookup
and registration metadata, `cli/toolang/routing.py` for the agent group, and
`cli/common/context.py` for target parsing, context binding, and lazy preparation.
Keep command availability metadata on the existing lazy command factory.
Adjust command callbacks only to defer their current target preparation.
Remove superseded roaming/visiting/resident argv dispatch paths and preparation
rules. No new routing package, registry framework, or invocation subsystem is
needed. Update `docs/design/cli.md` and changelog coverage during implementation.

Acceptance scenarios in CLI unit/integration tests:

1. Current root/agent availability and placement rules hold. Registering a
   synthetic command with `agent_placements` adds lookup, help, and completion
   without editing group logic. Agent groups reuse the exact registered objects;
   their help and completion do not load implementations. Lifecycle commands
   accept native operands/options only at the root.
2. Names, explicit resident selectors, remote refs, local paths, and name
   collisions select the right group. Malformed refs retain their own error;
   recognition never fetches or writes. Bare valid names have the documented
   behavior independently of existence.
3. Unknown commands and known unavailable commands produce the distinct errors
   above. Native parameter errors identify the selected command and do not
   execute callbacks or prepare an agent, including nested groups and missing
   operands. Help is equally free of preparation.
4. Native root help/version, all root option spellings, separators, option-like
   values, Talk messages, and script tails remain correctly owned. A root option
   after an agent/command receives the native error rather than being hoisted.
5. Direct scripts, explicit `run`, shebangs, runnable collisions/defaults, lazy
   loading, root-versus-agent scope, and concurrent context isolation retain
   their existing behavior.
6. Implementation passes the repository's default Ruff, type, and offline pytest
   checks, relevant link validation, and `git diff --check`.

Native global-option positioning, unregistered valid names selecting agent
groups, and command-first-only lifecycle/info forms are intentional compatibility
changes. The main risks are leaking context between invocations and changing
local script selection. Cover both through real Typer entry points, including
reusing one command tree. No open design questions remain.
