# Sandbox Startup UX

Status: Approved for implementation on 2026-08-25.

## Work Type

Feature definition for observable agent startup, concise Docker bootstrap
output, actionable launch failures, and sandbox-aware runtime identity in
`too info`.

## Verified Current Behavior

- Runtime commands finish their existing source-resolution progress renderer
  before calling the sandbox lifecycle. `resolve_startup()` accepts a progress
  object but discards it.
- Foreground Docker launch follows the container log stream before readiness,
  so raw `ensurepip`, `pip`, `uv`, and AgentServer output is interleaved without
  a user-visible startup stage.
- Background Docker launch redirects the same output to `agent.log`; `too
  start` remains silent while `launch()` creates the workload and polls the
  health endpoint.
- Docker uses one `uv tool run --from ...` command. Package installation and
  execution therefore have no reliable boundary, and the controller cannot
  distinguish installation, CLI compatibility, and server startup failures.
- A development controller without `--dev` warns about package skew, but an
  older package-index Toolang can still fail later with an unqualified `No such
  command 'serve'` followed by the generic `agent server exited before becoming
  ready` error.
- `SandboxRef.runtime_id` is deliberately a sandbox-owned workload identifier:
  a host PID for the host sandbox and an immutable container ID for Docker.
  `too info` nevertheless labels every active value as `PID` and prints the
  complete Docker ID.

## Goal And Success Criteria

Make agent startup explain its current work without exposing installer noise by
default. Foreground `run` and background `start` use one lifecycle progress
model, failures identify the last reliable stage and retain diagnostics, and
runtime information uses the identity vocabulary supplied by the sandbox.

The change succeeds when:

- a TTY always shows the current startup stage and elapsed time until the agent
  is ready or launch fails;
- non-TTY stderr emits stable, newline-delimited stage transitions without ANSI
  control sequences;
- foreground and background Docker launches report installation, Toolang CLI
  validation, AgentServer start, and readiness as distinct stages;
- default successful output contains no `ensurepip` chatter, pip root warning,
  package download list, or uv progress bars;
- a package-index Toolang without the internal `serve` command fails during the
  validation stage with an explicit `--dev` remedy;
- background failure includes the underlying reason and the existing
  `agent.log` path instead of replacing the reason with the path;
- Ctrl+C during any foreground or background startup stage retains the current
  lifecycle cleanup and recovery guarantees;
- host runtime information is labeled `PID`, Docker runtime information is
  labeled `Container`, and an unknown sandbox kind falls back to `Runtime`;
- Docker control identity remains the full immutable container ID even though
  human output uses the container name and a short ID;
- the default offline verification passes.

## Scope

In scope:

- lifecycle progress events for `run`, `start`, and their supported routed
  forms;
- a dedicated runtime-startup CLI presenter;
- explicit Docker guest bootstrap stages and a quiet default installer;
- an installed-CLI compatibility check before AgentServer execution;
- foreground, background, TTY, non-TTY, failure, and interrupt presentation;
- structured sandbox runtime identity and `too info` rendering;
- focused unit, integration, documentation, and live Docker acceptance checks.

Out of scope:

- adding `--sandbox` to more commands;
- changing sandbox selection, configuration, environment exposure, mounts,
  recovery ownership, or health semantics;
- pinning or automatically building the package-index Toolang version;
- changing AgentServer logs after readiness or adding a general `--verbose`
  flag;
- exposing startup progress through the Agent HTTP API;
- native Windows Docker control. The existing Linux, macOS, and Windows-through-
  WSL2 support boundary remains unchanged.

## User-Facing Startup Model

The ordered stages are:

1. `Preparing sandbox`
2. `Starting workload`
3. `Installing Toolang` when the selected sandbox requires a guest install
4. `Validating Toolang`
5. `Starting agent server`
6. `Waiting for agent API`

Host skips installation and validation. A plugin may skip any stage that does
not apply, but must not invent CLI presentation text; it emits semantic phase,
status, and detail values. The controller owns stages 1, 2, and 6. Docker owns
stages 3 through 5.

For a TTY, a dedicated startup presenter renders one transient line containing
the agent, sandbox, current stage, useful bounded detail, and elapsed time. A
successful foreground launch ends the startup presentation with:

```text
Running agent eve: http://localhost:7001 (Ctrl+C to stop)
```

A successful background launch ends it with:

```text
Started agent eve: http://localhost:7001
```

While either command is waiting, the transient line makes the operation and
its elapsed time visible. It is stopped before the final line. Foreground
AgentServer output retains its existing logging policy and may displace the
transient line during the final server/readiness stages; the presenter must
redraw cleanly without duplicating completed stages.

