# Hosting

| Command | Resident name | Local `.too` | Remote Source Ref |
| --- | --- | --- | --- |
| `too serve` | yes | yes | yes |
| `too start` | yes | no | yes |
| `too stop` | yes | no | yes |

Behavior:

| Command | Behavior |
| --- | --- |
| `toolang serve` | Runs a local agent, or fetches one remote agent program into a stable visiting root and runs it in the foreground |
| `toolang clone` | Clones one local agent, or fetches one remote agent program into a new local managed agent |
| `too start` | Starts a persistent resident/visiting server, promotes an automatic one in place, or reuses a compatible persistent one |
| `too stop` | Stops the selected generation, including active work, without fetching its source |

`too serve` and `too start` resolve concrete startup inputs at the control
boundary. Host `serve` executes in the foreground command's process; host
`start` launches a detached child. Docker runs one guest workload and its host
controller. Detached and guest workloads use hidden `too _serve`, receiving
explicit agent paths, input policy, sandbox, location, and launch ID without
placement decisions. The execution core is shared across sandboxes.

The CLI preserves the interpreter's process name and command-line arguments.
Terminal conversation titles continue to use OSC independently of process names.

Server discovery uses lightweight references under the Toolang root's
`.runtime/instances/`, pointing to the agent's authoritative
`agents/<agent>/.runtime/status.json`. Host references validate PID and creation time;
container references use adapter-owned instance IDs. `info` shows the referenced
PID or instance even during startup and ignores status reports from another
workload. Missing or unverifiable control state produces a diagnostic instead of
adopting a process by name. Stop unregistered legacy servers before upgrading.

Control-side launches report ordered operational work on stderr: preparing the
sandbox, creating the runtime, and connecting to the Agent API. Host foreground
`serve` reports workload lifecycle through its normal logging. Docker adds
Toolang installation and compatibility checks. A TTY uses one transient line
without a spinner and shows elapsed time after one second. A non-TTY writes
each action and outcome as an append-only plain-text line. The stable
`Agent NAME running: ...` (guest foreground) and `Agent NAME started: ...`
result lines are written only after readiness succeeds.

## Startup policy

Both commands accept repeatable `--allow RESOURCE=QUERY`,
`--limit LIMIT=VALUE`, and `--default SETTING=VALUE` options. The CLI parses these
with `TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*`, and `TOOLANG_LIMIT_*` into frozen
field overrides passed to `SetupWatcher`.

`--compact-model MODEL` selects the new runtime's compaction model,
using the same model expression as `--model`, without a `model=` prefix.

