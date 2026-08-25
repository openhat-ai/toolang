# Define Docker Sandbox Access to Host Model Runtimes

## Status

Proposed; awaiting human approval.

## Goal

Let a non-container Toolang process running on a host start a Linux Docker guest
that can use Ollama and llama.cpp services on that same host, with useful
defaults on macOS, Windows, and Linux and without introducing a general
execution-topology system.

## Success Criteria

- A host `too run` or `too start` with `--sandbox docker` preflights the active
  local Docker daemon and produces one concrete guest launch command.
- The guest reaches default host Ollama and llama.cpp endpoints automatically
  on Docker Desktop, OrbStack, and compatible desktop VM runtimes.
- Native Linux Docker uses the standard `host-gateway` mapping and reports an
  actionable diagnostic when a loopback-only model listener is unreachable.
- Native Windows paths and Linux guest paths are serialized independently, and
  Windows container mode fails before an image is pulled.
- Exact provider or catalog endpoints remain authoritative and are never
  rewritten.
- The default suite remains offline and deterministic; Docker and real-provider
  checks are opt-in.

## Verified Baseline

On 2026-08-24, a macOS OrbStack smoke test against the current implementation
proved the following:

- Docker hosting start, health, status, stop, and cleanup succeeded.
- Container requests to `127.0.0.1:11434` and `127.0.0.1:8080` failed because
  loopback referred to the guest.
- `host.docker.internal` reached both host runtimes.
- One Ollama model and one llama.cpp model each completed a Toolang agent run
  from inside the Docker sandbox.
- On 2026-08-25, adding the standard
  `host.docker.internal=host-gateway` mapping on OrbStack still reached both
  loopback host services, so an explicit mapping can normalize the launch
  without overriding OrbStack reachability.

The current default suite mocks Docker lifecycle calls and does not cover this
host-network path.

## Scope

In scope:

- a non-container Toolang process controlling one local Docker daemon;
- Linux guests launched by the built-in Docker sandbox;
- Docker Desktop on macOS and Windows, native Docker Engine on Linux, OrbStack,
  and compatible local Docker API implementations that pass the same preflight;
- Docker launch preflight, host-route arguments, guest endpoint selection,
  cross-platform bind mounts, diagnostics, and acceptance coverage;
- `ollama` and `llama_cpp` local catalog defaults plus their existing exact
  endpoint escape hatches.

Out of scope:

- Toolang invoked inside a user-created container, nested containers,
  Docker-outside-of-Docker, and Docker-in-Docker;
- SSH or TCP remote Docker daemons, Kubernetes, and Compose service discovery;
- Windows containers, Podman-native behavior, Finch, and containerd/nerdctl;
- provisioning, starting, stopping, proxying, or changing the bind address of
  Ollama or llama.cpp;
- host networking mode, because it weakens isolation and conflicts with the
  existing published-port behavior;
- exposing environment topology to executors, tools, channels, or protocol
  adapters.

The controller-host and local-daemon boundary is a product precondition, not a
best-effort inference problem. Unsupported nested or remote setups fail before
guest launch rather than receiving guessed routes.

## Docker Preflight and Launch Resolution

Keep the Docker CLI as the compatibility boundary. It already resolves the
active context, `DOCKER_HOST`, Unix sockets, Windows named pipes, TLS, and
platform-specific authentication. Do not add Docker SDK or Testcontainers as a
production dependency for this feature.

Immediately before `docker run`, the Docker hosting plugin obtains structured
server facts with Docker CLI JSON/Go-template output and resolves one immutable,
plugin-owned launch value:

```text
DockerLaunchEnvironment
  endpoint: local_unix | local_npipe | remote | unknown
  server_os: linux | windows | unknown
  server_version: string
  guest_host: host.docker.internal
```

Resolve the effective endpoint from `DOCKER_HOST` when present, otherwise from
the active Docker context. Unix sockets and Windows named pipes are local. SSH
endpoints are remote. TCP endpoints are unsupported because Toolang cannot
prove that host bind-mount paths and model services refer to the daemon machine.
`OperatingSystem`, `Name`, kernel strings, labels, and context names may be
included in diagnostics, but never select product behavior.

Inspect `OSType` from the server, not the Docker client platform. Require
`linux`; reject `windows` with:

