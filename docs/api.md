# Control Surfaces

This document defines the public CLI and local agent HTTP API.

Interactive CLI, TUI, and WebUI surfaces may parse the `ChatInput` forms
defined by [input-syntax.md](./input-syntax.md). Quick commands remain local to
the interaction surface. Execution surfaces resolve `RunOverride` and
`RunnableInputRaw` values into the structured run request described here.


## CLI

The CLI entry points are:

- `toolang`
- `too`
- `caps`

Top-level commands are:

- `new`
- `clone`
- `remove`
- `list`
- `info`
- `run`
- `start`
- `stop`
- `chore`
- `task`
- `psyche`
- `skill`
- `service`
- `prompt`
- `chat`
- `steer`
- `cancel`
- `retry`
- `rerun`
- `rewind`
- `fork`
- `inspect`
- `caps`
- `models`
- `providers`
- `tools`
- `catalogs`
- `adapters`
- `toolsets`
- `sandboxes`

Global options:

- `--root`
- `--version`

Cap commands:

- `caps [AGENT] list [--query QUERY]`
- `caps [AGENT] <kind> list [--query QUERY]`
- `caps [AGENT] <kind> new <name>`
- `caps [AGENT] <kind> edit <name>`
- `caps [AGENT] <kind> delete <name>`
- `caps [AGENT] <kind> add <ref>`
- `caps [AGENT] <kind> remove <name>`
- `caps [AGENT] <kind> template [template-name]`

`<kind>` is one of `psyche`, `skill`, `service`, or `prompt`. Without `AGENT`,
cap mutations target root caps. With `AGENT`, they target the selected agent home's caps.

List output uses:

- `KIND`
- `CAP`
- `ORIGIN`
- `FORM`
- `SCOPE`
- `SOURCE`

Kind-specific list commands omit `KIND`.

`SOURCE` is the authored source location. File sources are paths relative to
the Toolang root. Inline caps use `<path-to-agent.too>:<line>`. External GitHub
sources are shown as directly accessible `https://github.com/...` URLs.

`FORM` accepts `authored`, `inline`, `configured`, and `referenced`. `SCOPE`
accepts `root`, `home`, and `here`. Query predicates use typed fields such as
`scope=home`, `form=authored`, and `origin=remote`. Combined lists use
`psyches`, `skills`, `services`, and `prompts` as identity prefixes;
kind-specific lists also accept local cap names. Run `too query COLLECTION` for
the complete query contract.

Typical usage:

```bash
toolang new alice
toolang list
PY_LOG=toolang.execution=info toolang ./examples/script-playground.too summarize "Summarize this workspace"
toolang ./examples/script-playground.too --help
toolang ./examples/script-playground.too summarize "Summarize this workspace"
toolang ./examples/file-agent.too --inbox ./inbox
toolang run alice
toolang run alice --sandbox docker
toolang run brice/alice
toolang run https://toolang.ai/alice.too
toolang clone brice/alice
toolang start alice
toolang start alice --sandbox docker
toolang stop alice
toolang info alice
toolang alice info
toolang ./examples/deep_search.too info
toolang alice chat
toolang alice chat term_3nprht9x
toolang alice chat --sandbox docker
toolang alice inspect controls
toolang alice inspect threads
toolang alice inspect runs
toolang alice inspect term_3nprht9x runs
toolang alice inspect run_ppkp9e94 steps
toolang alice inspect run_ppkp9e94.0/output/value
toolang alice inspect run_ppkp9e94.0 call
toolang alice retry run_ppkp9e94 --limit tokens=200000 --limit time=900
toolang alice rerun run_ppkp9e94 --default model=openai/gpt-5
toolang alice steer run_ppkp9e94 "Use the smaller patch"
toolang alice cancel term_3nprht9x
toolang alice rewind run_ppkp9e94
toolang alice fork run_ppkp9e94
toolang models
toolang catalogs
toolang toolsets
```

Top-level routing uses three command shapes:

- catalog commands are command-first only: `new`, `clone`, `list`, and
  `remove AGENT`
- agent-self commands accept either order: `info`, `run`, `start`, and `stop`
- commands for an agent's execution history, caps, tasks, or chores require
  the target first, such as `toolang alice retry RUN` or
  `toolang alice skill list`

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

Thread and run inspection, retry, rerun, steering, cancellation, rewind, and
fork open the selected agent's durable execution store directly. They do not
start or call the agent HTTP server. A run id selects its owning thread; a
thread id selects its active run for steering or cancellation and its latest
terminal run for retry, rerun, rewind, or fork. Retry reopens the same root run
and optionally starts at `--anchor`; rerun starts a new root run from the source
invocation. Fork retains the anchor run, while rewind removes it and the
following visible suffix.


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
| Roaming file runtime | Local `.too` path with `--inbox` and no agic name | Choose an OS temporary port |


## Script Run Surface

A script run uses one local `.too` source path directly:

```bash
toolang SCRIPT RUNNABLE [OPTIONS] [ARGS] [INPUT]...
```

Script progress, inspection output, and chat TUI activity use the shared
execution presentation language defined in
[execution-presentation.md](./execution-presentation.md). Script mode retains
its stdout/stderr contract; it does not use the TUI renderer.

Arguments:

- `SCRIPT` is the local Toolang script or agent file
- `RUNNABLE` is the uniquely named public agic or flow to run
- `ARGS` provide named runnable parameters, written as `NAME=VALUE`
- `INPUT` values form the primary source of one `RunnableInputRaw`; script mode
  parses policy prefixes but does not accept chat quick commands

Behavior:

- a local `.too` path enters script-run mode
- `agic:NAME`, `flow:NAME`, and `runnable:NAME` explicitly select a runnable
  when its name collides with a top-level command
- default agics and generated internal agics are not exposed as script commands
- runnable command descriptions come only from their authored `doc`
- stdout is reserved for the final runnable result
- progress messages are written to stderr by default
- TTY progress uses color and live replacement; non-TTY progress is stable,
  append-only, and contains no ANSI control sequences
- `-q` or `--quiet` suppresses prepare and execution progress
- `--sandbox SELECTOR` selects the execution sandbox for this invocation; an
  already-running compatible AgentServer is attached instead
