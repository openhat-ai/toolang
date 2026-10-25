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
- [Foreground invocations](../../src/toolang/up/invocation.py) claim script HOMEs
  and bind controller shutdown to the owned launch. The CLI supplies readiness
  and interruption events; invocation code publishes the fenced closing state.
- [Closing deadlines](../../src/toolang/up/closing.py) bound process finalization
  independently of the event loop, without interpreting HOME or launch records.
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
`start`, `chat`, Script `run`, and `rerun`. Without `--dev`, Docker installs
`toolang==<controller distribution version>`. Bare `--dev` uses `.` (the working directory,
not the script directory or agent home). An explicit `PATH` selects that path.
`PATH` is either one Toolang `.whl` file or a directory to search recursively
for Toolang wheels. Directory selection uses the most recent file modification
time and breaks equal-time ties by absolute path. The selected concrete wheel
is staged into Docker and supplies its `too _serve` command. During argument
resolution, its embedded identity must match the current `too --version`, including
revision and dirty marker. A different or unknown identity requires a trailing
`!` on the file or directory path, such as `--dev 'dist!'`. This check runs once.
Build a current wheel and select it with:

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
workload. Guests receive only explicit agent-file bindings or the picked
variable; host-only `keep` values are not staged. Docker accepts every forwarded
name by default. An explicitly configured
legacy `environment_allow_pattern` can further narrow process names using a
full-match regular expression:

```toml
[plugin.sandbox.docker]
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'
```

