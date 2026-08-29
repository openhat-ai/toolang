# Docker Bootstrap Harness

Status: Proposed.

## Work Type

Feature definition for a host-side developer harness that runs the production
Docker guest bootstrap against an arbitrary image.

This plan extends `docker-guest-bootstrap.md`. It does not add a public Toolang
command or a second bootstrap implementation.

## Goal And Success Criteria

Allow a developer to supply one Docker image, start a disposable container,
install the runtime requirements and Toolang, and observe the complete guest
bootstrap directly:

```console
uv run python scripts/docker_bootstrap.py python:3.13-slim
```

The change succeeds when the command needs no agent or Toolang configuration,
uses exactly the guest bootstrap staged by `DockerSandbox`, streams ordinary
container output, returns the workload exit code, and cleans up predictably on
success, failure, and interruption.

## Interface

Add this repository developer command:

```console
uv run python scripts/docker_bootstrap.py IMAGE [--dev WHEEL_OR_DIST] \
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

Keep one guest implementation. Extract a small staging function from the
existing Docker guest helpers; both `DockerSandbox.prepare()` and the host
harness call it to generate `start.sh`, `bootstrap.py`, `guest.env`, the
instance file, and any staged wheel. The harness must not reproduce bootstrap
commands or maintain a simplified test-only shell program.

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

The host harness uses only Python's standard library and Docker CLI commands:

1. validate Docker availability and stage the production guest artifacts in a
   host temporary directory;
2. `docker create` the requested image with the stage mounted read-only and a
   container-local writable runtime;
3. write the full returned container ID to the staged instance file;
4. `docker start --attach` so bootstrap and workload output remain one ordered
   stream; and
5. propagate the container exit code, then remove or retain resources according
   to `--keep`.

Use argument arrays rather than shell-built Docker commands. Ctrl+C stops the
container before cleanup. On failure, print the existing private diagnostic
path before cleanup; `--keep` makes the container available for inspection.
Do not forward the full host environment. Explicit Docker control values and
the staged guest environment are the only inputs. As in production, values
loaded from `guest.env` become available only after Python acquisition; the
image must make earlier bootstrap networking available itself.

The host implementation is identical on Linux, macOS, and Windows. Docker
Desktop or another compatible Docker CLI owns host-path translation; all
bootstrap shell behavior remains inside the Linux guest.

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

- `scripts/docker_bootstrap.py`;
- shared staging and pip-to-uv acquisition in
  `src/toolang/plugin/sandboxes/_docker_guest.py`;
- the Docker sandbox call site where staging is currently assembled;
- focused offline unit tests and opt-in real-Docker checks; and
- the Docker bootstrap plan and developer invocation documentation.

Out of scope:

- a public CLI or configuration shape;
- non-Docker engines or Windows containers;
- building or publishing an image;
- package-manager installation inside the guest;
- a second progress transport; and
- keeping containers by default.

## Acceptance Tests

1. Offline tests verify argument handling, Docker command construction, exact
   exit-code propagation, and cleanup without contacting Docker or a package
   index.
2. The harness and `DockerSandbox` stage the same bootstrap artifacts for the
   same concrete inputs.
3. An opt-in Docker test runs the harness with the current development wheel on
   `python:3.13-slim`, covering the Python/pip-to-uv path.
4. An opt-in Docker test covers a no-Python image whose official uv installer
   path is available.
5. A capable preinstalled uv is reused without pip or installer download.
6. An unsupported image exits once with a concise reason and diagnostic path.
7. Success, guest failure, Docker failure, Ctrl+C, and `--keep` have no
   unexpected container or staging leaks.
8. Paths and arguments containing spaces are passed without shell evaluation on
   Linux, macOS, and Windows hosts.
9. Default verification remains offline and passes.

## Risks And Open Questions

The one-argument package-index mode validates the published release, while
`--dev` validates current source; documentation must distinguish them. Real
Docker checks remain opt-in because they require network access and images.

There are no open product questions.
