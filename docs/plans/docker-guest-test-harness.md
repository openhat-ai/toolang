# Docker Guest Test Harness

Status: Proposed.

## Work Type

Feature definition for a host-side test harness that runs the production
Docker guest bootstrap against an arbitrary image.

This plan extends `docker-guest-bootstrap.md`. It does not add a public Toolang
command or a second bootstrap implementation. It supersedes that plan's
host-generated `start.sh` and `bootstrap.py` artifacts with the two shell
boundaries defined below.

## Goal And Success Criteria

Allow a developer to supply one Docker image, start a disposable container,
install the runtime requirements and Toolang, and observe the complete guest
bootstrap directly:

```console
scripts/test_docker_guest.sh python:3.13-slim
```

The change succeeds when the command needs no agent or Toolang configuration,
uses exactly the guest bootstrap staged by `DockerSandbox`, streams ordinary
container output, returns the workload exit code, and cleans up predictably on
success, failure, and interruption.

## Interface

Add this repository developer command:

```console
scripts/test_docker_guest.sh IMAGE [--dev WHEEL_OR_DIST] \
  [--keep] [-- COMMAND...]
```

- `IMAGE` is the only required argument.
- Without `--dev`, install `toolang` from the configured package index.
- `--dev` uses the same deterministic wheel resolution as `too --dev`.
- The default workload is `too --version`.
- Arguments after `--` replace the default workload.
- `--keep` retains the container and staging directory and prints their paths;
  otherwise both are removed.

This is a repository script, not a `too` subcommand, package entry point,
configuration section, or supported plugin API.

## Design

Use two POSIX shell scripts with one-way ownership:

- `src/toolang/plugin/sandboxes/docker_guest.sh` is the packaged guest core. It
  owns all uv, Python, environment loading, Toolang installation, validation,
  progress, and final process replacement.
- `scripts/test_docker_guest.sh` is a thin repository test wrapper. It owns only
  host-side argument parsing, staging, Docker lifecycle, attachment, and
  cleanup.

`DockerSandbox.prepare()` stages and invokes the packaged guest core. The host
wrapper mounts and invokes that exact same file; it must not contain package
installation commands or maintain a simplified test-only guest flow. The guest
core never starts Docker and has no host mode.

These names and dependencies are canonical: Toolang `--sandbox docker`
references only `docker_guest.sh`; product code never imports, locates, or
executes `test_docker_guest.sh`. Do not retain `start.sh`, `agent.sh`, or
`docker_bootstrap.sh` aliases.

The guest core supports Linux containers only. It begins by requiring
`uname -s` to report `Linux` and otherwise exits with an unsupported-guest
message. Keep Linux paths, signals, executable permissions, and `/bin/sh`
behavior unconditional; do not add macOS, native Windows, or Windows-container
branches inside the guest core.

The guest core embeds the fixed Python bootstrap source in a single-quoted
heredoc and writes it into the container-local runtime only after Python is
available. That Python program retains the restricted `guest.env` parser,
Toolang install validation, and final `os.execvpe()`. Dynamic paths, package
source, and workload arguments are passed as positional values; they are never
interpolated into generated shell source.

The guest capability order is:

1. reuse a capable `uv` from `PATH`;
2. when Python 3.8+ and `python -m pip` are available, install the pinned uv
   package into the container-local runtime and invoke it with `python -m uv`;
3. otherwise run the pinned official uv standalone installer when its
   prerequisites are available; and
4. stop with one actionable unsupported-image error when no path works.

After uv is available, use it for supported-Python discovery or installation
and for the one Toolang installation path. Do not add a parallel pip-based
Toolang environment, emulate `curl` with Python, detect distributions, or call
guest package managers.

The host wrapper uses POSIX shell built-ins, ordinary Unix utilities, and Docker
CLI commands:

1. validate Docker availability and stage the production guest artifacts in a
   host temporary directory;
2. `docker create` the requested image with the stage mounted read-only and a
   container-local writable runtime;
3. write the full returned container ID to the staged instance file;
4. `docker start --attach` so bootstrap and workload output remain one ordered
   stream; and
5. propagate the container exit code, then remove or retain resources according
   to `--keep`.

Build Docker arguments with quoted positional parameters and never use `eval`
or a command string. Ctrl+C stops the container before cleanup. On failure,
print the existing private diagnostic path before cleanup; `--keep` makes the
container available for inspection. Do not forward the full host environment.
Explicit Docker control values and the staged guest environment are the only
inputs. As in production, values loaded from `guest.env` become available only
after Python acquisition; the image must make earlier bootstrap networking
available itself.

Run the host wrapper with POSIX `sh` on Linux and macOS, and inside WSL2 on
Windows. Native PowerShell, cmd.exe, Git Bash path rewriting, and Windows
containers are out of scope. Docker Desktop or another compatible Docker CLI
owns host-path translation; the guest core always runs in the Linux container.

## Output

Host lifecycle messages surround, but never parse or rewrite, guest output:

```text
Creating container...
Created container · 3f7c9a12b840
Using Python · 3.13.7
Installing uv...
Installed uv · 0.12.7
Installing Toolang...
Installed Toolang · 0.3.0
toolang 0.3.0
Removing container...
Removed container
```

The existing progress vocabulary applies: active lines use an ellipsis,
completed lines have no terminal punctuation, and non-TTY output is append-only.
Installer details remain in the private diagnostic log.

## Scope And Touchpoints

In scope:

- the packaged guest core
  `src/toolang/plugin/sandboxes/docker_guest.sh`;
- the thin host wrapper `scripts/test_docker_guest.sh`;
- shared environment, diagnostic, instance, and wheel staging in
  `src/toolang/plugin/sandboxes/_docker_guest.py`;
- the Docker sandbox call site where staging is currently assembled;
- focused offline unit tests and opt-in real-Docker checks; and
- the Docker bootstrap plan and developer invocation documentation.

Out of scope:

- a public CLI or configuration shape;
- non-Docker engines, native Windows shells, or Windows containers;
- building or publishing an image;
- package-manager installation inside the guest;
- a second progress transport; and
- keeping containers by default.

## Acceptance Tests

1. Offline shell tests verify each script's argument boundary, quoted argument
   handling, Docker command construction, exact exit-code propagation, and
   cleanup without contacting Docker or a package index.
2. The host wrapper and `DockerSandbox` execute the same packaged guest core.
3. An opt-in Docker test runs the harness with the current development wheel on
   `python:3.13-slim`, covering the Python/pip-to-uv path.
4. An opt-in Docker test covers a no-Python image whose official uv installer
   path is available.
5. A capable preinstalled uv is reused without pip or installer download.
6. An unsupported image exits once with a concise reason and diagnostic path.
7. Success, guest failure, Docker failure, Ctrl+C, and `--keep` have no
   unexpected container or staging leaks.
8. Paths and arguments containing spaces are passed without shell evaluation on
   Linux, macOS, and Windows-through-WSL2 hosts.
9. Default verification remains offline and passes.

## Risks And Open Questions

The one-argument package-index mode validates the published release, while
`--dev` validates current source; documentation must distinguish them. Real
Docker checks remain opt-in because they require network access and images.

There are no open product questions.
