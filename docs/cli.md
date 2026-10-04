# CLI Orchestration

The CLI resolves selectors, filesystem/environment defaults and execution
transport before calling package-owned services. It does not implement language
evaluation or durable execution rules. `too` aliases `toolang`; the separate
`caps` entry point reuses capability commands. Use command help and the website
for the complete public flag reference.

Implementation starts at [main.py](../src/toolang/cli/toolang/main.py),
[routing.py](../src/toolang/cli/toolang/routing.py), the
[command modules](../src/toolang/cli/toolang/commands/) and
[entry-point registrations](../pyproject.toml).

## Routing and help

Top-level routing uses these command shapes:

- Script commands use `init DIR` and `run FILE [RUNNABLE]`; they appear in the
  Script Commands help panel. `init` creates the executable `aide.too` and
  comment-only `toolang.toml`; [script projects](script-projects.md) owns creation
  and placement. `run` accepts local `.too` files; foreground agents use `serve`.
- catalog commands are command-first only: `new`, `clone`, `list`, and
  `remove AGENT`
- agent-self commands accept either order: `info`, `serve`, `start`, and `stop`
- commands for an agent's execution history, caps, tasks, or chores require
  the target first, such as `too alice retry RUN` or
  `too alice skill list`

A command name wins whenever an unassigned token could be either a command or
a dynamic name. Use `agent:NAME` to force a colliding resident target. After a
local `.too` target, use `agic:NAME`, `flow:NAME`, or `runnable:NAME` to force a
colliding runnable name. A token ending in `.too` selects a local source path
even when that path does not exist; use `agent:NAME` for a resident name ending
in `.too`. Once a command is selected, its remaining operands are parsed by
that command and are not reclassified.

A target without a command shows the commands accepted by that placement.
Plain resident names are recognized from the selected root's agent catalog;
explicit resident selectors and remote selectors are unambiguous. Showing
remote target help does not resolve or fetch the agent. An incomplete selected
command shows its own help before target existence or other runtime validation.

## Agent Selectors

Runtime commands accept these selector forms:

| Form | Meaning |
| --- | --- |
| `name` | A local managed agent such as `alice` |
| `agent:name` | An explicit local managed agent, including a name that collides with a command |
| `shorthand` | A convention-based remote selector such as `brice/alice` or `toolang.ai/alice` |
| `ref` | A canonical remote ref such as `github://brice/agents/alice.too@main` or `https://toolang.ai/alice.too` |

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

`rev` is one git revision token. Toolang does not distinguish branch, tag, and
commit in selector syntax.

Foreground runtime port selection depends on the agent mode:

| Mode | Selector | Default port behavior |
| --- | --- | --- |
| Resident | Local managed name such as `alice` | Reuse the agent's last port when available, otherwise choose from `7001-7999` |
| Visiting | Remote selector such as `brice/alice` or `https://toolang.ai/alice.too` | Reuse the visiting root's last port when available, otherwise choose an OS temporary port |
| Script run | Local `.too` path with an agic or flow name | No port for embedded host execution; attached and temporary guest execution use the selected AgentServer endpoint |


## Script command boundary

`run FILE [RUNNABLE]` builds help/input collection from the parsed
program. `_` selects the unnamed entry; omitted selectors choose it when present,
otherwise show file help. No synthesized runnable is created.

Help requires neither stdin reads nor runtime preparation. File help lists the
unnamed entry first, then named agics and flows; runnable help derives arguments,
required markers, documentation and flow outlines from the AST. Named assignments
precede primary input in help even though their command-line order is flexible.
Common options work at file and runnable levels. The exact help renderer is
covered by [CLI tests](../tests/unit/cli/) instead of duplicated flag tables here.

