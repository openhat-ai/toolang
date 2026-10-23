# Hosting

| Command | Behavior |
| --- | --- |
| `too AGENT serve` | Run a resident/visiting server in the foreground. |
| `too AGENT start` | Start a persistent resident/visiting server, promote an automatic one in place, or reuse a compatible persistent one. |
| `too AGENT stop` | Stop the selected resident/visiting generation, including active work, without fetching its source. |

AGENT accepts an agent name, ref, URL, or visiting instance. Source refs/URLs may
prepare the stable visiting agent root; explicit instances reuse their existing home
without fetching. Roaming does not support `serve`, `start`, or `stop`.

`serve` and `start` resolve concrete startup inputs at the control boundary.
Host `serve` executes in the foreground command's process; host
`start` launches a detached child. Docker runs one guest workload and its host
controller. Detached and guest workloads use hidden `too _serve`, receiving
explicit agent paths, placement, place code, source policy, sandbox, location,
launch ID, and required store schema; guests do not recompute host identities. The execution core is shared across sandboxes.

Embedded host scripts expose the full Agent API, including activity and Stats,
through that script's executor and event loop. The caller owns its shutdown.

The CLI preserves the interpreter's process name and command-line arguments.
Terminal conversation titles continue to use OSC independently of process names.

Server discovery uses lightweight references under the Toolang root's
`.runtime/active/`, pointing to the agent's authoritative
`agents/<agent>/.runtime/status.json`. Host references validate PID and creation time;
container references use adapter-owned instance IDs. `info` shows the referenced
PID or instance even during startup and ignores status reports from another
workload. Missing or unverifiable control state produces a diagnostic instead of
adopting a process by name. Stop unregistered legacy servers before upgrading.
Owners withdraw references when their workload ends; queries repair stale
references independently of resource release. Places and history remain available.

Control-side launches report ordered operational work on stderr: preparing the
sandbox, creating the runtime, and connecting to the Agent API. Host foreground
`serve` reports workload lifecycle through its normal logging. Docker adds
Toolang installation and compatibility checks. A TTY uses one transient line
without a spinner and shows elapsed time after one second. A non-TTY writes
each action and outcome as an append-only plain-text line. The stable
`Agent NAME running: ...` (guest foreground) and `Agent NAME started: ...`
result lines are written only after readiness succeeds.

## Execution boundaries

Placements share generation initialization, execution services, API assembly,
and resource-release ordering. Place calculation and source preparation
are separate boundaries; sandbox transport and foreground ownership are execution
modes, not additional placement implementations.

- [Startup](../../src/toolang/cli/common/startup.py) resolves a concrete `LaunchSpec`
  for new launches and only explicit requirements for attachment.