- `--dev PATH` installs Toolang in a newly started guest from one wheel; a
  directory selects its newest Toolang wheel recursively
- `--default model=REF` supplies one exact invocation model binding
- `--limit FIELD=VALUE` overrides one run-limit field; it may be repeated
- `--allow COLLECTION=QUERY` sets one of `models`, `tools`, `psyches`, `skills`,
  `services`, or `prompts` and may be repeated
- host execution remains embedded when no AgentServer is active; a selected
  non-host sandbox starts a temporary AgentServer and cleans it up after the run
- an explicit sandbox that does not match an active AgentServer is rejected
  before creating a thread or run; `--dev` cannot modify an active AgentServer
- `PY_LOG=toolang.execution=info toolang a.too summarize ...` writes runtime logs
  under `.toolang/agents/<agent>/.runtime/logs/<runnable>/<run_id>.log`
- `PY_LOG=debug toolang a.too summarize ...` also writes lower-level provider
  and HTTP logs to that run log file
- `toolang a.too --help` lists public runnables
- `toolang a.too summarize --help` prints runnable-specific dynamic usage
- `toolang a.too` shows usage instead of running a default agic
- a runnable missing a required named argument or required primary input shows
  its dynamic help and does not create a run; omitted input is first read from
  stdin when available
- script run reads one effective Setup and State publication; the executor
  narrows their model, tool, and cap collections with request and runnable
  directives
- `NAME=VALUE` supplies one named argument and is coerced using its declared
  parameter type
- `INPUT` rules:
  - adjacent ordinary shell words are joined with spaces into one text item
  - `TEXT` adds one text part; use `@@TEXT` for literal text beginning with `@`
  - `@PATH` adds one path-based percept part; text-like paths become text parts
  - image extensions such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, and `.svg` infer image parts
  - `.mp3` and `.wav` infer audio parts
  - supported document extensions infer document parts
  - unsupported video, archive, executable, and binary formats are rejected
  - omitting input reads non-interactive stdin; `-` explicitly selects stdin
- `--` ends option parsing so later arguments stay `INPUT` values
- `--option` is reserved for Toolang runtime options
- `PY_LOG` uses env_logger-style directive formatting and does not affect stdout
- key execution events are recorded in `runs.db` for script runs just like chat,
  task, and chore runs

The same roaming source path can select agent commands:

```bash
toolang SCRIPT info
toolang SCRIPT run
toolang SCRIPT chat [THREAD]
toolang SCRIPT inspect SUBJECT... [PROJECTOR] [--human | --json]
toolang SCRIPT retry RUN [--anchor STEP]
toolang SCRIPT rerun RUN
```

It also supports `steer`, `cancel`, `rewind`, and `fork`. These command names
immediately following the source are interpreted as agent commands. Prefix a
same-named runnable with `agic:`, `flow:`, or `runnable:` to invoke it. The
removed `threads` and `runs` command names are available to authored runnables
without a typed prefix.

Visiting selectors support the same agent-self and execution-history commands:

```bash
toolang brice/alice info
toolang brice/alice run
toolang brice/alice chat [THREAD]
toolang brice/alice inspect SUBJECT... [PROJECTOR] [--human | --json]
toolang brice/alice retry RUN
```

Commands that execute or inspect current program state (`info`, `run`, `chat`,
`retry`, and `rerun`) resolve and materialize the remote program. History-only
commands, including `inspect`, derive the stable visiting layout and read its
existing `runs.db` without fetching the source.

### Historical record inspection

`inspect` evaluates a subject chain and an optional terminal projector:

```text
toolang AGENT inspect SUBJECT... [PROJECTOR] [--human | --json]
```

The former top-level `threads` and `runs` commands have been removed. Replace
them with `inspect threads`, `inspect runs`, or `inspect THREAD runs`. The old
filters have no inspect equivalent; use `--json` and filter externally when
needed. Inspect collections are unbounded rather than limited to 50 rows, and a
missing `runs.db` is an error rather than an empty successful result.

The initial collection and relation subjects are:

```text
threads                 every visible Thread record
runs                    every visible Run record
controls                every visible Control record
THREAD runs             every visible Run belonging to THREAD
RUN steps               every visible Step physically owned by RUN
STEP runs               every visible Run directly accepted by STEP
LOOP_STEP steps         every direct same-Run Step owned by LOOP_STEP
```

Collections are unbounded and preserve durable ordering. Their JSON form is a
bare array of canonical record objects; every object is identical to inspecting
its printed Pointer individually. A missing `runs.db` is an error, while an
existing store with no matching records returns an empty collection.
`controls` combines all Thread Controls with Controls belonging to visible Runs.
It excludes a Run Control when its owning Run or parent Step is ejected and
orders records by creation time descending, target ascending, then Control index
descending.

A Pointer still selects one durable Thread, Control, Run, or Step record, or a
field inside that record:

```text
term_ab12                         Thread record
run_ab12                          Run record
run_ab12.0                        Step record
term_ab12@0                       Thread Control record
run_ab12@1                        Run Control record
run_ab12.0/output/value/0         nested field
run_ab12@1/payload/locals/0/value nested Control field
```

`.` enters the Step hierarchy, `@` selects a Control index, and `/` enters a
field using RFC 6901 escaping (`~0` for `~` and `~1` for `/`). Run ids occupy
the `run_` namespace; thread ids cannot begin with `run_`.

Exact `threads`, `runs`, `controls`, and `steps` tokens are reserved by this
grammar and take precedence over Thread Pointer parsing. The accepted
transitions are Agent to `threads`, `runs`, or `controls`, Thread to `runs`, Run
to `steps`, container Step to `runs`, and loop Step to `steps`. The Step
relations are direct and return an empty canonical array when no child was
created. Collections, Controls, non-container Steps, and fields do not accept
relation subjects.

Every successful query selects one projector after its subject resolves:

| Projector | Selection | Human result | JSON result |
| --- | --- | --- | --- |
| `records` | collection subject | summarized record rows | canonical record array |
| `fields` | browsable Pointer value | direct child fields | exact selected value |
| `value` | scalar, empty, resolved, or specialized value | rendered value | exact selected value |
| `tree` | Run with the explicit terminal name | hierarchical durable execution | flat depth-first node array |
| `call` | supported Step with the explicit terminal name | Step-owned historical call | normalized call or flat node array |

`records`, `fields`, and `value` are implicit view kinds, not accepted command
tokens. `tree` is explicit only on a whole Run. `call` is explicit only on a
whole model, tool, run, par, or loop Step. Run and Step subjects never expose
equivalent projector names, and the removed `model-call` spelling is not an
alias.

`call` on a model Step reconstructs the complete normalized call persisted for
that Step, including instructions, messages, tool definitions, the
structured-output schema, and continuation data:

```bash
toolang alice inspect run_ab12.0 call
toolang alice inspect run_ab12.0 call --json
```

This is distinct from `run_ab12.0/given/call`, which exposes compact persisted
references. Projection is local and read-only: it does not prepare a call,
select a model, construct a provider-native request, or send provider traffic.

The Human view follows call lifecycle order: summary, non-empty instructions,
messages, tools, output contract, continuation, and result. Empty sections are
omitted. Request text and result payloads are never truncated. All tool
signatures and descriptions are shown, while parameter schemas remain
summarized by the signatures. Text preserves authored line breaks. Messages use
descending review numbers, tool-call and tool-result parts retain their fenced
Human layout, structured values use indented key and index lines, and the
output contract uses formatted JSON. Section headers contain only their title:
message and tool counts and result Pointers are not appended. Result is last.
The JSON view keeps the complete normalized call and exact `output_schema`
value, including `null` for unstructured and historical calls.

`call` on a tool Step shows a summary, its persisted plugin, normalized
invocation, and the stored result payload without truncation when present. Tool
results retain the same fenced, structured Human layout used inside model
messages. Both identifiers are shown only when they differ. Its JSON is the bare
canonical `ToolCall` with exactly `tool_call_id`, `call_id`, `name`, and `input`;
it does not add the result or an inspection envelope.

`tree` on a Run and `call` on a run, par, or loop Step render the same durable
structural model. A Run tree starts at that Run. A container-Step call starts at
the selected Step, omits its owning Run and siblings, and retains each accepted
child Run as a separate node:

```text
NODE                  ACTIVITY                       OCCUR
run_parent            ✔ <flow>  parent
└─ run_parent.0       ✔ [run]   <agic>  child        3 items · 2 lanes
   └─ run_child       ✔ <agic>  child                item 2 · lane 1
      └─ run_child.0  • [model] openai/gpt-5
```

Human trees contain exactly `NODE`, `ACTIVITY`, and `OCCUR`. Activity starts
with `•` for pending or running, `✔` for succeeded, and `✖` for failed or
canceled; color distinguishes the statuses that share a marker. A child shows
only one-based item and lane indexes, while the owning Step shows known totals.
JSON is a flat depth-first array with `pointer`, `record_kind`, `step_kind`,
`parent`, `depth`, operation, status, occurrence, timestamps, canonical error,
and metrics. This is a transactionally consistent structural snapshot, not
event replay or a live trace; exact interleaving is unavailable because
execution events are not persisted as a journal.

Human output is the default. Record and container tables use the CLI's
horizontal-rule Rich style. Run collections order identity, runnable, status,
Step count, ownership, and creation time; they never show occurrence. Step
collections use `STEP`, `ACTIVITY`, `CHILD RUNS`, `CHILD STEPS`, optional
`PARENT STEP`, `CREATED`, and `OCCUR`. Their activity uses the same marker
vocabulary as trees, and both child counts are direct visible relations.

Field tables always use `FIELD`, `TYPE`, and `VALUE`. They list direct children
as relative field suffixes and show a bounded preview of the raw canonical value
in the third column. Long or multiline strings include size facts. Field tables
do not unwrap `Local`, follow a Pointer, mark a resolved type, or fail because a
stored Pointer is missing, cyclic, or has a mismatched target. A directly
selected value retains normal Pointer resolution and validation.

Human projections have no trailing context footer. Direct scalar and
specialized values print only their value. Strings have no JSON quotes, and
nullable Human type labels use `T?`. Multiline Part content stays aligned
inside the VALUE cell without a leading bullet. `--json` does not resolve
Pointers and prints only the selected canonical JSON value. The two display
modes are mutually exclusive, and `--type` is not an option. Inspection is
read-only and historical and does not load a runnable.

## File Request Runtime

Roaming scripts can also start a foreground file request runtime without naming
a runnable:

```bash
toolang SCRIPT --inbox PATH [--inbox PATH...]
```

Behavior:

- `SCRIPT` is materialized into its sibling `.toolang` roaming root.
- Each `--inbox` value must name an existing directory.
- Startup enables `runner.file` and `trigger.file`; AgentState watching is always active.
- Startup requires an agic named `file` that accepts primary input and has no
  required named parameters.
- Files already present in an inbox at startup are eligible for processing.
- Newly discovered stable files are passed to the `file` agic using the same
  percept-part classification rules as `@PATH`.
- File request progress is stored in `.runtime/files.db`.
- Finished, failed, and canceled file fingerprints are not automatically retried.
- When a runnable name is present, such as `toolang SCRIPT summarize ...`,
  Toolang uses normal one-shot runnable invocation.


## Runtime Commands

| Command | `name` | `shorthand` | `ref` |
| --- | --- | --- | --- |
| `toolang run` | yes | yes | yes |
| `toolang clone` | yes | yes | yes |
| `toolang start` | yes | no | no |

Behavior:

| Command | Behavior |
| --- | --- |
| `toolang run` | Runs a local agent, or fetches one remote agent program into a stable visiting root and runs it in the foreground |
| `toolang clone` | Clones one local agent, or fetches one remote agent program into a new local managed agent |
| `toolang start` | Starts one local managed agent only. Remote selectors must be cloned first |

`toolang run` and `toolang start` resolve the same `LaunchSpec` and call the
same sandbox lifecycle. A hidden `toolang serve` command is the only
AgentServer process entrypoint. The sandbox implementation launches that
entrypoint locally, in Docker, or in another environment; the server and
executor do not branch on sandbox.

