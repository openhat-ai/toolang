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
- Runtime facts distinguish a Toolang-managed Docker child, a host AgentServer,
  and Toolang invoked inside a user-managed container. Executors and plugins
  receive the same immutable, non-secret topology.
- Ollama and llama.cpp choose a useful endpoint from that topology by default.
  Users can select `agent`, `host`, or an exact endpoint without Docker-specific
  configuration.
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
- a shared execution-topology value that separates physical process facts from
  Toolang-managed hosting provenance;
- topology-aware endpoint resolution and simple location configuration for the
  built-in `ollama` and `llama_cpp` providers;
- explicit topology transport from a controlling `too` process to a child
  AgentServer and typed exposure to executors and plugin call contexts;
- native Windows serialization of Linux-container paths, bind mounts, and the
  staged shell script;
- platform-specific documentation, unit coverage, deterministic Docker
  integration coverage, and opt-in real-provider smoke coverage.

Out of scope:

- provisioning, bundling, starting, stopping, or changing the bind address of
  an Ollama or llama.cpp runtime;
- Windows containers, Podman-specific networking, Kubernetes, Compose service
  discovery, remote Docker daemons, and automatic port scanning;
- automatic endpoint routing across nested Docker, Docker-outside-of-Docker, or
  arbitrary user-created container networks;
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

## Execution Environment Topology

Do not infer physical containerization from the selected sandbox. The current
`AgentEnvironment.container = sandbox == docker` rule conflates independent
facts and cannot represent Toolang invoked in a user-created container.

Add package-neutral immutable values under `toolang.base.types.environment`:

```text
ProcessEnvironment
  system, release, machine
  container: yes | no | unknown

HostingEnvironment
  managed: bool
  driver: none | docker | <plugin name>
  sandbox: <complete selector>
  network: same | host_gateway | unknown
  host_gateway: string?

ExecutionEnvironment
  controller: ProcessEnvironment
  runtime: ProcessEnvironment
  hosting: HostingEnvironment
```

`controller` describes the process that selected and launched the hosting
implementation. `runtime` describes the process in which the AgentServer and
executor actually run. `hosting` describes the Toolang-managed boundary between
them. `managed` is false for direct in-process execution and true when hosting
orchestration launched a child, including the `none` hosting plugin.

Detect process containerization independently in both controller and runtime.
On Linux, use well-known container marker files and cgroup evidence; return
`unknown` rather than claiming `no` when inspection is inconclusive. This fact
is diagnostic and routing input only. It must never grant permissions, weaken a
sandbox, or become a security boundary.

Hosting orchestration captures the controller facts and creates a versioned,
non-secret topology payload for the child. The hidden `serve` boundary validates
that payload, recomputes runtime facts locally, and passes one concrete
`ExecutionEnvironment` into `AgentCore`. With no valid payload, direct execution
uses the current process for both controller and runtime and sets
`hosting.managed = false`; it does not trust arbitrary `TOOLANG_SANDBOX` text as
proof of physical placement.

The three primary cases are:

| Case | Controller | Hosting | Runtime | Network | Auto local-model location |
| --- | --- | --- | --- | --- | --- |
| Host `too`, Docker agent | native | managed `docker` | container | `host_gateway` | `host` |
| Host `too`, host agent | native | managed `none` | native | `same` | `agent` |
| User container `too`, same-container agent | container | managed `none` or direct | container | `same` | `agent` |

A user container that starts another Docker container creates an ambiguous
nested topology. Record the facts, but require an exact provider endpoint; do
not guess whether the model runtime is in the controller container, a sibling,
or the Docker daemon host.

Store the environment on each immutable `AgentSetup` and root run. Never cache
it by Toolang root, agent name, or provider, so host and Docker AgentServers may
run sequentially or concurrently without sharing a translated endpoint. Persist
the safe summary in runtime status for `too info` and API inspection, but never
persist authored endpoint rewrites.

Expose the same typed value to runtime consumers:

- the executor runtime context and generated runtime instructions;
- `ToolContext.environment` for tool plugins;
- `ChannelContext.environment` for channel plugins;
- hosting requests for sandbox plugins;
- local model endpoint resolution before a concrete `ModelTarget` is created.