```text
docker sandbox requires a Docker daemon running Linux containers
```

Require Docker Engine 20.10 or newer for the standard `host-gateway` value. Add
the same explicit mapping to every supported local Linux guest, including when
the runtime already supplies the conventional name:

```text
--add-host host.docker.internal=host-gateway
```

This capability normalization avoids a Docker Desktop, OrbStack, Colima,
Rancher Desktop, or native Linux vendor table. A compatible implementation is
claimed supported only after the same deterministic guest reachability check
passes.

The resolved launch environment supplies concrete values to
`docker_run_detached`; that function does not inspect the host, environment,
context, or model configuration. The guest receives:

```text
TOOLANG_HOST_GATEWAY=host.docker.internal
```

This value is non-secret, scoped to the launched process, and describes only
the route established by this Docker launch. Direct host execution does not set
it. It is not persisted in authored configuration or runtime state and is not a
security boundary.

## Local Model Endpoint Selection

Keep Docker knowledge out of the generic Chat Completions adapter. The built-in
Ollama and llama.cpp catalogs own local endpoint selection and receive the
guest route through their existing environment view.

Add one optional shared setting:

```toml
[models.local]
location = "auto" # auto | agent | host
```

The default is `auto`:

- without `TOOLANG_HOST_GATEWAY`, use agent loopback;
- with `TOOLANG_HOST_GATEWAY`, use that host for local model defaults;
- `agent` always uses loopback, including when a model service intentionally
  runs inside the guest;
- `host` uses `TOOLANG_HOST_GATEWAY` when present and host loopback otherwise.

An exact `[models.providers.<name>].endpoint` or
`[models.catalogs.<name>].endpoint` always wins and is never rewritten. A
non-loopback `OLLAMA_HOST` or `LLAMA_CPP_HOST` remains exact. For an unspecified,
loopback, wildcard, or localhost environment value, retain its scheme, port,
path, query, and fragment while the selected location supplies the hostname.
Default ports remain 11434 for Ollama and 8080 for llama.cpp.

This produces the following defaults without a process-global topology cache:

| Invocation | Marker | `auto` endpoint host |
| --- | --- | --- |
| Host AgentServer | absent | `127.0.0.1` |
| Toolang-managed Docker guest | `host.docker.internal` | `host.docker.internal` |

Host and Docker AgentServers can therefore run sequentially or concurrently
from the same Toolang root without sharing a rewritten endpoint.

## Platform Behavior

### macOS

Docker Desktop, OrbStack, and Colima expose a container-to-host route using
`host.docker.internal`. Toolang supplies the standard explicit mapping as well,
and acceptance checks require a normal host loopback Ollama or llama.cpp
listener to remain reachable. Bind mounts retain native host paths and use
POSIX guest targets.

### Windows

Support a native Windows Toolang process with Docker Desktop running Linux
containers. Docker Desktop supplies `host.docker.internal`, which targets the
Windows host. Windows container mode is rejected during preflight.

Represent Linux guest roots, homes, working directories, commands, and mount
targets as POSIX paths. Keep host mount sources as native Windows paths. Build
guest paths from path components rather than stringified backslashes. Use
Docker `--mount type=bind` arguments so drive-letter colons are unambiguous,
and pass each mount as one subprocess argument so spaces survive. Write the
staged `start.sh` using explicit LF newlines and invoke it through `/bin/sh`.

Docker Desktop through an interactive WSL2 distribution is supported only when
the effective endpoint is the local Desktop integration socket and the mounted
workspace is accepted by Docker Desktop. A separate native Docker daemon inside
WSL is treated as native Linux and reaches services in that WSL distribution,
not an arbitrary Windows process.

### Linux

Native Docker receives the explicit host-gateway mapping. That mapping reaches
a host interface but does not make a service bound only to `127.0.0.1`
reachable. Keep Docker network isolation and require host model services to
listen on a reachable interface, for example:

```text
OLLAMA_HOST=0.0.0.0:11434 ollama serve
llama-server --host 0.0.0.0 --port 8080 ...
```

Provider diagnostics must explain this distinction and include the concrete
guest endpoint. Documentation warns users to limit external access with host
firewall rules. Rootless Docker is best effort; an exact endpoint remains the
escape hatch when RootlessKit cannot expose the selected route.