For non-TTY stderr, print one line when a stage first becomes active and one
terminal failure line. Do not animate, rewrite lines, print successful stage
summaries, or add ANSI. Stdout contracts remain unchanged.

The development-source warning becomes one concise preflight warning:

```text
Warning: Docker will install Toolang from the package index, not local source PATH. Use --dev dist to run this build.
```

Use the selected sandbox name instead of `Docker` for another guest sandbox.
The installation stage identifies `package index` or the selected wheel
filename, so the user can see which Toolang source is being prepared.

## Progress Contract

Continue to use package-neutral `ProgressEvent` and `ProgressSink` values, with
stable `startup.*` phase names. Do not overload the cap/source `CliProgress`
state machine: add a runtime-startup presenter with its own small ordered state
model and remove the unused `startup.*` branch from the existing presenter.

Pass an optional progress sink explicitly through `sandbox.run()` and
`sandbox.launch()`. Lifecycle orchestration emits controller-owned stages
around `prepare()`, workload `launch()`, and `_wait_ready()`. Extend
`Sandbox.attach(plan, ref, *, progress=None)` so Docker can start its guest-stage
observer only after the recovery reference is durable. No other plugin method
needs the sink. Do not store callbacks in `LaunchSpec`, `SandboxRequest`,
`SandboxPlan`, `SandboxRef`, or persisted state.

Each stage uses one stable event ID per launch. Repeated observations update the
same stage rather than creating more rows. Details are bounded display values,
not raw commands or logs. Progress delivery is observational: a missing sink,
renderer failure, or stale guest progress file must not alter launch, readiness,
cleanup, or recovery decisions.

## Docker Guest Stages

Replace the single `uv tool run` operation with two explicit operations:

1. quietly install the selected `toolang` requirement or staged wheel with `uv
   tool install`;
2. execute the installed `too` command as the container's primary workload.

The container remains ephemeral, so this does not create a persistent
environment outside the existing workload. Preserve the configured indexes,
proxies, certificates, and uv/pip environment. Make `ensurepip`, pip fallback,
and uv installation quiet on success, suppress pip's root-user warning inside
this purpose-built container, and preserve their stderr on failure.

After installation, verify that the installed CLI accepts the hidden `too
serve` entrypoint without starting the server. A failed check emits one curated
diagnostic and does not fall through to Typer's generic unknown-command output:

```text
Installed Toolang does not provide the required `too serve` entrypoint. Build a wheel with `uv build --wheel` and pass `--dev dist`.
```

Docker reports guest stages through one plugin-owned, per-launch status file
under the writable agent runtime directory. Its filename includes the launch
ID, and its contents are an append-only sequence of closed phase tokens, one
token per line. This preserves short stages even when the observer attaches or
polls after a transition. The controller watches the matching file after the
durable recovery reference is saved. The file is display-only, is never read as
sandbox control state, cannot mark the workload ready, and is removed during
normal release. Unknown, malformed, duplicate, out-of-order, or stale contents
are ignored. This avoids parsing installer or AgentServer logs and keeps the
existing host-owned `.sandbox/<agent>/state.json` trust boundary.

The closed vocabulary represents running, successful, and failed installation
and validation plus the transition to server execution. The generated guest
script appends a failed token before exiting a failed install or validation
command. The observer maps those tokens to semantic progress events and curated
reasons; it never carries command output or environment values. Server exit and
readiness timeout remain controller observations because the guest command is
replaced by the AgentServer process.

Foreground Docker continues to expose AgentServer output after installation,
but successful installer output is quiet. Background Docker continues to write
complete errors and AgentServer logs to the mode-`0600` `agent.log`; progress is
reported separately on the controlling terminal. No guest phase file is
required for the host sandbox.

## Failure And Interrupt Presentation

On failure, finish the live display and report:

- agent and sandbox;
- the last active startup stage;
- the original lifecycle or curated guest reason;
- `Log: PATH` when a background diagnostic log exists;
- the `--dev` hint only for package-source or compatibility failures.

For example:

```text
Could not start agent eve in docker:python:3.13-slim
Stage: Validating Toolang
Reason: Installed Toolang does not provide the required `too serve` entrypoint.
Hint: Build a wheel with `uv build --wheel` and pass `--dev dist`.
Log: ~/.toolang/agents/eve/.runtime/agent.log
```

Do not print the generic readiness error as a second top-level cause when a
more specific guest failure is available. Timeout remains distinct from early
workload exit. A foreground error that has already been emitted by the workload
is not duplicated verbatim.

