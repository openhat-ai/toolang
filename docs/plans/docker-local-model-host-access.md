# Define Docker Sandbox Access to Host Model Runtimes

## Status

Proposed; awaiting human approval.

## Goal

Make host Ollama and llama.cpp runtimes usable from an agent started with the
Docker sandbox on supported macOS, Windows, and Linux configurations, without
changing host execution or silently rewriting remote provider endpoints.

## Success Criteria

- `too run` and `too start` with `--sandbox docker` discover and call host
  Ollama and llama.cpp models through a container-reachable endpoint.
- Docker Desktop on macOS and Windows, Docker Engine on Linux, and native
  Windows Toolang path handling have explicit behavior and acceptance coverage.
- Configured, environment, and default loopback endpoints resolve to the Docker
  host only for the child Docker runtime. Non-loopback endpoints remain exact.
- Windows uses Linux containers and emits a clear error in Windows container
  mode.
- The default suite remains offline and deterministic. Docker and real-provider
  smoke tests remain opt-in.

## Verified Baseline

On 2026-08-24, a macOS OrbStack smoke test against the current implementation
proved the following:

- Docker hosting start, health, status, stop, and cleanup succeeded.
- Container requests to `127.0.0.1:11434` and `127.0.0.1:8080` failed because
  loopback referred to the container.
- `host.docker.internal` reached both host runtimes.
- One Ollama model and one llama.cpp model each completed a Toolang agic run
  from inside the Docker sandbox.

The current default suite mocks Docker lifecycle calls and does not cover this
host-network path.

## Scope

In scope:

- host-gateway wiring for the built-in Docker sandbox;
- sandbox-local endpoint resolution for the built-in `ollama` and `llama_cpp`
  providers;
- an internal transport for resolved provider endpoint overrides from hosting
  orchestration to the child AgentServer setup;
- native Windows serialization of Linux-container paths, bind mounts, and the
  staged shell script;
- platform-specific documentation, unit coverage, deterministic Docker
  integration coverage, and opt-in real-provider smoke coverage.

Out of scope:

- running Ollama or llama.cpp inside the Toolang agent container;
- provisioning, starting, stopping, or changing the bind address of a model
  runtime;
- Windows containers, Podman-specific networking, Kubernetes, Compose service
  discovery, remote Docker daemons, and automatic port scanning;
- host networking mode, because it removes network isolation, discards the
  existing published-port behavior, and is not supported uniformly;
- enabling Docker for direct chat or scripts; their current sandbox boundaries
  remain unchanged.

## Supported Runtime Boundary

The Docker sandbox continues to run Linux images. Require Docker Engine 20.10
or newer so `host-gateway` is available. Docker-compatible runtimes may work
when they implement the same CLI and gateway behavior, but only Docker Engine,
Docker Desktop, and the already verified OrbStack path are claimed.

`host.docker.internal` is the one provider hostname inside the sandbox. The
Docker launch adds:

```text
--add-host host.docker.internal:host-gateway
```

Docker Desktop already supplies this name; the explicit mapping makes native
Linux behavior consistent. Do not use `--network host`.

The gateway names the machine running the Docker daemon. Remote daemons remain
unsupported because the current bind mounts also require paths local to that
machine.

## Endpoint Resolution

Retain the current endpoint precedence:

1. `[models.providers.<name>].endpoint`;
2. `[models.catalogs.<name>].endpoint`;
3. `OLLAMA_HOST` or `LLAMA_CPP_HOST`;
4. `http://127.0.0.1:11434` or `http://127.0.0.1:8080`.

After sandbox selection, hosting orchestration resolves the effective endpoint
for `ollama` and `llama_cpp`. For the Docker child only, replace the hostname
with `host.docker.internal` when it is:

- `localhost`;
- an IPv4 or IPv6 loopback address;
- the unspecified bind address `0.0.0.0` or `::`.

Preserve the scheme, port, path, query, and fragment. Leave every other
hostname or address unchanged. The `none` sandbox and host-side inspection keep
their original endpoints.

Pass translated values as internal provider endpoint overrides on the hidden
`serve` command. `AgentCore` passes those concrete values to `SetupWatcher`,
where they take precedence over mounted configuration for both dynamic catalog
discovery and provider target resolution. Do not encode the override as only
`OLLAMA_HOST` or `LLAMA_CPP_HOST`: configured provider endpoints currently have
higher precedence and would bypass such an override. Do not persist the
translated endpoint to authored configuration.

The child provider view reports its executable, translated endpoint. Host-side
`too models` and `too providers` may therefore show a host endpoint while the
resident AgentServer reports the corresponding Docker endpoint.

## Platform Behavior

### macOS

Docker Desktop and OrbStack route `host.docker.internal` to host services. No
model-runtime bind change is required when the desktop backend can reach a
loopback listener. Retain the existing host-only publication of the Toolang API.

### Windows

