# Docker Guest Bootstrap

Status: Approved for implementation on 2026-08-29.

## Work Type

Feature definition for a minimal Docker guest contract, explicit Toolang
installation, and one continuous output stream from bootstrap through the
AgentServer.

This definition supersedes the Docker guest progress-file transport and the
preinstalled-Python assumption in `sandbox-startup-ux.md`. It preserves that
plan's package-source, runtime-identity, readiness, and cleanup decisions.

## Verified Current Behavior

- Docker starts `/bin/sh start.sh`, but the script exits unless `python` or
  `python3` already exists.
- Python loads generated `guest.env` without shell expansion, then executes a
  second generated shell program, `agent.sh`.
- `agent.sh` obtains uv, explicitly installs Toolang, validates `too serve`,
  and replaces itself with the installed command. Bootstrap behavior is split
  across three programs.
- uv fallback depends on the preinstalled Python or downloads an unpinned
  installer through `curl`; it does not use uv's managed Python support.
- Guest progress is written to a mounted token file and parsed into host-side
  `ProgressEvent` values, separately from container output.
- Foreground output follows Docker logs, while `start.sh` redirects background
  output to `agent.log`.

## Goal And Success Criteria

Make POSIX `/bin/sh` the only preinstalled runtime required by the default
bootstrap. Obtain uv and Python when necessary, load guest environment data
without shell evaluation, explicitly install Toolang, then directly replace
bootstrap with the installed `too` process.

The change succeeds when:

- a shell-only image can start with either compatible uv or a supported
  downloader and network access;
- existing compatible uv and Python 3.11+ installations are reused;
- uv installs the tested Python version when no supported Python exists;
- Toolang is installed in an inspectable, container-local environment;
- `guest.env` values survive exactly and are never evaluated by a shell;
- bootstrap and AgentServer output form one ordered, concise stream;
- no guest status file or serialized progress transport remains;
- foreground output stays attached, background startup output remains visible
  through readiness, and later daemon output remains durable;
- failures preserve private diagnostics without exposing environment values;
  and
- the default offline verification passes.

## Scope

In scope:

- generated Docker bootstrap scripts and runtime layout;
- uv acquisition, supported-Python resolution, and Toolang installation;
- restricted generated-dotenv loading and private diagnostics;
- Docker output attachment around API readiness;
- removal of the guest progress file and observer;
- focused tests and current behavior documentation.

Out of scope:

- distro package managers such as apt, apk, dnf, or yum;
- scratch, distroless, native Windows guest, or Windows-container support;
- offline images without preinstalled uv, Python, and Toolang;
- changing `--dev` wheel resolution or package-index selection;
- arbitrary dotenv syntax, shell sourcing, or a JSON environment side channel;
- a general remote progress protocol or Agent API progress endpoint;
- broader HostSandbox output changes;
- building or publishing a Toolang-specific image.

## Guest Contract

Support Linux images with POSIX `/bin/sh` and a writable, executable temporary
filesystem. Before Python exists, one of these paths must work:

1. a compatible `uv` is already on `PATH`; or
2. `curl` or `wget`, CA certificates, DNS, and network access can fetch the
   pinned uv installer.

Do not detect distributions or invoke their package managers. Unsupported
architecture, libc, certificate, network, writable-directory, or executable-
filesystem failures stop with an actionable reason.

The controller retains the existing Linux, macOS, and Windows-through-WSL2
boundary. All bootstrap shell behavior runs in the Linux guest, so it requires
no controller-OS branches.

Use image-provided `TOOLANG_GUEST_RUNTIME`, otherwise
`${TMPDIR:-/tmp}/toolang-runtime`. Give uv explicit install, managed-Python,
tool, and tool-bin directories beneath it so behavior does not depend on
`HOME`. This state is container-local and must not enter host-mounted source or
agent paths.

Accept an existing uv through capability probes for Python discovery/install
and tool install, not version-string parsing. Otherwise install the
repository's pinned, tested uv release. Reuse Python 3.11+ found by uv; if none
exists, install the tested Python 3.13 release and resolve its executable
through uv.

## Bootstrap Flow

Generate only `start.sh`, `bootstrap.py`, `guest.env`, and launch metadata.
Remove `agent.sh`.

`start.sh` performs only work needed before Python exists:

1. validate and create the guest runtime layout;
2. find or obtain uv;
3. find or obtain supported Python; and
4. `exec` Python with `bootstrap.py` and shell-quoted positional arguments.

`bootstrap.py` then:

1. loads `guest.env` with the restricted generated-dotenv parser;
2. validates Docker's default twelve-character `HOSTNAME` and exposes it as
   `TOOLANG_SANDBOX_INSTANCE`;
3. installs the staged wheel or `toolang` requirement using
   `uv tool install --force` into the fixed tool directory;
4. verifies the installed executable provides hidden `too serve`; and
5. calls `os.execvpe()` on the installed executable and server arguments.