[Call input](call-input.md#script-runnable-calls) owns token capture and required
input behavior. [Script projects](script-projects.md) owns source-local layout,
configuration discovery, workspace grants and file inputs. [Presentation](execution-presentation.md)
owns stdout/stderr, quiet mode and output projection.

## Runtime acquisition and lifecycle

[Agent-server acquisition](../src/toolang/cli/common/agent_server.py) selects:

| Situation | Behavior |
| --- | --- |
| Compatible healthy server already running | Attach; an explicit incompatible sandbox is rejected. |
| No server, effective sandbox is `host` | Embed execution for Chat/script callers. |
| No server, another sandbox selected | Create a command-owned temporary server and connect remotely. |

This applies to resident, roaming and visiting layouts. Readiness/profile checks
precede remote use; an unhealthy recorded workload never triggers a competing
embedded executor. Closing a caller stops only its own temporary server, not an
attached one. `RunClient` remains the run transport boundary.

`serve` handles local and remote selectors in the foreground. `start` requires a
resident agent and returns after readiness. Both resolve a concrete `LaunchSpec`
and use the same sandbox lifecycle; hidden `_serve` is the AgentServer process
entry point. `stop` reloads the persisted sandbox reference, stops the workload,
and releases resources. Agent removal uses that lifecycle before deleting home.
Process names are not discovery or ownership evidence.

An explicit sandbox overrides root/home configuration; absent both, use `host`.
The control reference under root `.sandbox/<agent>/` is separate from runtime
status. Host references validate PID plus creation time; Docker retains immutable
container identity. Missing/unverifiable control state fails rather than adopting
an unrelated process. See [layout](layout.md) and [sandbox plugins](plugins.md#sandbox).

On automatic port selection, `start` first tries the last runtime port, then
scans `7001-7999` in order while excluding recorded local-agent ports.
Foreground operational progress goes to stderr; background server output goes
to the agent log. Embedded scripts use a per-run log when `PY_LOG` is set;
attached/temporary scripts leave server logging with that server.

## Configuration and guest startup

Startup `--allow`, `--default` and `--limit` values join matching
`TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*` and `TOOLANG_LIMIT_*` environment values
as concrete frozen overrides. Repeated CLI allow values accumulate; each default
or limit field appears once. [Execution policy](execution.md#policy-resolution)
owns precedence, ceilings and limits. `--compact-model` configures a newly started
runtime's model for [automatic compaction](models.md#automatic-compaction-configuration).

`--dev [PATH]` applies only when starting a new non-host guest. A wheel path or
recursively searched directory selects a concrete Toolang wheel; newest mtime
wins, with absolute path breaking ties. Bare/empty PATH means process cwd, not
script directory or agent home. A following non-option token is consumed as
PATH; `--dev=.` avoids consuming a runnable selector. It does not rebuild source
or alter attached/embedded runtimes. Build with `uv build --wheel` first.

Sandbox selectors and implementation settings are separate. Root/home plugin
configuration is merged before invoking the factory; status/stop/recovery read
current plugin settings. The plugin reports host/guest placement and interprets
its own runtime paths. The controlling CLI supplies a resolved environment and
never stores callbacks or credentials in its durable workload identity.

Docker startup preserves source-local roaming links as explicit read-only guest
files. Failure diagnostics distinguish package installation, compatibility and
server readiness. Cleanup failures retain the recovery reference; relaunch must
not discard it. Guest progress tokens are bounded presentation data and cannot
authorize readiness or cleanup. Detailed implementation is in
[Docker sandbox](../src/toolang/plugin/sandboxes/docker/sandbox.py) and
[lifecycle](../src/toolang/up/).

## Inspection and control

Historical `inspect` is read-only and does not load a runnable or start a server.
It navigates [record references](run-step-records.md), outputs, calls and trees.
Direct selected values can resolve typed references; JSON field inspection keeps
canonical stored values. Human field tables show direct children and bounded
previews, without dereferencing every row. Tree projection is a consistent
structural snapshot, not replay of live event interleaving.

Retry/rerun and control commands select a Run or its owning thread, then use the
appropriate local/hosted execution path. A thread selects its active Run for
steer/cancel and its latest terminal root for retry/rerun/fork/rewind. Concrete
lifecycle behavior belongs in [execution](execution.md). Do not infer process
launch or transport behavior from the command name alone.

Resource inspection uses effective Setup/State views without executing the agent.
Omitting an agent uses root scope, never an implicit `agents/default`.
`--all` selects a diagnostic view; it grants no execution authority. See
[queries](queries.md) for matching and public projections. Resource JSON arrays
have no human summary; human lists report displayed rows.

Plugin inventory (`catalogs`, `adapters`, `toolsets`, `sandboxes`, `channel list`)
reads entry-point names/distributions without invoking factories, loading agent
configuration, or applying resource allow policy. Internal toolsets are hidden
unless requested with `--all`; leaf-tool inspection is a different surface.

## Verification anchors

[Routing](../tests/unit/cli/test_cli_routing.py),
[CLI integration](../tests/integration/cli/),
[local Chat](../tests/integration/cli/test_chat_local_execution.py),
[remote Chat](../tests/integration/cli/test_chat_remote_execution.py) and
[sandbox lifecycle](../tests/integration/up/test_sandbox_lifecycle.py) verify
routing, preparation, transport and ownership across process boundaries.
