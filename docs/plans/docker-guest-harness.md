# Docker Guest Harness

Status: Approved for implementation on 2026-08-29. Work type: feature definition.

This extends `docker-guest-bootstrap.md` and replaces its generated `start.sh`
and `bootstrap.py` artifacts.

## Goal And Interface

Run the production guest setup against one disposable image without an agent or
Toolang configuration:

```console
scripts/try_docker_guest.sh IMAGE [--dev WHEEL_OR_DIST] \
  [--keep] [-- COMMAND...]
```

- The default source is `toolang` from the configured package index.
- `--dev` uses the same wheel resolution as `too --dev`.
- The default command is `too --version`; arguments after `--` replace it.
- `--keep` retains and prints the container and staging paths.

This is a repository experiment utility, not a public command, entry point,
config section, or plugin API.

## Fixed Scripts

- `src/toolang/plugin/sandboxes/docker_guest.sh` is the packaged Linux guest
  core. Toolang `--sandbox docker` stages and executes only this file.
- `scripts/try_docker_guest.sh` is the host experiment wrapper. Product code
  never references it.

The wrapper invokes the same guest core as production and contains no uv,
Python, or Toolang installation logic. Do not retain `start.sh`, `agent.sh`, or
`docker_bootstrap.sh` aliases.

## Guest Core

`docker_guest.sh` requires `uname -s` to report `Linux`; it has no host mode or
other OS branches. Paths, package source, and workload arguments stay as
positional values and are never interpolated into generated shell source.

Acquire uv in this order:

1. reuse a capable `uv` from `PATH`;
2. use Python 3.8+ and pip to install pinned uv into the private runtime and
   invoke it with `python -m uv`;
3. use the pinned official standalone installer; or
4. fail once with an actionable unsupported-image message.

uv then owns supported-Python discovery or installation and the single Toolang
installation path. Do not emulate curl with Python, invoke distro package
managers, or add a separate pip-based Toolang environment.

After Python is available, the shell writes its embedded Python helper into the
private runtime. The helper parses `guest.env` without shell evaluation,
validates Toolang, and finishes with `os.execvpe()`.

## Host Wrapper

The POSIX shell wrapper runs on Linux, macOS, and Windows through WSL2. Native
Windows shells, Git Bash path rewriting, and Windows containers are out of
scope. It:

1. stages environment, diagnostic, and optional wheel files;
2. uses `docker create` with the guest core mounted read-only;
3. retains the full returned container ID for host-side lifecycle operations;
4. lets the guest use Docker's default twelve-character `HOSTNAME` as its
   sandbox instance;
5. runs `docker start --attach` and returns the workload exit code; and
6. stops and removes resources on success, failure, or Ctrl+C unless `--keep`.

Build Docker commands from quoted positional parameters, never `eval`. Do not
forward the full host environment. Because `guest.env` loads after Python
acquisition, the image must provide earlier bootstrap networking itself.

Host lifecycle lines surround but never parse guest output. Active lines end in
an ellipsis, completed lines have no terminal punctuation, non-TTY output is
append-only, and installer details remain private diagnostics.

## Touchpoints And Acceptance

Touchpoints are the two scripts, `_docker_guest.py`, the Docker sandbox staging
call site, focused offline tests, and opt-in Docker checks. No public CLI,
configuration, plugin contract, image build, other engine, or remote progress
change is included.

Acceptance requires:

1. Toolang and the wrapper execute the same packaged guest core.
2. Offline tests cover quoting, Docker commands, exit codes, cleanup, Ctrl+C,
   and `--keep` without network or Docker access.
3. Opt-in Docker checks cover preinstalled uv, `python:3.13-slim` through pip,
   and a no-Python image through the official installer.
4. Unsupported images fail once with a concise reason and diagnostic path.
5. `--dev`, custom commands, and paths containing spaces work on Linux, macOS,
   and WSL2 hosts.
6. Default verification remains offline and passes.

The one-argument form tests the published package; `--dev` tests current source.