Both commands report the same ordered operational work on stderr: preparing the
sandbox, creating the runtime, and connecting to the Agent API. Docker adds
Toolang installation and compatibility checks. A TTY uses one transient line
without a spinner and shows elapsed time after one second. A non-TTY writes
each action and outcome as an append-only plain-text line. The stable
`Agent NAME running: ...` and `Agent NAME started: ...` result lines are written
only after readiness succeeds.

Both commands accept repeatable `--allow COLLECTION=QUERY`,
`--limit FIELD=VALUE`, and `--default FIELD=VALUE` options. The CLI parses these
with `TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*`, and `TOOLANG_LIMIT_*` into frozen
field overrides passed to `SetupWatcher`.

Setup policy uses the following TOML shape in root and agent-home `config.toml`
files:

```toml
[allow]
models = ["gateway/*"]
tools = ["shell/*"]
skills = ["reviewer"]

[default]
model = "gateway/chat"
runnable = "agic:chat"

[limit]
agic_model_calls = 200
agic_tool_calls = "none"
tokens = 200000
cost = "2.50"
time = 900
```

Limit fields are non-negative. Quoted `"none"` disables a limit. Empty allow
arrays deny all resources in that field; `"none"` clears a default binding.
Text CLI/environment values use `none` for an empty allow set, a cleared
binding, or an unlimited limit, according to the target field. `all` removes
an allow restriction. Empty text is always invalid.

The precedence order is built-in values, root config, agent-home config,
runtime environment, CLI, then any request-level binding or limit fields.
Config is re-read dynamically; environment and CLI mappings remain fixed for
the process lifetime. Each `--default` or `--limit` field may occur once;
repeated `--allow` values for the same domain accumulate within the CLI layer.

When `--sandbox` is omitted, resident run/start commands use the effective
root/home `[sandbox]` binding, falling back to `host` when no binding exists.
An explicit selector, including `--sandbox host`, overrides that binding.
Docker sandbox control is supported from Linux and macOS hosts. Windows users
must run Toolang through WSL2; native Windows host control is not supported.

Chat uses the same explicit/configured/host selection when no AgentServer is
active. `host` stays embedded in the CLI; another selector starts a temporary
AgentServer that Chat stops on exit. If an AgentServer is already running, Chat
attaches to it and rejects an explicit incompatible selector without executing
or restarting the server. `chat --dev PATH` installs a local Toolang wheel in a
new temporary non-host runtime. It is rejected for embedded host execution or
when Chat attaches to an existing AgentServer.

Commands that start a new guest accept `--dev PATH`. This includes `run`,
`start`, `chat`, script runs, `retry`, `rerun`, and roaming file-inbox runtime.
`PATH` is either one Toolang `.whl` file or a directory to search recursively
for Toolang wheels. Directory selection uses the most recent file modification
time and breaks equal-time ties by absolute path. The selected concrete wheel
is staged into Docker and supplies its `too serve` command. Build a current
wheel and select it with:

```sh
uv build --wheel
too alice run --sandbox docker --dev dist
```

`--dev` does not treat a directory as a source project and does not rebuild
after launch. It applies only while starting a new guest: host execution uses
the current Toolang installation, and an attached AgentServer has already
selected its package. When the controlling CLI runs from development source, a
new guest without `--dev` warns that it will install Toolang from the package
index instead of the local source. The warning includes the wheel build command
and does not block launch or query the package index.

Sandbox selection and implementation configuration are separate:

```toml
[sandbox]
driver = "docker"
target = "python:3.13-slim"

[plugin.sandbox.docker]
root = "/root/.toolang"
```

Root and agent plugin tables are merged before the selected sandbox factory is
created. Status, stop, and interrupted-launch recovery re-read this current
configuration; `SandboxState` stores only the sandbox selector and runtime
reference. Each plugin owns its runtime-root configuration and reports whether
the workload runs on the host or in a guest environment; orchestration does not
interpret plugin-specific path settings.

The host fixes the workload's runtime environment before sandbox preparation.
Docker includes every name authored in the root or agent `.env`, then includes
host-process names that match its default environment allow pattern.
That pattern covers Toolang controls, proxy and certificate settings, Python
bootstrap settings, and common model-provider variables. Override it with a
full-match regular expression when another process variable is required:

```toml
[plugin.sandbox.docker]
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'
```

The configured pattern replaces the default and is compiled as written. Root
dotenv values are overlaid by agent dotenv values without applying the pattern.
A host-process value overrides those layers only when its name matches the
pattern.

Docker writes the selected mapping to one mode-`0600` staged dotenv file. Its
comments separate merged root/agent dotenv values from filtered host-process
values, with the process section last to preserve precedence. The file is
bind-mounted read-only over the guest agent's `.env`; the root `.env` is not
mounted, and the original agent `.env` remains hidden behind the nested file
mount. A small bootstrap reads this same generated dotenv into the guest process
before package installation or plugin loading. Dotenv values are literal on
both host and guest, so `${NAME}` is not expanded during either load.
Toolang passes only `TOOLANG_HOST_GATEWAY`, `TOOLANG_ROOT`, and
`TOOLANG_SANDBOX` through Docker's environment arguments; Docker also maps
`host.docker.internal` through the engine's `host-gateway`. The complete staging
mount is read-only in the guest. Staged files are removed on release and after a
failed Docker launch whose workload was removed successfully. If Docker cleanup
fails, the staged files remain with the persisted recovery reference.

For every sandbox implementation, AgentServer is the environment's primary
foreground workload. `run` waits for that workload and releases it on exit,
while `start` returns after the health endpoint is ready. `stop` reloads the
persisted `SandboxState`, stops the primary workload, and releases its sandbox
resources. Agent removal also asks the sandbox lifecycle to release any stopped
workload before deleting the agent home; no caller deletes sandbox control state
as ordinary filesystem data.

Agent entrypoints also share one logging policy resolver:

| Entrypoint | Log destination |
| --- | --- |
| `toolang run` | `stderr` |
| `toolang start` | `agent_log` under the agent `.runtime` directory |
| embedded host `.too` script run | `run_log` under the agent `.runtime` directory when `PY_LOG` is set, otherwise `none` |
| attached or temporary `.too` script run | AgentServer output remains in its `agent_log`; Script progress and result output retain their stderr/stdout split |

The lifecycle persists a versioned recovery reference immediately after the
workload is created, then attaches process-local output observers and performs
the readiness check. The Docker sandbox follows container output locally from
container creation for foreground `run`. Background `start` instead creates the
host `agent_log` with mode `0600` and writes Docker launch diagnostics,
bootstrap errors, and AgentServer output there. Early container diagnostics are
copied to that log before a failed or stopped workload is released, bounded to
the final 2000 Docker log lines and streamed without buffering them in memory.
Diagnostic write failures do not prevent container cleanup. Foreground
interruption, ready-reporting errors, and wait failures stop and release the
workload; cleanup failures preserve `SandboxState` for a later forced stop.
`SandboxState` is host-control data under `.sandbox/<agent>/state.json`, outside
all guest mounts; only per-launch staging children are exposed to Docker.
An older guest-writable `agents/<agent>/.runtime/sandbox.json` is never trusted
or migrated automatically. Its presence blocks launch, stop, and agent removal
with instructions to stop the workload using the previous Toolang version or
clean it up manually.
Likewise, per-launch staging without a matching control reference is preserved
and blocks relaunch or removal until any associated workload is removed and the
staging directory is cleaned manually.

Docker uses the engine's default missing-image pull behavior. Its guest script
quietly bootstraps uv and installs the selected package with `uv tool install`.
For a source-local roaming agent, Docker overlays linked `agent.too` and
`config.toml` targets as explicit read-only guest files so host symlinks do not
become broken paths in the container.
Successful installation suppresses ensurepip chatter, pip's container root-user
warning, package lists, and uv progress bars; installer failure stderr remains
available. Before execution, the guest checks that the installed CLI can start
the required AgentServer. Structured startup errors identify whether package
index or wheel installation failed and recommend a source-appropriate fix. A
compatibility failure from a development CLI recommends `uv build --wheel` and
`--dev dist`; a selected wheel instead recommends rebuilding or selecting a
compatible wheel. Foreground output reports that diagnostic once; background
output also retains the guest diagnostic in `agent_log`.

Guest stage observation uses a unique mode-`0600`, append-only token file under
the agent runtime directory. Tokens are a closed vocabulary for install,
validation, and server-start transitions; they contain no commands, logs, or
environment values. Guest writes are best-effort, and the host reads only a
bounded, regular, non-symlink file. The file is presentation-only: unknown,
duplicate, out-of-order, or stale values cannot affect readiness, recovery, or
cleanup. The referenced file is removed with the sandbox resources.

An active `toolang info` uses the sandbox reference's structured workload
identity. Host workloads show `PID`; Docker workloads show `Container` with the
generated name and a 12-character hexadecimal ID, while recovery retains the
full immutable container ID. Other sandbox kinds show `Runtime KIND:ID` without
assuming their identifiers can be shortened. Older version-1 references without
identity fields remain readable as generic workloads.

When `toolang start` runs without `--port`, Toolang first tries the agent's last
runtime port. If that port is not reusable, Toolang scans its auto-assigned
local range `7001-7999`, starting at `7001` and counting upward, skipping ports
already recorded by other local agents, instead of asking the OS for a random
ephemeral port.


## Model Catalog Commands

- `toolang models`
- `toolang providers`
- `toolang adapters`

`toolang models` shows model catalog entries and current availability,
including:

- canonical provider/model identity
- current `AVAILABLE` value as `yes` or `no`
- right-aligned context and maximum output sizes with underscore digit grouping
  for copyable numeric literals
- input modalities and a comma-separated `CAPABILITIES` list
- right-aligned base input/output prices formatted as `$input / $output` under
  `PRICE ($/1M)`, with every numeric rate shown to two decimal places
- a compact total and per-catalog model counts

Pass repeatable `--query/-q` options to select models, for example
`toolang models --query '*[scope=remote]'` or
`toolang models --query 'openrouter/*[adapter=chat_completions]'`. Run `too
query models` for the exact identity, fields, and operators.

`toolang providers` shows catalog providers and runtime availability.
`toolang adapters` lists installed model adapter names.
`toolang models` is a leaf command with `--query` and `--json`; it has no
`inspect` or `update` subcommands and no `--output` or `--force` options.

Discovered Ollama and llama.cpp records use reported context, output limits,
modalities, and capabilities to populate the same table fields used by remote
models. Their API token prices are explicitly zero; local compute costs are
outside model token accounting.

`yes` means the API is resolved, a required key is present, and the adapter
is installed. Remote API reachability, credentials, and account entitlement
are not probed by this listing. Local endpoints are probed for discovery;
unavailable local models are omitted from the model table.

`toolang providers` shows `ADAPTERS`, `API`, and `ENV` in that order.
`ADAPTERS` is the deduplicated set resolved across the provider's catalog
models, including model-level protocol overrides; it is not a preferred-adapter
hint. Catalog-known protocols remain visible when no implementation is
installed, such as `messages` for Anthropic. An empty or offline local catalog
uses the provider-level adapter signal. Unavailable field values are dimmed.
Multiple alternative environment variables use an unstyled `, ` separator and
are all dimmed only when none is configured. An offline local provider remains
in the table with `AVAILABLE` set to `0` and its API dimmed. JSON output
remains the original models.dev-compatible provider data and does not expose
resolved API, adapter, environment, or readiness facts.
Anthropic uses the known default endpoint `https://api.anthropic.com` when the
models.dev record omits `api`.
The provider table footer mirrors the model footer, for example
`7 providers from 3 catalogs: models.dev 5, ollama 1, llama_cpp 1`.


## Plugin Inventory Commands

- `toolang catalogs`
- `toolang adapters [--json]`
- `toolang tools [--query QUERY]`
- `toolang toolsets`
- `toolang sandboxes`