Docker always writes resolved guest values to mode-`0600` `guest.env`, including
an empty result. It mounts that file read-only at the ordinary loader's
highest-priority root policy path: `ROOT/.<name>.env` for residents, or the
shared [source-identity filename](setup.md#environments) for roaming/visiting
agents. Managed Docker startup also receives the bootstrap copy's explicit path
through hidden `--environment-file` and requires that launch snapshot before
plugin construction; missing or invalid inputs fail. Both paths use the common
dotenv parser. File presence disables fallback and `pick` for ordinary guest
commands. The complete home mount retains any authored `HOME/.env` without masking
or modification; the loader does not merge it with the selected root file. The
controller does not separately reload authored dotenv files for staging. Public
sandbox requests retain the trusted `dotenv_envs` input, whose names bypass the
optional pattern; resolved process bindings win on duplicate names in the generated file.
Generated names use the common dotenv validator before container creation.

The guest preserves all native image/entrypoint environment variables, overlays
the loaded bindings, and restores the finite launcher-owned control values.
Host `keep` never removes image variables or automatically imports host
`PATH`/`HOME`. Toolang prepends its private bin to the selected guest PATH.
Host serving preserves the selected PATH, including an absent or empty value.
Shell tools use `/bin/sh -c`, preserving that environment without login profiles.
Setup refreshes, model calls, tools, and child processes reuse the startup
snapshot; file edits take effect after restart.
Managed host children use the controller's reduced exec environment without
rereading authored dotenv files. A standalone `_serve` invocation still applies
the host selection rules.

Bootstrap installs in a child process using private `UV_CACHE_DIR`,
`UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`,
and `UV_NO_PROGRESS` settings. Installation uses image-provided network settings.
The final workload starts from the original image/entrypoint environment, so
installer UV overrides, temporary module `PYTHONPATH`, and shell variable
assignments do not replace workload values. Internal executable paths are
absolute. Bootstrap does not parse authored or generated dotenv files.

Toolang passes gateway, mapped Toolang-root, sandbox, and workspace control
values through Docker's environment arguments; `_serve` receives the mapped
root/place paths and original source identity explicitly. Docker maps
`host.docker.internal` through the engine's `host-gateway`. Bootstrap inputs
are read-only in the guest. Staged files are removed on release and after a
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
the readiness check. Foreground Docker uses attached stdin without a TTY as
its owner channel, retaining separate output/diagnostics. Background `start` creates the
host `agent_log` with mode `0600` and writes Docker launch diagnostics,
bootstrap errors, and AgentServer output there. Early container diagnostics are
copied to that log before a failed or stopped workload is released, bounded to
the final 2000 Docker log lines and streamed without buffering them in memory.
Diagnostic write failures do not prevent container cleanup. Foreground
interruption, ready-reporting errors, and wait failures stop and release the
workload; cleanup failures preserve `SandboxState` for a later forced stop.

New Docker containers use `toolang-<root_code>-<agent_name>[-<place_code>]`.
Using the notation from [agent addresses](agent-addresses.md):

| Placement | Container name |
| --- | --- |
| Resident | `toolang-<R8>-<name>` |
| Visiting | `toolang-<R8>-<name>-<P10>` |
| Roaming | `toolang-<R8>-<name>-<P19>` |

Resident names omit the place code; visiting and roaming names include its full
code. The name is stable across launches of the same home. Docker naming is
centralized in the adapter using the explicitly bound agent address and canonical
Toolang root; intent and preparation share that helper, and later references
carry the name in `runtime_name`.

Labels `io.toolang.root`, `io.toolang.address`, and `io.toolang.launch-id` bind each
container to its canonical root, original address, and runtime generation.
Expected values live in `SandboxRef.meta.docker_identity` (version 1). The adapter
verifies ownership before using new references and resolves names to immutable
container IDs for subsequent operations. Batched liveness also checks ownership.
Conflicts, missing labels, malformed metadata, and uncertain inspections never
authorize cleanup; pending creation keeps its recovery reference. Existing
references without `docker_identity` retain their original locators and behavior;
existing containers keep their names until normal stop/release and a new launch.

`SandboxState` reads the `control` section of authoritative HOME runtime status.
Host and guest merge owned fields with generation checks; that cooperative
contract does not isolate control fields from arbitrary guest writes.
Legacy `.sandbox/<agent>/state.json` and `.runtime/sandbox.json`
block launch, stop, and removal: stop old workloads with the previous version
before upgrading. Older status formats are rejected without migration; preserve
required history separately before removing obsolete runtime metadata.
Likewise, bootstrap inputs without a matching control reference are preserved
and block relaunch or removal until any associated workload is removed and the
staging directory is cleaned manually.

## Docker filesystem

[Layout](layout.md#agent-placement) defines host and guest ROOT, PLACE, and HOME.
Let `R` be host ROOT, `P` host PLACE, `H` host HOME, `G` guest ROOT = PLACE, and
`A` the complete address. `G` defaults to `/root/.toolang`; guest HOME is always
`G/agents/NAME`, including roaming without a source-directory grant.

| Host source | Guest target | Access |
| --- | --- | --- |
| `H/` | `G/agents/NAME/` | Read-write; linked authored inputs receive read-only overlays |
| `P/.state/` and cap directories | Same relative paths below `G/` | Read-write |
| Each granted workspace directory | `G/workspaces/NAME/WORKSPACE/` | Read-write |
| `R/hub/` | `G/hub/` | Read-only |
| `R/.runtime/active/A/docker/L/active/` | `G/.runtime/active/NAME/` | Read-only |
| `R/.runtime/active/A/docker/L/config.toml` | `G/config.toml` | Read-only |

Lab is an ordinary separate mount from `P/workspaces/NAME/lab`. All workspace
names are unique before mounting; nested and alias sources retain distinct named
targets. Authored-input overlays cannot make a workspace grant read-only.

The generated configuration projects effective workspace paths into the guest
ROOT table, overriding paths in the whole-mounted HOME configuration. Host files
remain unchanged. [Workspaces](workspaces.md) owns selection and precedence.
Config and active inputs have disjoint mount sources; mounting one cannot expose
the other. The guest reads its own info view and writes HOME status.

A selected catalog keeps its existing mapped scope and filename. Root/place
`catalog.json` maps to `G/catalog.json`; a HOME catalog remains below guest HOME.
Reuse a covering mount when available. An uncovered external catalog receives a
read-only mount at `G/.inputs/models/<path-hash>/<filename>`.

Assembly is split between [mount preparation](../../src/toolang/up/mounts.py),
the [launch boundary](../../src/toolang/up/sandbox.py), and the
[Docker adapter](../../src/toolang/plugin/sandboxes/docker/sandbox.py).

`L` is the launch ID's SHA-256 digest. Each launch gets fresh immutable host
staging paths while the address keeps one exclusive preparation slot. Delayed
release cannot remove a replacement launch's inputs.

## Guest bootstrap

Docker uses the engine's default missing-image pull behavior. Its guest script
quietly bootstraps uv and installs the selected package with `uv tool install`.
The self-contained `guest.sh` reuses a capable image uv, installs pinned uv
through Python 3.8+ with pip into a private directory, or downloads the pinned
installer through curl/wget. uv reuses Python 3.11+ or installs managed Python
3.13, then installs Toolang in a private tool environment without replacing
image packages. Bootstrap variables and uv directory overrides stay in a
subshell; the parent retains the image environment and directly `exec`s the
workload with its original arguments, container identity, and private bin path.
Workload output is opened once before diagnostics are retired. Its opened file
is marked `0600` and remains valid across log rotation.
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

[Layout](layout.md#runtime-state-and-discovery) defines format-6 info records and
their host/guest path views. Canonical identity and Docker ownership labels retain
host bindings. HOME status remains authoritative.

Root guests create RunStore databases with the mounted runtime directory's owner;
SQLite's WAL/SHM files inherit that owner. Host inspection and subsequent HOME
reuse retain access to the private execution records.
New State cache directories, locks, revision trees and current pointers likewise
retain their mounted parent's owner. Ownership is set on newly staged revisions
before publication; existing source/workspace trees are not recursively changed.

The registry guard serializes one address; the status guard serializes one home.
After resource release and generation checks, bootstrap inputs and the empty
address directory are removed. Legacy nested and flat format-5 references remain
readable during upgrade without moving their resources.

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

Publication shares a two-second contention budget across the registry and status
guards, releasing all acquired guards before retrying. Query repair is nonblocking.
Liveness probes happen before mutation guards. Mutation order is
registry guard, then status guard: repair compares the full probed entry and
reference under both, marks a dead active workload stopped, and withdraws its
reference. Deferred repairs, including entries beyond the repair budget, recheck
the probed entry before reporting departure. A changed entry is observed once
more; missing status or further instability stays unknown. A terminal launch
cannot bind a resource or return to an active phase; a new execution requires a
new launch ID.

Host workloads perform the ordered terminal update and withdrawal on exit.
New guests update only home status; the next verified host observation retires
their registration. Bootstrap ownership metadata stays until resource release.
Host lifecycle management owns release. Other host CLI processes
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

## Foreground ownership and finalization

Scripts and foreground `serve --sandbox docker` use an attached `docker run -i --init`
with no TTY. Docker's init owns PID 1 and reaps children, leaving the workload
terminable by its independent guest watchdog when it cannot handle signals.
Only the host frontend holds the dedicated stdin writer; terminal
input and tool subprocesses use separate descriptors. The guest watches EOF from
bootstrap through shutdown, independently of model/tool execution. HTTP/SSE loss
alone does not end ownership; EOF does. Background lifetimes are unchanged. This
channel covers local Docker Engine/Desktop; remote-daemon blackhole detection is
not provided.

Closing stops admission, records the first shutdown reason, cancels and drains
unfinished roots/descendants/spawns/tools, then closes writers and storage.
Completed results remain intact. Normal order is persisted result, guest cleanup,
container exit, host registration/staging release, then CLI return. Ctrl-C returns
130 after cleanup; owner loss records `foreground owner channel closed` in the
guest. The host owns global active/staging records; the guest writes only workload
status and execution data.

One ten-second closing budget covers cancellation and cleanup; repeated signals
do not reset it. The guest enforces the deadline even if the frontend/Docker client
is gone. Forced exit or storage failure may leave incomplete records and is not
reported as successful finalization. Recovery verifies process creation time and
container identity/labels before releasing resources; unknown ownership blocks
HOME reuse. Cleanup preserves HOME/history/lab and cannot release a newer launch.

## Server lifetime and client leases

Terminal Chat shares a compatible AgentServer for the same agent, Toolang root,
and source identity, retaining its startup settings. Resident implicit servers
remain running; visiting implicit servers exit once no client leases or accepted
work remain.
Fresh implicit servers enable chat only. Fresh start/serve keep placement
defaults: resident chat,chore,talk; visiting chat,talk. Roaming has no Chat.
Standalone scripts exclusively own embedded host execution or a foreground guest
server until final cleanup. Reusing a HOME starts a new launch with current inputs;
it does not attach to an existing script owner.

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