Support Docker Desktop running Linux containers with either a native Windows
Toolang process or Toolang inside WSL2. `host.docker.internal` targets the
Windows host. A model runtime inside a separate WSL distribution must be made
reachable from the Windows host or configured with an explicit non-loopback
endpoint; Toolang does not infer WSL distribution addresses.

Represent paths inside the Linux container independently from host filesystem
paths. Container roots, homes, work directories, commands, and mount targets
use POSIX path serialization. Host mount sources retain native `Path` behavior.
Translate relative host paths into container paths by joining their individual
parts, not by stringifying Windows separators.

Use Docker `--mount type=bind` arguments so a Windows drive-letter colon is not
ambiguous. Pass each mount as one subprocess argument so spaces survive. Write
the staged `start.sh` with explicit LF newlines; invoking it through `/bin/sh`
does not depend on Windows executable bits.

Before invoking `docker run`, inspect the Docker server OS. Reject Windows
container mode with `docker sandbox requires Linux containers` before pulling
an image. Windows firewall or endpoint-security denial remains an environment
error and must retain Docker's diagnostic detail.

### Linux

Native Docker reaches the gateway through the added host mapping, not through
container loopback. The host model runtime must listen on the Docker bridge or
another reachable interface. Document these provider-side examples:

```text
OLLAMA_HOST=0.0.0.0:11434 ollama serve
llama-server --host 0.0.0.0 --port 8080 ...
```

Warn users to restrict these ports with host firewall rules. Rootless Docker is
best effort; users must set an explicit reachable provider endpoint when its
gateway cannot reach the configured listener.

## Configuration and Errors

Do not add a public CLI flag or a new sandbox configuration key. Existing
provider endpoint configuration is the explicit escape hatch for remote,
rootless, WSL-specific, and custom-network cases.

Docker command absence, unsupported server OS, rejected host-gateway mapping,
mount sharing denial, and container startup failure remain launch errors with
the underlying Docker detail. An unavailable Ollama or llama.cpp endpoint keeps
the current offline-catalog behavior; selecting a model absent from the child
catalog fails normally rather than falling back to a remote model.

## Design Touchpoints

- `src/toolang/up/hosting.py`: resolve sandbox-local provider endpoints after
  the sandbox choice and pass concrete child overrides.
- `src/toolang/up/server.py` and `src/toolang/up/core.py`: carry hidden serve
  overrides into the process-local core.
- `src/toolang/setup/watcher.py` and `src/toolang/plugin/models/config.py`:
  apply endpoint overrides to discovery and resolved provider targets.
- `src/toolang/base/types/hosting.py` and `src/toolang/up/mounts.py`: distinguish
  native host paths from POSIX hosted paths and model the host mapping.
- `src/toolang/plugin/sandboxes/docker.py`: validate Linux-container mode, add
  the host mapping, serialize cross-platform bind mounts, and write LF scripts.
- `docs/api.md` and `docs/models.md`: document platform setup, security, and
  explicit endpoint escape hatches.
- Existing hosting, setup, server, and sandbox unit tests plus one opt-in Docker
  integration module cover the behavior.

## Acceptance Tests

1. Endpoint resolution rewrites default, `localhost`, IPv4 loopback, IPv6
   loopback, and unspecified addresses only for Docker. It preserves URL
   components and exact non-loopback endpoints from both config and environment.
2. The hidden AgentServer override wins over mounted provider configuration and
   drives both catalog discovery and the model call target. The `none` sandbox
   remains unchanged.
3. Docker launch includes the host-gateway mapping and keeps the Toolang API
   published only on the requested host address.
4. Linux-container paths are POSIX when the host mount source is represented by
   a Windows drive path. Mount sources containing spaces survive as one CLI
   argument, and staged scripts contain LF without CRLF.
5. Windows container mode fails with the specified actionable error before
   `docker run`. Missing Docker and gateway/mount failures retain their current
   wrapped diagnostics.
6. An opt-in, offline Docker integration test starts deterministic host fake
   Ollama and llama.cpp endpoints, starts a sandbox AgentServer, discovers each
   model, completes one call through each adapter, and cleans up the container.
7. Opt-in `live_provider` smoke coverage discovers and calls one real Ollama and
   one real llama.cpp model without hard-coding model IDs.
8. Manual platform checks cover macOS Docker Desktop or OrbStack, Windows Docker
   Desktop from native Windows and WSL2, and native Linux Docker before the
   implementation is declared supported.
9. The default verification suite remains offline and passes without a Docker
   daemon or local model runtime.

## Risks

- Linux users must expose model listeners beyond loopback. Documentation must
  make the firewall consequence explicit.
- Windows file sharing, firewall, VPN, or endpoint-security policy can block a
  valid Docker command outside Toolang's control.
- `host-gateway` establishes reachability but does not prove provider health;
  local catalog probes remain transient and best effort.
- Container and host inspection intentionally report different executable
  endpoints for the same local provider.

## Open Questions

None.