`catalogs`, `adapters`, `toolsets`, and `sandboxes` list installed plugin entry
points and their `built-in` or `external` source. `tools` instead lists the leaf
tools assembled from installed toolsets. Catalog names identify integrations;
`toolang models` and `toolang providers` own the merged catalog and provider
availability views.


## Agent HTTP API

Each running agent exposes one local FastAPI server.

The process keeps one `AgentCore`, `CapsManager`, and `JobsManager` for the
application lifetime. `AgentCore` owns the process-local executor, history,
thread manager, setup watcher, and state watcher. These owners are stored on
`app.state` and exposed through small typed request dependencies. The API also
owns one process-local `LiveEventRelay` for live SSE subscribers.
Application-wide FastAPI dependencies are reserved for side-effect-only
concerns such as authentication or common validation. FastAPI lifespan owns
required startup and shutdown; module globals and `ContextVar` do not carry
application state.

`RunExecutor.run()` returns a `LocalRunHandle`; the application retains handles
only when its own protocol needs additional lifecycle bookkeeping.

Core endpoints are grouped as:

- `agent`
- `caps`
- `jobs`
- `runs`
- `threads`

Non-interactive execution uses `POST /api/v1/runs/stream`. It accepts an agic
or flow's unique `runnable` name, primary input, optional model, and optional
declared arguments, and returns the canonical trace event stream for HTTP
clients. An omitted model uses the current `AgentSetup.defaults.model`. CLI
script runs and TUI execution do not consume this endpoint.


## Agent Endpoints

- `GET /healthz`
- `GET /api/v1/profile`
- `GET /api/v1/models`
- `GET /api/v1/agics`
- `GET /api/v1/flows`

`/api/v1/profile` returns:

- profile metadata
- runtime identity:
  - the server process's Toolang source `version`
  - `sandbox.driver`
  - the complete `sandbox.selector`
  - the complete, unprojected `sandbox.instance` for Docker, otherwise `null`
  - the host-plugin-supplied `sandbox.description` for non-Docker runtimes,
    otherwise `null`

- environment summary
- overview metrics:

| Metric Group | Contents |
| --- | --- |
| `threads` | Thread totals grouped by chat, chore, and task |
| `steps` | Step totals grouped by `model_call`, `tool_call`, and `runtime` |
| `tokens` | Aggregated input, output, and total token usage |

`sandbox.description` is optional presentation metadata. Its absence or a `null`
value does not block Chat: host execution falls back to the local host sandbox
plugin description, while Docker continues to use `sandbox.instance`. Runtime
profile readers ignore unknown additive fields, so TUI and executor releases can
be upgraded independently. Breaking protocol changes require a separately
versioned contract rather than making an additive display field mandatory.

`GET /api/v1/models` returns the server's current effective
`AgentSetup.models` collection. Runnable `models` directives are applied when a
run starts, not by this inspection endpoint. The response includes:

- `default`
- `items`
  - `ref`
  - `name`
  - `provider`
  - `parameters.reasoning.effort`

`ref` is the exact catalog route identity, including nested model ids, and does
not include query predicates. Reasoning efforts are distinct recognized catalog
values in catalog order; an unsupported model returns an empty list.

`GET /api/v1/agics` and `GET /api/v1/flows` list the agent's runnable
definitions.


## Cap Endpoints

Summary:

- `GET /api/v1/caps`

Collections:

- `GET /api/v1/psyches`
- `GET /api/v1/skills`
- `GET /api/v1/services`
- `GET /api/v1/prompts`

Detail:

- `GET /api/v1/psyches/{name}`
- `GET /api/v1/skills/{name}`
- `GET /api/v1/services/{name}`
- `GET /api/v1/prompts/{name}`

Templates:

- `GET /api/v1/psyches/templates`
- `GET /api/v1/skills/templates`
- `GET /api/v1/services/templates`
- `GET /api/v1/prompts/templates`
- `GET /api/v1/psyches/templates/{template_name}`
- `GET /api/v1/skills/templates/{template_name}`
- `GET /api/v1/services/templates/{template_name}`
- `GET /api/v1/prompts/templates/{template_name}`

Write:

- `PUT /api/v1/psyches/{name}/authored`
- `PUT /api/v1/skills/{name}/authored`
- `PUT /api/v1/services/{name}/authored`
- `PUT /api/v1/prompts/{name}/authored`
- `DELETE /api/v1/psyches/{name}/authored`
- `DELETE /api/v1/skills/{name}/authored`
- `DELETE /api/v1/services/{name}/authored`
- `DELETE /api/v1/prompts/{name}/authored`
- `PUT /api/v1/psyches/{name}/configured`
- `PUT /api/v1/skills/{name}/configured`
- `PUT /api/v1/services/{name}/configured`
- `PUT /api/v1/prompts/{name}/configured`
- `DELETE /api/v1/psyches/{name}/configured`
- `DELETE /api/v1/skills/{name}/configured`
- `DELETE /api/v1/services/{name}/configured`
- `DELETE /api/v1/prompts/{name}/configured`

Authored write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `content`: raw cap content

Configured write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `ref`: external cap ref

Delete routes accept `scope=home|root` as a query parameter. Cap read
items include:

- `name`
- `description`
- `scope`
- `origin`
- `form`
- `ref`
- `definition_file`
- `line` when known
- `editable`

Read and write payloads use the same `root`, `home`, and `here` scope
vocabulary. Read payloads expose `form`, `scope`, and `origin`; CLI list
commands project those into `SOURCE`, `FORM`, and `SCOPE`.


## Chat Client Orchestration

The HTTP API has no endpoint that accepts terminal `ChatInput` text. A chat
client creates a thread when needed, converts the interaction to the shared
`RunRequest` boundary, and starts each turn through the authored run stream:

1. `POST /api/v1/threads` with the client and optional peer descriptor.
2. `POST /api/v1/runs/authored/stream` with the returned thread id, request id,
   authored input, run/session overrides, and ordered runnable fallbacks.

An existing chat thread can be passed directly to the authored endpoint; the
client does not create another thread for every turn. The server resolves the
request against its current setup and Agent State, including fallback
selection, policy precedence, prompts, named input, and server-relative file
includes. This keeps the Chat TUI and a future WebUI on the same run protocol
without adding a chat-specific server vocabulary.