Ctrl+C marks the active presentation interrupted, invokes the existing
stop/release path, and exits 130. If cleanup fails, retain the recovery state
and surface that failure as today. `start` must handle interruption during
launch and readiness, not only during source resolution.

## Runtime Identity

Keep `SandboxRef.runtime_id` as the complete immutable control identifier. Add
validated `runtime_kind` and optional `runtime_name` fields to `SandboxRef`:

- host: `runtime_kind="process"`, numeric PID as `runtime_id`, no name;
- Docker: `runtime_kind="container"`, full container ID as `runtime_id`,
  generated container name as `runtime_name`;
- third-party default: `runtime_kind="workload"`, no name.

The fields are data, not preformatted labels. Existing version-1 state without
them remains readable as an unknown workload so active resources can still be
stopped safely. This is an additive state change and does not change the
recovery key. Newly written references include both fields; Docker no longer
needs to duplicate the container name in untyped metadata.

`too info` renders:

- `PID 1234` for a process;
- `Container toolang-eve-abc123 (176191c1528b)` for a named container, using
  the first 12 hexadecimal container-ID characters only in human output;
- `Runtime KIND:ID` for another kind, without assuming its ID can be shortened.

Machine and recovery state retain the full ID. Runtime state written by the
AgentServer may still contain its environment-local PID, but it is not shown as
the controller workload PID when a sandbox reference exists.

## Design Touchpoints

- `src/toolang/common/events.py` and `src/toolang/common/progress.py`
- `src/toolang/base/types/sandbox.py`
- `src/toolang/base/protocols/sandbox.py`
- `src/toolang/up/sandbox.py`
- `src/toolang/up/process.py`
- `src/toolang/plugin/sandboxes/host.py`
- `src/toolang/plugin/sandboxes/docker.py`
- `src/toolang/plugin/sandboxes/_docker_guest.py`
- `src/toolang/cli/common/progress.py`
- a focused runtime-startup presenter under `src/toolang/cli/common/`
- `src/toolang/cli/toolang/commands/runtime.py`
- `src/toolang/cli/toolang/commands/agent.py`
- corresponding unit and CLI integration tests
- `docs/api.md` and sandbox plugin documentation

No configuration shape, entry-point name, or Agent API schema changes are
expected.

## Acceptance Tests

1. Host and Docker lifecycles emit ordered semantic stages; skipped stages do
   not leave pending output.
2. TTY `run` and `start` display the active stage and elapsed time transiently,
   then leave exactly their existing ready line.
3. Non-TTY `run` and `start` emit ordered plain stage transitions on stderr and
   preserve empty stdout until the final command contract writes there.
4. A slow background Docker bootstrap visibly advances through install,
   validation, server start, and readiness while complete details remain in
   `agent.log`.
5. Successful Docker startup contains no pip root warning, package download
   list, or uv progress bar.
6. A missing `too serve` command fails at validation with the `uv build` and
   `--dev dist` remedy; the generic readiness error is not presented as a
   competing cause.
7. Installer, container-launch, server-start, early-exit, and readiness-timeout
   failures preserve their distinct stage, reason, cleanup, and recovery
   behavior.
8. Interrupting `run` or `start` during each observable stage exits 130 and
   leaves no workload, state, staging directory, or guest progress file when
   cleanup succeeds.
9. Malformed and stale guest progress files cannot affect readiness or
   lifecycle control and are removed when referenced by the current launch.
10. `too info` shows a host PID, a named Docker container plus short ID, and a
    generic unknown workload without truncating its opaque ID.
11. Version-1 sandbox state without identity fields still loads, reports a
    generic runtime, and can be stopped and released.
12. Agent-first and command-first routed forms receive identical startup
    presentation, and Linux/macOS plus WSL2-controlled Docker use the same guest
    behavior.
13. A live Docker smoke test covers package-index validation failure, explicit
    wheel success, background progress, foreground Ctrl+C, `too info`, stop,
    and cleanup.

## Risks And Open Questions

`uv tool install` is intentionally different from the previous ephemeral `uv
tool run`, but the Docker container itself is already ephemeral and the
installed command remains the primary workload. The implementation must verify
the supported uv version and quoting for both a registry requirement and a
wheel path.

External writes can interleave with a Rich live display. Installer success
output is therefore quiet, and the presenter must redraw cleanly around the
small amount of foreground AgentServer startup output before stopping at the
final ready or error line. Tests must exercise real terminal and non-terminal
streams.

Guest progress is advisory and potentially writable by code inside the guest.
Keeping its token set closed and excluding it from every control decision
prevents it from weakening the host-owned recovery boundary.

There are no open product questions.