The process chain is `docker -> /bin/sh -> Python -> too`; bootstrap processes
are replaced, not retained as supervisors. A user entering the container can
inspect the runtime directory, installed executable, and uv tool environment.

## Environment Boundary

Keep `guest.env` as a mode-`0600` generated data file with root/agent dotenv
values followed by filtered host-process values. Preserve existing precedence.
The writer and generated parser share one restricted round-trip contract for
dollar signs, backticks, `${...}`, quotes, backslashes, carriage returns,
newlines, empty values, and non-ASCII text.

Neither script uses `source`, `eval`, value-based command substitution, or an
intermediate JSON representation. Because Python loads dotenv data, uv/Python
acquisition cannot use values present only in `guest.env`. Restricted images
must provide bootstrap networking through image environment or preinstall uv
and Python; do not broaden Docker's host-environment exposure.

## Output And Attachment

`ProgressEvent` and `ProgressSink` remain process-local. Sandbox child output
is ordinary stdout/stderr and is never parsed into progress events. Remove the
guest token file, observer, and `progress` parameter from `Sandbox.attach()`.

Bootstrap emits natural progress on stderr. Active operations end in an
ellipsis; completed operations have no trailing punctuation; a middle dot
separates a bounded fact:

```text
Installing uv...
Installed uv · 0.x.y
Installing Python...
Installed Python · 3.13.x
Installing Toolang...
Installed Toolang · 0.x.y
Starting agent...
```

Use `Using uv · VERSION` and `Using Python · VERSION` for reused dependencies.
Do not use a spinner, repeat the agent name, print secrets, or expose installer
progress bars. Send raw installer output to a per-launch, mode-`0600`
diagnostic file in the mounted agent runtime. Failures print a concise reason
and the surviving local diagnostic path.

`start.sh` does not redirect the server stream. Docker owns attachment for the
whole container process:

- foreground `run` follows Docker logs until server exit or Ctrl+C;
- background `start` follows the same logs through API readiness, then detaches
  while Docker continues retaining daemon output;
- failure or release copies bounded Docker diagnostics to the private agent log
  before removing the container.

Add symmetric `Sandbox.detach()`. Host implements it as a no-op. Docker uses it
only to stop the controller-side log follower; it never stops the workload.
`release()` is the attachment-cleanup backstop. Controller live progress ends
before child output attaches, and readiness probing remains silent.

## Failure And Cleanup

Keep missing shell capabilities, download failure, unsupported platform,
unusable runtime path, Python failure, Toolang install failure, compatibility
failure, server early exit, and readiness timeout as distinct reasons. Preserve
the `--dev dist` remedy only for package-source or compatibility failures.

On success, normal release removes staging and per-launch bootstrap artifacts.
On failed startup, preserve diagnostics before container and staging cleanup.
Interruption retains existing stop/release and recovery guarantees. Commands,
progress, and diagnostics must never contain guest environment values.

## Design Touchpoints

- `src/toolang/plugin/sandboxes/_docker_guest.py`
- `src/toolang/plugin/sandboxes/docker.py`
- `src/toolang/plugin/sandboxes/_docker_cli.py`
- `src/toolang/plugin/sandboxes/host.py`
- `src/toolang/base/protocols/sandbox.py`
- `src/toolang/up/sandbox.py`
- focused sandbox, lifecycle, runtime-command, and presentation tests
- `README.md`, `docs/concepts.md`, and `docs/api.md`

No configuration shape, entry point, persisted state, Agent API schema,
model/plugin contract, or `pyproject.toml` change is expected.

## Acceptance Tests

1. Generated scripts reuse capable uv and Python without downloading.
2. Fake downloader and uv executables cover the no-Python path without network.
3. Missing bootstrap capabilities fail once with an actionable reason and
   private diagnostic path.
4. Generated dotenv values round-trip exactly without shell expansion.
5. Package-index and staged-wheel sources explicitly install Toolang, then exec
   the installed `too` path rather than `uv tool run`.
6. Generated artifacts contain no `agent.sh`, progress token file, or JSON
   environment side channel.
7. Successful output contains only ordered natural progress and AgentServer
   output, without installer chatter or progress bars.
8. Foreground Docker follows the full stream; Ctrl+C stops and releases the
   workload and follower.
9. Background Docker follows startup through readiness, detaches without
   stopping the server, and preserves later daemon diagnostics.
10. Attachment, early-exit, failure, interruption, recovery, and release races
    leave no orphan follower, container, status file, or staging directory when
    cleanup succeeds.
11. Default tests remain offline; an opt-in Docker check covers development
    wheel success, background detach, foreground interrupt, and cleanup.
12. The complete project verification passes.

## Risks And Open Questions

Minimal images may lack CA certificates or use a read-only/noexec temporary
filesystem. Treat these as explicit unsupported-image failures rather than
adding distro-specific branches. Docker log retention continues to depend on
the configured logging driver; preserve diagnostics before removal.

There are no open product questions.