Do not make protocol adapters inspect cgroups, environment variables, sandbox
names, or Docker DNS. Ollama and llama.cpp both use the generic Chat Completions
adapter; their catalog/provider resolver owns local endpoint selection and gives
the adapter one final `ModelTarget.base_url`. Other plugins consume typed facts
from their call context rather than reading an internal topology environment
variable.

## Local Model Routing and Configuration

Add one shared local-model location with a provider-specific override:

```toml
[models.local]
location = "auto" # auto | agent | host

[models.providers.ollama]
location = "host"

[models.providers.llama_cpp]
endpoint = "http://models.example:8080"
```

`TOOLANG_LOCAL_MODEL_LOCATION=auto|agent|host` provides the equivalent root or
agent `.env` default. Provider configuration wins over `[models.local]`, which
wins over the environment value, which defaults to `auto`.

The locations mean:

- `agent`: use loopback in the AgentServer network namespace;
- `host`: use the hosting environment's host gateway when one exists, otherwise
  loopback when the runtime is native;
- `auto`: use `host` only for one Toolang-managed Docker boundary whose
  controller is native, and use `agent` for same-network execution. Ambiguous or
  unknown topologies do not guess a host route.

An exact `[models.providers.<name>].endpoint` or
`[models.catalogs.<name>].endpoint` always wins and is never rewritten. Reject
`endpoint` and `location` in the same provider table. A non-loopback
`OLLAMA_HOST` or `LLAMA_CPP_HOST` remains exact. A loopback or unspecified
environment endpoint supplies its scheme, port, path, query, and fragment while
the selected location supplies the hostname. This preserves common server-side
values such as `OLLAMA_HOST=0.0.0.0:11434` without sending a Docker child back to
its own loopback. Default ports remain 11434 and 8080.

For `host`, use `host.docker.internal` when the runtime is containerized and the
hosting environment supplies that gateway. In a user-created container,
`location = "host"` uses the same conventional name but the user remains
responsible for creating its DNS/host mapping. If no route is available, keep
the provider offline with an actionable diagnostic that names the selected
location and expected hostname.

The child provider view reports its concrete executable endpoint. Host-side
`too models` and `too providers` may therefore show a host loopback endpoint
while a resident Docker AgentServer reports the corresponding gateway endpoint.

## Platform Behavior

### macOS

Docker Desktop and OrbStack route `host.docker.internal` to host services. No
model-runtime bind change is required when the desktop backend can reach a
loopback listener. A managed Docker child therefore selects `host` in `auto`
mode, while a host AgentServer selects `agent`. Retain the existing host-only
publication of the Toolang API.

### Windows

Support Docker Desktop running Linux containers with either a native Windows
Toolang process or Toolang inside WSL2. `host.docker.internal` targets the
Windows host. Toolang inside a user-managed WSL or Linux container records a
containerized controller independently from the selected sandbox. A model
runtime inside a separate WSL distribution must be made reachable from the
Windows host or configured with an exact endpoint; Toolang does not infer WSL
distribution addresses.

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
gateway cannot reach the configured listener. A user-created Linux container
must be started with a usable `host.docker.internal` mapping before
`location = "host"` can reach the machine outside that container.

## Configuration and Errors

Do not add a public CLI or sandbox flag. The shared `models.local.location`
setting handles the common choice for both providers, a provider `location`
overrides one provider, and the existing provider `endpoint` remains the exact
escape hatch for remote, rootless, WSL-specific, nested, and custom-network
cases.

Docker command absence, unsupported server OS, rejected host-gateway mapping,
mount sharing denial, and container startup failure remain launch errors with
the underlying Docker detail. An unavailable Ollama or llama.cpp endpoint keeps
the current offline-catalog behavior plus its selected location and concrete
endpoint in provider diagnostics. Selecting a model absent from the child
catalog fails normally rather than falling back to a remote model.

## Design Touchpoints

- `src/toolang/base/types/environment.py` and `src/toolang/setup/types.py`:
  define process, hosting, and execution topology without conflating sandbox
  choice with physical containerization.
- `src/toolang/up/hosting.py`, `src/toolang/up/server.py`, and
  `src/toolang/up/core.py`: capture controller facts, transport hosting
  provenance, recapture runtime facts, and pass one immutable environment.