These are fixed watcher inputs. [Setup](setup.md#policy) owns configuration
sources, field precedence, and generation publication. Startup model defaults
use `--default model=BODY`; Script/rerun `--model BODY` selects one Run, while
Chat initializes its own multi-run session. Default/limit fields occur once;
repeated allow values accumulate within the CLI layer. Driver handler selection
belongs to [Drivers](drivers.md#handler-selection).

## Sandbox selection and development wheels

Sandbox selection uses CLI > current `[sandbox]` binding > last successful
selector > `host`. An unavailable selected sandbox fails without falling back.
Persistent servers reuse their last successful available host-facing port when
no current port input overrides it; a busy remembered port is reallocated, while
an explicit/configured busy port fails. Successful settings are recorded only
after readiness; failed attempts do not replace them. Stopped records retain
these settings and database/log history. Temporary script ports remain temporary.
Docker sandbox control is supported from Linux and macOS hosts. Windows is not
supported yet.

Chat uses the same sandbox selection when no AgentServer is active. See
[server lifetime](#server-lifetime-and-client-leases) for driver defaults,
retention, and command combinations.
If an AgentServer is already running, Chat attaches to it and rejects an explicit
incompatible selector without executing or restarting the server.
`chat --dev [PATH]` installs a local Toolang wheel in a new non-host runtime;
bare `--dev` searches the working directory. It is rejected for host execution
or when Chat attaches to an existing
AgentServer. Use `too stop <agent-or-ref>` to stop a retained server explicitly.

For execution commands, `--catalog` and `--compact-model` apply only when
starting a runtime. Stop the existing agent before changing those startup
settings; an attached command rejects them rather than ignoring them.

Commands that start a new guest accept `--dev [PATH]`. This includes `serve`,
`start`, `chat`, Script `run`, and `rerun`. Omitting `--dev` keeps the
existing package selection. Bare `--dev` uses `.` (the process working directory,
not the script directory or agent home). An explicit `PATH` selects that path.
`PATH` is either one Toolang `.whl` file or a directory to search recursively
for Toolang wheels. Directory selection uses the most recent file modification
time and breaks equal-time ties by absolute path. The selected concrete wheel
is staged into Docker and supplies its `too _serve` command. Build a current
wheel and select it with:

```sh
uv build --wheel
too start alice --sandbox docker --dev
```

A following non-option token is consumed as PATH. Before a runnable selector or
input, use `--dev=.` or terminate options with `--dev --`. For example,
`too demo.too --dev=. summarize hello` and
`too demo.too summarize --dev -- hello` both select from the working directory.
Use `--dev=PATH` or a `./` prefix for paths beginning with `-`. Repeated `--dev`
options use the last value; a runnable-level occurrence overrides the script
root value. Explicit empty paths (`--dev=` or `--dev ""`) also resolve to `.`.

Help displays environment, default, and bare-option metadata as separate tags:
`[env: NAME=] [default: VALUE] [bare: VALUE]`. Only applicable tags are shown.
For `--dev`, `[bare: .]` describes the value used when PATH is omitted; it does
not change the default when the entire option is absent.

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
Toolang passes gateway, Toolang-root, sandbox, and workspace control values
through Docker's environment arguments; `TOOLANG_ROOT` retains the selected
Toolang root, and `_serve` receives the mapped agent root explicitly. Docker maps
`host.docker.internal` through the engine's `host-gateway`. The complete staging
mount is read-only in the guest. Staged files are removed on release and after a
failed Docker launch whose workload was removed successfully. If Docker cleanup
fails, the staged files remain with the persisted recovery reference.

## Runtime recovery and logs

For every sandbox implementation, AgentServer is the environment's primary
foreground workload. `serve` waits for that workload and releases it on exit,
while `start` returns after the health endpoint is ready. `stop` reloads the
persisted `SandboxState`, stops the primary workload, and releases its sandbox
resources. Agent removal also asks the sandbox lifecycle to release any stopped
workload before deleting the agent home; no caller deletes sandbox control state
as ordinary filesystem data.

Agent entrypoints also share one logging policy resolver:

| Entrypoint | Log destination |
| --- | --- |
| `toolang serve` | `stderr` |
| `toolang start` | `agent_log` under the agent `.runtime` directory |
| embedded host `.too` script run | `run_log` under the agent `.runtime` directory when `PY_LOG` is set, otherwise `none` |
| attached or temporary `.too` script run | AgentServer output remains in its `agent_log`; Script progress and result output retain their stderr/stdout split |

The lifecycle saves intent before creation and binds recoverable references as
soon as they are available, then attaches output observers and performs
the readiness check. The Docker sandbox follows container output locally from
container creation for foreground `serve`. Background `start` instead creates the
host `agent_log` with mode `0600` and writes Docker launch diagnostics,
bootstrap errors, and AgentServer output there. Early container diagnostics are
copied to that log before a failed or stopped workload is released, bounded to
the final 2000 Docker log lines and streamed without buffering them in memory.
Diagnostic write failures do not prevent container cleanup. Foreground
interruption, ready-reporting errors, and wait failures stop and release the
workload; cleanup failures preserve `SandboxState` for a later forced stop.
`SandboxState` reads the `control` section of agent instance format 3 `status.json`; host and
guest update that shared file with generation checks and owned-field merges.
Docker mounts the agent home read-write, including status, database sidecars,
and logs; required inputs/workspaces and read-only bootstrap materials are added
separately. Entire roots and discovery references are not mounted. Guest field
ownership is cooperative, not filesystem isolation. Independent sandbox state
is removed. Legacy `.sandbox/<agent>/state.json` and `.runtime/sandbox.json`
block launch, stop, and removal: stop old workloads with the previous version
before upgrading. Stopped legacy status reports migrate; history is preserved.
Likewise, per-launch staging without a matching control reference is preserved
and blocks relaunch or removal until any associated workload is removed and the
staging directory is cleaned manually.

## Guest bootstrap

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

## Runtime identity and ports

An active `toolang info` uses the sandbox reference's structured workload
identity. Host workloads show `PID`; Docker workloads show `Container` with the
generated name and a 12-character hexadecimal ID, while recovery retains the
full immutable container ID. Other sandbox kinds show `Runtime KIND:ID` without
assuming their identifiers can be shortened. Older version-1 references without
identity fields remain readable as generic workloads.

When `toolang start` runs without `--port`, Toolang first tries the agent's last
runtime port, except `7001`, which is reserved for the default Hub. If that port
is not reusable, Toolang scans its auto-assigned local range `7002-7999`, starting
at `7002` and counting upward, skipping ports
already recorded by other local agents, instead of asking the OS for a random
ephemeral port.

An agent recorded on `7001` receives a new port on its next default start.
Explicit CLI, environment, or home configuration port overrides still take
precedence, including `7001`. Running processes are not relocated.

## Server lifetime and client leases

Terminal Chat shares a compatible AgentServer for the same agent, Toolang root,
source identity, and startup settings. Resident implicit servers remain running;
visiting implicit servers exit once no client leases or accepted work remain.
Fresh implicit resident/visiting servers enable chat only. Fresh start/serve
keep placement defaults: resident chat,talk,chore; visiting chat,talk. Roaming
Chat servers remain running after exit. Standalone scripts use embedded host
execution or stop the guest server they created when the command exits.

Use `too start <agent-or-ref>` for unattended availability. It promotes an
automatic visiting server in place or reuses a persistent server, reports actual
drivers, and never adds drivers implicitly. To change a running server's drivers
or startup configuration, stop and restart it with the desired options.
`too stop <agent-or-ref>` explicitly ends the current generation and active work;
clients do not relaunch it. Start cannot detach foreground serve.

The server owns idle exit, with no supervisor process. Automatic clients renew
leases every five seconds; leases expire fifteen seconds after the last renewal.
Once a renewal confirms promotion to persistent, clients stop renewing. Expired
clients stop submitting work, including after suspension or a delayed response.
Accepted pending/running roots, independent spawn roots, owned async children, and terminal
cleanup retain the server even without clients. Client transport loss never
cancels accepted work. Startup has a thirty-second hold, consumed on the first
client registration. Docker removes automatic containers on exit; history and diagnostics
remain on host mounts. Host staging cleanup may wait for a later management call.

Generation checks reject delayed requests to replacement servers. Steer/cancel
attach only to a healthy live owner and pin the target invocation; stale history
cannot start a replacement or redirect a control to another invocation. Fork/rewind --chat
retains one acquisition through both phases. Legacy servers stay unmanaged and
retained; stop/restart them to use lifecycle promotion and guarded controls.

Chat checks health/profile before use. It retains session defaults locally and
uses HTTP for runtime/model/runnable inspection, thread creation, and result
reads. Failed cursor recovery falls back to durable detail without repeating
execution. Lease-loss errors include the last accepted Chat run ID when known.
Rerun observation failures report accepted run IDs without
canceling; explicit interruption requests cancellation. Standalone scripts remain
separate: inactive host execution is embedded and script-created guests stop on exit.