The separate non-interactive `POST /api/v1/runs/stream` endpoint continues to
accept a selected runnable and canonical percept parts such as:

- `text`
- `image`
- `audio`
- `document`

Actual part support still depends on the selected model route. The built-in
OpenAI Chat Completions and Responses adapters map text, image, audio, and
document inputs. Chat Completions rejects a `DocumentPart` that has only a
document URL; the caller must first provide document data or a provider file
id.

For multipart payload details:

- an image part's `image_url` may be a remote URL or a local `data:` URL
- an audio part's `data` should be base64 payload; `data_url` is also accepted
  as an alias and is normalized to base64
- a document part's `url` is for remote documents
- a document part's `data` carries inline provider-facing document data and may
  be a full `data:...;base64,...` URL
- `file_id` references a document already uploaded to the selected provider

Both run-streaming endpoints return the canonical `RunEvent` SSE protocol. A WebUI
that needs another presentation shape adapts these events client-side; the API
does not maintain a second chat event vocabulary.

The CLI command for interactive chat is `toolang <agent> chat [thread]
[--sandbox <selector>] [--allow COLLECTION=QUERY] [--limit FIELD=VALUE]
[--default FIELD=VALUE]`.
Without a thread id, the TUI creates a terminal chat thread on first input. With
a thread id, it continues that thread. A stopped resident, roaming agent, or
visiting agent uses embedded execution through `LocalRunClient`. A healthy
running resident uses its recorded endpoint through `RemoteRunClient`; an
unready or unhealthy resident fails without starting a competing embedded
executor. Explicit Chat policy options become remotely validated session
commands, while the server keeps ownership of setup, environment, providers,
working directory, and sandbox.

The banner always shows the TUI process version, executor, sandbox, and
host-side agent home in that order. Embedded execution uses
`executor  embedded`. Remote execution links the normalized endpoint and follows
it with the server version when it is not a confirmed clean match, for example
`executor  http://localhost:7001 · v0.3.9`. Docker displays its complete selector
and conventional twelve-character container ID, for example
`sandbox  docker:python:3.13-slim · a1b2c3d4e5f6`. Host execution displays the
ready OS description produced by the host sandbox plugin, for example
`sandbox  host · macOS 27.0 arm64`. The middle-dot separators use the dim style,
and OS build identifiers are omitted. `/api/v1/profile` returns the same
twelve-character Docker hostname used to identify the guest; the host retains
the complete container ID for Docker lifecycle operations.
Job thread ids are inspectable and controllable through thread and run commands,
but `chat` does not implicitly reopen tasks or create manual chore runs.


## Job Endpoints

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/archived`
- `GET /api/v1/jobs/archived/{job_id}`
- `GET /api/v1/tasks`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `PATCH /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/draft`
- `POST /api/v1/tasks/{task_id}/ready`
- `POST /api/v1/tasks/{task_id}/archive`
- `POST /api/v1/tasks/{task_id}/reopen`
- `POST /api/v1/tasks/{task_id}/cancel`
- `GET /api/v1/tasks/archived`
- `GET /api/v1/tasks/archived/{task_id}`
- `PATCH /api/v1/tasks/archived/{task_id}`
- `DELETE /api/v1/tasks/archived/{task_id}`
- `GET /api/v1/chores`
- `POST /api/v1/chores`
- `GET /api/v1/chores/{chore_id}`
- `PATCH /api/v1/chores/{chore_id}`
- `POST /api/v1/chores/{chore_id}/draft`
- `POST /api/v1/chores/{chore_id}/ready`
- `POST /api/v1/chores/{chore_id}/archive`
- `POST /api/v1/chores/{chore_id}/run`
- `POST /api/v1/chores/{chore_id}/cancel`
- `GET /api/v1/chores/archived`
- `GET /api/v1/chores/archived/{chore_id}`
- `PATCH /api/v1/chores/archived/{chore_id}`
- `DELETE /api/v1/chores/archived/{chore_id}`

`GET /api/v1/jobs` returns tasks and chores in one response. Use `kind=task` or
`kind=chore` to filter the unified list. `GET /api/v1/tasks` and
`GET /api/v1/chores` return the same projections split by kind.
The unified `/jobs` collection is read-only; mutations use the concrete
`/tasks` or `/chores` collection selected by the job kind.

List endpoints return authored job fields at the top level and runtime-derived
status under `runtime`.

Task items include:

- `id`
- `kind`
- `stage`
- `status`
- `title`
- `path`
- `updated_at`
- `runtime`

Chore items include:

- `id`
- `kind`
- `stage`
- `status`
- `schedule`
- `title`
- `path`
- `updated_at`
- `runtime`

`stage` values are:

- `ready`
- `draft`
- `archived`

Task status values are:

- `pending`
- `running`
- `done`
- `failed`
- `canceled`

Chore status values are:

- `pending`
- `running`
- `done`

`runtime` contains:

- `thread_id`
- `last_run`
- `next_run_at`
- `error`

`last_run` is the latest run object or `null`. If `last_run.status` is
`running`, that run is the active run. `next_run_at` is the next scheduled
chore timestamp or `null`. `runtime.error` is the current scheduler-side error;
`last_run.error` is the execution failure for that run.

Default job list endpoints return ready jobs. Draft and archived jobs are
available only through explicit `/archived` routes.

Detail endpoints return the same item shape plus `body`.

Collection endpoints return JSON arrays directly, and detail or mutation
endpoints return the projected resource directly. Destructive cap and archived
job deletion endpoints return `204 No Content`.

Task create requests accept:

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks."
}
```

Task patch requests accept any subset of `title` and `body`. Stage actions
use the task `draft`, `ready`, and `archive` endpoints. `task reopen` sets a
completed, failed, or canceled task back to scheduler status `pending`.
Delete is destructive and is available only through archived routes.

Chore create requests accept:

```json
{
  "title": "Check stale PRs",
  "body": "Check stale pull requests and summarize blockers.",
  "schedule": "FREQ=HOURLY;INTERVAL=6"
}
```