- [Placement policy](../../src/toolang/up/config.py) centralizes source-only executor
  reuse, hosted/client driver defaults, and implicit lifetime. Explicit instance
  and record continuation stay in the selected home in every placement; controls
  attach only to existing owners. [agent addresses](agent-addresses.md#binding-and-execution)
  owns those bindings.
- [Workloads](../../src/toolang/up/workload.py) initialize generation bindings and
  own preparation/finalization. Source-only roaming execution begins with a newly
  allocated identity; bound continuation validates the retained home instead.
  Discovery publication does not own execution state.
- [Application assembly](../../src/toolang/up/application.py) supplies the complete
  Agent API and Stats to both embedded and hosted `AgentCore` instances. Each
  execution host starts/closes those services within its existing resource scope.

## Startup policy

Both commands accept repeatable `--allow KEY=VALUE`,
`--limit KEY=VALUE`, and `--default KEY=VALUE` options. The CLI parses these
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

Attachment retains the running executor's settings when options
are omitted. It checks identity, API compatibility, required drivers, and explicit
requirements without resolving a new launch or preparing/fetching source. Caller
environment and current local startup configuration do not constrain attachment.
Saved layout and source identities must match the selected target, including
attach-only controls; copied runtime status cannot redirect execution to its owner.
Explicit sandbox selectors must match; a driver-only selector accepts its running
concrete instance. Explicit `--enable` must match the bound drivers, while omission
retains them for attachment and `start` promotion.

Other explicit startup settings compare only their supplied fields against saved,
secret-free bindings. Parameter-only model defaults retain the running identity
and omitted parameters; selecting an identity follows the shared
[model replacement rules](models.md#runtime-configuration).
Explicit workspace additions must already be bound to the
same paths; unrelated existing grants need not be repeated. Conflicting or
unverifiable requirements report that stopping and restarting is necessary,
without changing the owner. Chat session defaults, limits, allows, and workdir
remain request/session settings validated by the executor. Attachment preserves
lifetime; controls use saved bindings. Placement defaults and full startup input
precedence apply when creating an executor.

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
bare `--dev` searches the working directory. It is rejected for host execution.
Attachment accepts an explicit wheel only when its path and content match the
running selection. Use `too AGENT stop` to stop a resident/visiting server explicitly.

For execution commands, `--catalog` and `--compact-model` configure new runtimes.
On attachment, explicit values must match saved settings; omitted values retain
the running settings. Stop the existing agent before changing them.

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
too alice start --sandbox docker --dev
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
reference. Each plugin owns its agent root configuration and reports whether
the workload runs on the host or in a guest environment; orchestration does not
interpret plugin-specific path settings.

The CLI resolves the [root/agent environment](setup.md#environments) before
sandbox preparation. The host sandbox passes that concrete mapping to the
workload. Docker accepts every selected name by default. An explicitly configured
legacy `environment_allow_pattern` can further narrow process names using a
full-match regular expression:

```toml
[plugin.sandbox.docker]
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'
```

Docker writes resolved values to a mode-`0600` staged dotenv file, bind-mounted
read-only over `HOME/.env`. The controller does not separately reload authored
dotenv files for staging. Public sandbox requests retain the legacy
`dotenv_envs` input for trusted callers; those explicit names bypass the optional
pattern. The original home `.env` remains hidden behind the nested mount.
Bootstrap reads the generated file without interpolation, adds finite generated
control/bootstrap paths, and starts the workload without unrelated image
variables. Setup receives that final startup snapshot and never reloads staged
or authored dotenv during refresh.
Toolang passes gateway, Toolang-root, sandbox, and workspace control values
through Docker's environment arguments; `TOOLANG_ROOT` retains the selected
Toolang root, and `_serve` receives the mapped place explicitly. Docker maps
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
| `too AGENT serve` | `stderr` |
| `too AGENT start` | `agent_log` under the agent `.runtime` directory |
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
`SandboxState` reads the `control` section of runtime status format 5 `status.json`; host and
guest update that shared file with generation checks and owned-field merges.
Docker mounts the agent home read-write, including status, database sidecars,
and logs; required inputs/workspaces and read-only bootstrap materials are added
separately. Only the instance's `.runtime/active/<address>` directory is mounted read-write at
an explicit guest path, so the guest can atomically unlink its own reference.
Other instances' registrations and guards are outside that mount. Startup
passes the reference name (the complete agent address), canonical host status
path, and launch ID; guests never derive host paths from their mounted layout.
Guest field ownership is cooperative, not filesystem isolation. Independent sandbox state
is removed. Legacy `.sandbox/<agent>/state.json` and `.runtime/sandbox.json`
block launch, stop, and removal: stop old workloads with the previous version
before upgrading. Older status formats are rejected without migration; preserve
required history separately before removing obsolete runtime metadata.
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

## Runtime observation and retirement

`RuntimeWatcher` observes canonical `preparing`, `starting`, `running`, `stopped`,
`failed`, or `unknown` status. Its single-home `runtime_status` capability serves
`info` without scanning peers. Basic status uses process/sandbox identity and
persisted phase; HTTP readiness is an explicit startup check. Endpoint, drivers,
and display identity come from the same launch-bound snapshot. Inspection errors
remain unknown; they are not recorded as workload failures.

`up.discovery` enumerates resident homes and resolves registered identities;
`up.observation.RuntimeProbe` reads and classifies one runtime without mutation.
`up.watcher` owns scope selection and repair policy. `up.instance` owns status
updates and shared-lock recovery; `up.registry` owns ordered publication and
withdrawal. Lifecycle code owns workload exit and sandbox resource release.
Port allocation reads registered endpoints only, reserving declared endpoints
during preparation and startup before an API becomes available. Resident-only
collection filters nonresident references before reading or probing their status.

New publications retain the format-5 payload in a per-address directory:

```text
TOOLANG_ROOT/.runtime/active/<address>/
    register.lock
    <address>.json         # canonical host status path and launch_id
AGENT_HOME/.runtime/
    status.json
    status.json.lock
```

The registry guard serializes only one address; the status guard serializes one
home. Empty per-address directories are removed after host guard release when
possible. Existing flat format-5 references remain readable and repairable by
hosts during an upgrade; new guests receive only their private directory.

Both guards use shared-file claims carrying owner location, PID, process creation
time, and acquisition time. Guest claims additionally retain their exact launch,
canonical control identity, sandbox, and resource reference. Claims remain
recoverable after status/reference removal. Host readers recover dead host PIDs;
a guest namespace is dead only when its sandbox resource is proven absent. Guests
recover only claims from their own launch/resource namespace, never host claims.
Unknown ownership stays busy; time alone never expires a claim. Only hosts recover
legacy empty registry guards through the shared primitive's native reaper lock.

Both guards share one recovery budget during a repair. Either orphaned claim
can spend the refresh's single additional namespace probe; the other guard
reuses that evidence. Terminal reports need no ordinary sandbox liveness probe.
Prepare recovery evidence in registry-then-status order before acquiring either
guard, so recovery progresses even when their orphaned owners differ.

Publication waits at most two seconds for the registry guard. Query repair is
nonblocking. Liveness probes happen before mutation guards. Mutation order is
registry guard, then status guard: repair compares the full probed entry and
reference under both, marks a dead active workload stopped, and withdraws its
reference. Deferred repairs, including entries beyond the repair budget, recheck
the probed entry before reporting departure. A changed entry is observed once
more; missing status or further instability stays unknown. A terminal launch
cannot bind a resource or return to an active phase; a new execution requires a
new launch ID.

The owning workload performs the same ordered terminal update and withdrawal on
exit, including from a sandbox guest. These operations never release sandbox
resources; host lifecycle management owns release. Other host CLI processes
repair records left by interrupted owners. Run history is never rewritten by
runtime observation or retirement.

## Runtime identity and ports

An active `too AGENT info` uses the sandbox reference's structured workload
identity. Host workloads show `PID`; Docker workloads show `Container` with the
generated name and a 12-character hexadecimal ID, while recovery retains the
full immutable container ID. Other sandbox kinds show `Runtime KIND:ID` without
assuming their identifiers can be shortened. References without structured
identity fields display as generic workloads.

Automatic host-facing ports:

| Placement | Selection |
| --- | --- |
| Resident | Last successful available port, otherwise `7002–7999` |
| Visiting | Last successful available port, otherwise OS-assigned |
| Roaming | OS-assigned on each new executor |

Only active global registrations reserve peer ports. Hold host listener sockets
through serving; sandbox adapters publish actual endpoints. Automatic bind
failures retry at most three times, releasing failed resources and renewing
`launch_id` while retaining the place code. Explicit conflicts fail.
Adapter-assigned endpoints supply the effective port used for attachment.

An agent recorded on `7001` receives a new port on its next default start.
Explicit CLI, environment, or home configuration port overrides still take
precedence, including `7001`. Running processes are not relocated.

## Server lifetime and client leases

Terminal Chat shares a compatible AgentServer for the same agent, Toolang root,
and source identity, retaining its startup settings. Resident implicit servers
remain running; visiting implicit servers exit once no client leases or accepted
work remain.
Fresh implicit servers enable chat only. Fresh start/serve keep placement
defaults: resident chat,chore,talk; visiting chat,talk. Source-only roaming Chat
allocates a fresh home; instance/Thread continuation reuses its bound home.
A roaming client stops the server it created on exit; attachment retains the
existing lifetime owner. Standalone scripts use embedded host execution or stop
the guest server they created when the command exits.

Use `too AGENT start` for unattended availability. It promotes an
automatic visiting server in place or reuses a persistent server, reports actual
drivers, and never adds drivers implicitly. To change a running server's drivers
or startup configuration, stop and restart it with the desired options.
`too AGENT stop` explicitly ends the current generation and active work;
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
cannot start a replacement or redirect a control to another invocation.
Fork/rewind `--chat` continues in the edited home for every placement. A committed
edit survives a subsequent Chat failure. Stop/restart legacy owners to establish
verifiable startup contracts and use lifecycle promotion and guarded controls.

Chat checks health/profile before use. It retains session defaults locally and
uses HTTP for runtime/model/runnable inspection, thread creation, and result
reads. Failed cursor recovery falls back to durable detail without repeating
execution. Lease-loss errors include the last accepted Chat run ID when known.
Rerun observation failures report accepted run IDs without
canceling; explicit interruption requests cancellation. Standalone scripts remain
separate: inactive host execution is embedded and script-created guests stop on exit.