- `src/toolang/up/process.py` and `src/toolang/api/routers/agent.py`: persist and
  inspect the safe runtime topology summary.
- `src/toolang/plugin/models/config.py`, `src/toolang/plugin/models/local.py`,
  and `src/toolang/setup/watcher.py`: parse local/provider location and resolve
  one concrete discovery and call endpoint from topology.
- `src/toolang/base/types/tool.py`, `src/toolang/base/types/channel.py`, and
  executor tool-call construction: expose the typed environment to plugins.
- `src/toolang/base/types/hosting.py` and `src/toolang/up/mounts.py`: distinguish
  native host paths from POSIX hosted paths, pass controller facts, and model
  the host mapping.
- `src/toolang/plugin/sandboxes/docker.py`: validate Linux-container mode, add
  the host mapping, serialize cross-platform bind mounts, and write LF scripts.
- `src/toolang/execution/executor/prepare.py`, `docs/api.md`, and
  `docs/models.md`: expose the structured runtime context and document location,
  platform setup, security, and exact endpoint escape hatches.
- Existing hosting, setup, server, and sandbox unit tests plus one opt-in Docker
  integration module cover the behavior.

## Acceptance Tests

1. Process detection does not consult `TOOLANG_SANDBOX`. Tests independently
   cover native, detected container, and inconclusive process environments.
2. Versioned topology transport produces the three primary case rows exactly,
   rejects malformed payloads, recomputes runtime facts, and never treats its
   contents as authorization.
3. Two AgentServers using the same Toolang root, one on the host and one in
   Docker, retain separate immutable environments and local endpoints without
   process-global or persisted rewrite leakage.
4. Executor runtime context, `ToolContext`, `ChannelContext`, runtime status,
   and profile inspection project the same safe controller/runtime/hosting
   facts and contain no raw environment variables.
5. `auto`, `agent`, and `host` produce the documented endpoints for all three
   primary cases. Nested or unknown topology requires an exact endpoint rather
   than guessing.
6. Provider location, shared local location, environment location, and `auto`
   follow their precedence. Exact provider/catalog endpoints are unchanged;
   loopback, unspecified, and non-loopback provider environment values follow
   the documented routing and URL-preservation rules.
7. Ollama and llama.cpp discovery and call targets receive the same concrete
   endpoint. The generic Chat Completions adapter contains no Docker or topology
   branches.
8. Docker launch includes the host-gateway mapping and keeps the Toolang API
   published only on the requested host address.
9. Linux-container paths are POSIX when the host mount source is represented by
   a Windows drive path. Mount sources containing spaces survive as one CLI
   argument, and staged scripts contain LF without CRLF.
10. Windows container mode fails with the specified actionable error before
    `docker run`. Missing Docker and gateway/mount failures retain their current
    wrapped diagnostics.
11. An opt-in, offline Docker integration test starts deterministic host fake
    Ollama and llama.cpp endpoints, starts a sandbox AgentServer, discovers each
    model, completes one call through each adapter, and cleans up the container.
12. Opt-in `live_provider` smoke coverage discovers and calls one real Ollama
    and one real llama.cpp model without hard-coding model IDs.
13. Manual platform checks cover macOS Docker Desktop or OrbStack, Windows
    Docker Desktop from native Windows and WSL2, native Linux Docker, and `too`
    inside a user-created Linux container before implementation is declared
    supported.
14. The default verification suite remains offline and passes without a Docker
    daemon or local model runtime.

## Risks

- Linux users must expose model listeners beyond loopback. Documentation must
  make the firewall consequence explicit.
- Windows file sharing, firewall, VPN, or endpoint-security policy can block a
  valid Docker command outside Toolang's control.
- `host-gateway` establishes reachability but does not prove provider health;
  local catalog probes remain transient and best effort.
- Container detection is necessarily best effort. The explicit location and
  endpoint settings remain authoritative when topology is unknown.
- Adding environment facts to plugin call contexts expands shared protocol
  values; optional defaults and coordinated built-in plugin updates avoid a
  flag-day for external plugins.
- Container and host inspection intentionally report different executable
  endpoints for the same local provider.

## Open Questions

None.