Chore patch requests accept any subset of `title`, `body`, and `schedule`.
Stage actions use the chore `draft`, `ready`, and `archive` endpoints.
`chore run` starts one manual occurrence without changing the schedule.
Delete is destructive and is available only through archived routes.


## Run And Thread Endpoints

- `POST /api/v1/runs/stream`
- `POST /api/v1/runs/authored/stream`
- `GET /api/v1/runs/defaults`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/stream`
- `POST /api/v1/runs/{run_id}/retry/stream`
- `POST /api/v1/runs/{run_id}/rerun/stream`
- `POST /api/v1/runs/{run_id}/retry`
- `POST /api/v1/runs/{run_id}/rerun`
- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`
- `POST /api/v1/threads`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `GET /api/v1/threads/{thread_id}/result`
- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`
- `GET /api/v1/threads/{thread_id}/stream`

`/api/v1/runs/{run_id}` is the main trace-detail endpoint.

Run collections return `RunInfo` arrays directly. `RunInfo` combines run
identity, status, input text, output summary, failure, and timestamps; there is
no separate `RunSummary` response type.

`RunDetail.output` contains the canonical message parts resolved from the
run's durable output edge. It is `null` until the run has an output edge and
may be an empty array when the resolved runnable result is empty.

`steer` and `cancel` operate on active (`pending` or `running`) runs. Steer
accepts a user message whose parts may be empty. `retry` and `rerun` accept a
terminal root run. Retry reopens that run from an optional canonical step-path
`anchor`; omitting it selects the latest retryable step. Rerun starts a new root
run from the source invocation and replaces the source in the visible thread
projection. Both accept optional `request_id` and partial `limits`; only rerun
accepts an optional exact `model` replacement, while retry preserves the
persisted model request. Both return `202 Accepted` and execute on the server's
owner event loop.

Thread `rewind` and `fork` request bodies take an optional `run_id` anchor and
`request_id`. An omitted run id selects the last visible run. Task and chore
threads cannot be rewound or forked because their thread ids are derived from
job ids.

`steer` and `cancel` return the accepted `RunControlInfo`. An accepted manual
chore start returns its `RunInfo`. Thread create and fork return the created
thread; rewind returns the updated existing thread representation. None of
these thread operations starts a follow-up run.

`POST /api/v1/runs/stream` accepts:

- `thread_id`: required existing thread id
- `request_id`: required globally unique caller-supplied control identifier
- `runnable.ref`: required concrete agic or flow ref
- `runnable.input`: canonical percept-part array
- `runnable.args`: optional resolved runnable argument mapping
- `model`: a concrete `ModelRequest`, or `null` for a model-free runnable
- `policy`: materialized `allow` ceilings and complete `limits`

HTTP limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`,
and `time`. The caller materializes omitted session values before submission;
an explicit JSON `null` disables that field for the run.

`POST /api/v1/runs/authored/stream` accepts the authored `RunRequest` wire
shape:

```json
{
  "thread_id": "term_example",
  "request_id": "term_request",
  "runnable": {
    "ref": "agic:chat",
    "input": {
      "_": "Summarize\n@notes.md",
      "named": [{"name": "audience", "source": "maintainers"}]
    }
  },
  "model": {
    "ref": "openai/gpt-5",
    "parameters": {"reasoning": {"effort": "high"}}
  },
  "policy": {
    "allow": [],
    "limits": {
      "agic_model_calls": 200,
      "agic_tool_calls": null,
      "tokens": 4000,
      "cost": null,
      "time": null
    }
  }
}
```

The server reads setup and state once and validates the concrete runnable,
model parameters, policy, input, prompts, named sources, and file includes
before accepting the run. Reasoning effort is validated against the selected
catalog route and replaces that target's complete reasoning choice. Named input
names must be unique. Unknown fields and invalid combinations return `422`; a
missing thread returns `404`.

`GET /api/v1/runs/defaults` returns one concrete `model`, `runnable`, and
materialized `policy` for clients to adopt as session-owned defaults. Changing
client session state does not call a validation endpoint; validation occurs on
the next complete run submission.

`GET /api/v1/threads/{thread_id}/result` returns the newest succeeded root
`RunDetail` with a nonempty resolved output. An unknown thread and a known
thread without a result return distinct `404` details.

An accepted response exposes `X-Toolang-Run-ID` and subscribes to the live root
run before execution can publish its first event. CORS exposes the header to
allowed browser origins. The first event is the matching root `run_begin`, the
stream ends at its `run_end`, and disconnecting only removes the subscription;
it does not cancel the run. Clients must not retry an ambiguous start or
reconnect an incomplete stream because events have no replay cursor.

Clients create a thread explicitly with `POST /api/v1/threads` before the first
run. The thread request accepts `web`, `term`, `tui`, `chat`, or `script` as its
client placement; `script` creates a `script_*` thread.

Run and thread streams expose only live events; Toolang does not persist an
exact event log or provide a historical `/events` collection.
`GET /api/v1/runs/{run_id}/stream` accepts only a root run id and carries the
complete recursive run tree. Child runs remain individually inspectable through
their run-detail endpoint. A child-run stream request returns `409` and
identifies the root run to subscribe to.

A reconnecting client establishes and buffers the live stream before reading
run or thread detail from durable records. It then uses the durable detail as
its baseline and applies buffered and subsequent events idempotently. Streams
do not emit SSE ids and ignore `Last-Event-ID`, because the server cannot replay
a precise historical cursor.

Streams use SSE framing directly: the SSE `event` field is the canonical event
type, and `data` is that event's serialized payload. The API does not wrap a
`RunEvent` or `ThreadEvent` in a second transport event type.

Canonical run progress event names are:

- `run_begin`
- `step_begin`
- `part_begin`
- `part_delta`
- `part_end`
- `step_end`
- `run_end`

Every payload retains its canonical `type` discriminator. A `part_begin`
payload uses `part_type` for the message-part kind so it does not collide with
the event discriminator.

Run control acceptance and status are durable `ControlRecord` truth, not
synthetic stream events. A thread stream may additionally carry
`thread_created`, `thread_forked`, and `thread_rewound`, and aggregates live run
events belonging to that thread.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