## Configuration and Errors

The common path requires no new CLI flag or configuration. The shared
`models.local.location` setting handles the only routing choice, and existing
exact provider or catalog endpoints handle custom networks and non-default
addresses.

Docker command absence, an unreadable active context, a remote endpoint,
unsupported server version or server OS, rejected host-gateway mapping, mount
sharing denial, and container startup failure are launch errors that retain the
underlying Docker detail. An unavailable local model keeps the catalog offline
and adds its selected location and concrete endpoint to provider diagnostics.
Selecting a model absent from the guest catalog fails normally rather than
falling back to a remote model.

## Design Touchpoints

- `src/toolang/plugin/sandboxes/docker.py`: inspect the effective local Docker
  environment, resolve concrete launch arguments, add the host marker, serialize
  cross-platform mounts, and write LF scripts.
- `src/toolang/plugin/models/config.py`, `src/toolang/plugin/models/local.py`,
  and `src/toolang/setup/watcher.py`: parse the shared location and resolve one
  concrete discovery/call endpoint inside each AgentServer process.
- `src/toolang/plugin/models/views.py` and `docs/models.md`: report and document
  the concrete endpoint, Linux listener requirement, and exact endpoint escape
  hatch.
- Existing sandbox and local-model unit tests plus one opt-in Docker integration
  module cover the behavior.

No package-neutral topology types, child topology payload, executor context,
tool context, channel context, or persisted environment schema are added.

## Acceptance Tests

1. Docker preflight parses structured server facts, accepts Linux containers,
   and rejects Windows container mode before `docker run`.
2. Local Unix sockets and Windows named pipes are accepted. SSH and TCP
   endpoints fail before bind mounts or model endpoints are translated.
3. Docker Desktop, OrbStack, Colima, native Linux, and compatible local daemons
   add exactly one standard host-gateway mapping; vendor strings do not change
   the launch command.
4. The resolved launch function receives concrete route and mount values and
   performs no environment, context, platform, or vendor inspection.
5. A Docker guest receives `TOOLANG_HOST_GATEWAY=host.docker.internal`; direct
   host execution does not synthesize the marker.
6. `auto`, `agent`, and `host` select the documented endpoint hosts. Exact
   provider/catalog endpoints remain byte-for-byte unchanged, and provider
   environment endpoints preserve all URL components while only eligible host
   names are translated.
7. Ollama discovery, Ollama calls, llama.cpp discovery, and llama.cpp calls use
   the same resolved endpoint. Generic protocol adapters contain no Docker
   branch.
8. Windows host mount sources retain drive letters and backslashes while all
   guest paths are POSIX. Sources containing spaces remain one CLI argument,
   and staged scripts contain LF without CRLF.
9. Linux loopback-only provider failure reports the concrete guest endpoint and
   the listener-bind remedy without changing the host process.
10. An opt-in offline Docker integration test starts deterministic fake Ollama
    and llama.cpp endpoints on the host, starts a sandbox AgentServer, discovers
    and calls each endpoint, and cleans up the container.
11. Opt-in `live_provider` checks discover and call one real Ollama and one real
    llama.cpp model without hard-coded model IDs.
12. Manual platform checks cover macOS Docker Desktop or OrbStack, Windows
    Docker Desktop in Linux-container mode, and native Linux Docker before
    implementation is declared supported. Colima is claimed only after the same
    check passes.
13. The default verification suite passes without Docker or a local model
    runtime and remains offline.

## Risks

- Linux users must expose model listeners beyond loopback; documentation must
  make the firewall consequence explicit.
- Docker-compatible desktop products can implement host routing differently.
  The support claim follows acceptance evidence, while the exact endpoint
  remains the fallback.
- Windows file sharing, firewall, VPN, and endpoint-security policies can block
  otherwise valid Docker commands outside Toolang's control.
- A local socket can be forwarded to another machine. The host-only guarantee
  cannot be cryptographically proven, so explicit SSH/TCP endpoints are rejected
  and unusual forwarded sockets remain unsupported.
- `host-gateway` establishes routing, not provider health; catalog probes remain
  transient and best effort.

## Open Questions

None.
