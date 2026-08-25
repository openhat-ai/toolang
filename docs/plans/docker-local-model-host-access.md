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
- Plugins receive one generic host-gateway marker and own any Docker-specific
  configuration, defaults, and endpoint selection.
- Public plugin configuration uses the singular snake-case plugin family as its
  top-level key, matching the Python entry-point group suffix exactly.
- Every configured plugin receives its complete resolved plugin-owned mapping
  through the same factory contract in host and Docker execution.
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
- Docker launch preflight, host-route arguments, cross-platform bind mounts,
  diagnostics, and acceptance coverage;
- canonical plugin configuration roots, compatibility parsing, and generic
  preservation and factory delivery of configured plugin-owned values;
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
security boundary. The Docker sandbox does not translate provider endpoints or
interpret plugin configuration. Any plugin may use or ignore the marker.

## Plugin-Owned Docker Behavior

Each plugin owns its Docker behavior. Toolang core and the Docker sandbox only
establish the route and add `TOOLANG_HOST_GATEWAY`; they do not define a shared
model-location schema, rewrite URLs, or add Docker branches to executors or
protocol adapters.

Plugin configuration may contain an optional plugin-owned `docker` table. The
built-in local model catalogs support:

```toml
[model_catalog.ollama.docker]
endpoint = "http://host.docker.internal:11434"

[model_catalog.llama_cpp.docker]
endpoint = "http://host.docker.internal:8080/v1"
```

These values are examples, not required common-case configuration. The catalog
plugin ignores its `docker` table when `TOOLANG_HOST_GATEWAY` is absent. When
the marker is present, each built-in catalog resolves its endpoint in this
order:

1. its configured `docker.endpoint`, used exactly;
2. its configured top-level catalog `endpoint`, used exactly;
3. a non-loopback `OLLAMA_HOST` or `LLAMA_CPP_HOST`, used exactly;
4. a loopback, localhost, or wildcard provider environment value with only its
   hostname replaced by `TOOLANG_HOST_GATEWAY`;
5. its Docker built-in default.

Without the marker, each plugin keeps its existing host resolution order:
top-level catalog endpoint, provider environment value, then host built-in
default. An exact `[models.providers.<name>].endpoint` remains the final call
override in the provider resolver and is never rewritten.

The two plugin-owned built-in defaults are:

| Plugin | Host default | Docker guest default |
| --- | --- | --- |
| Ollama | `http://127.0.0.1:11434` | `http://host.docker.internal:11434` |
| llama.cpp | `http://127.0.0.1:8080/v1` | `http://host.docker.internal:8080/v1` |

The Docker endpoint is resolved inside each AgentServer process and is never
written back to authored configuration or shared process state. Host and Docker
AgentServers can therefore run sequentially or concurrently from the same
Toolang root. Generic Chat Completions behavior remains unchanged.

## Generic Plugin Configuration Contract

The canonical family identifier is singular `snake_case`. The Python entry-point
group is `toolang.<family>`, and the authored plugin configuration root is
`[<family>]`. The entry-point name is the stable key below that root. Product
model configuration remains under `[models]`: `[models.providers]`,
`[models.aliases]`, and `models.default` are not plugin factory configuration
and must not be renamed or passed to plugins.

| Plugin family | Entry-point group | Canonical authored configuration |
| --- | --- | --- |
| Tool | `toolang.tool` | `[tool.<entry-point-name>]` |
| Channel | `toolang.channel` | `[channel.<binding>]`, with `plugin` selecting the entry point |
| Sandbox | `toolang.sandbox` | `[sandbox]` selects `driver` and `target`; `[sandbox.<entry-point-name>]` configures the selected plugin |
| Model catalog | `toolang.model_catalog` | `[model_catalog.<entry-point-name>]` |
| Model adapter | `toolang.model_adapter` | `[model_adapter.<entry-point-name>]` |

Plugin configuration delivery is a family-independent rule. A family loader
may consume only its declared core binding keys, such as `plugin`, `enabled`,
`driver`, or `target`. After root and agent configuration layers are merged and
declared environment references are resolved, every remaining plugin-owned
value is passed to the selected entry-point factory as one fresh mapping:

```text
factory(dict(resolved_plugin_config))
```

Nested mappings such as `docker` are preserved; generic loaders do not inspect,
flatten, rename, or discard their fields. No configured mapping becomes `{}`
merely because the plugin is built in. With no authored configuration, the
factory receives an empty mapping. Built-in and external entry points follow
the same rule.

The rule applies every time a plugin factory is invoked, not only at initial
launch. Status, running, stop, release, reload, and recovery paths must resolve
the current authored configuration before re-instantiating a plugin. Do not
persist the resolved mapping in hosting state because it may contain secrets;
persisted references remain sufficient to identify the plugin instance, while
the current configuration is reloaded at the host call site.

The current authored locations map to factories as follows:

| Plugin family | Authored configuration delivered to the factory |
| --- | --- |
| Tool | All values in `[tool.<entry-point-name>]` |
| Channel | `[channel.<binding>]` minus the core `plugin` selector |
| Sandbox | `[sandbox.<selected-driver>]`; `[sandbox].driver` and `target` remain core binding values |
| Model catalog | `[model_catalog.<entry-point-name>]` minus core `enabled` |
| Model adapter | All values in `[model_adapter.<entry-point-name>]` |

Every family therefore has an authored configuration surface, including model
adapters. An installed, unconfigured plugin still receives an empty mapping.
Family-owned runtime inputs may be added after the authored mapping, but must
not remove unrelated plugin-owned keys. For example, the setup watcher may add
the resolved environment view to Ollama while preserving its `docker` mapping.

For one compatibility cycle, Toolang also accepts the existing `[tools]`,
`[channels]`, `[models.catalogs]`, and `[sandbox.config]` locations and emits one
actionable deprecation warning for each legacy root used. Normalize each config
layer before applying the existing root-then-agent precedence. If one layer
defines the same plugin or binding through both its canonical and legacy
location, reject it as ambiguous instead of silently choosing one. Documentation
and generated examples use only the canonical names.

Docker guests load the mounted root and agent `config.toml` files through the
same setup path as a host AgentServer. The Docker sandbox transports only the
host-gateway marker; it does not serialize a second plugin configuration or
special-case built-in plugins.

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

The common path requires no new CLI flag or configuration. A plugin-specific
`docker` table handles Docker-only overrides, while existing exact provider or
catalog endpoints continue to handle endpoints shared by host and guest.

Docker command absence, an unreadable active context, a remote endpoint,
unsupported server version or server OS, rejected host-gateway mapping, mount
sharing denial, and container startup failure are launch errors that retain the
underlying Docker detail. An unavailable local model keeps the catalog offline
and adds its selected concrete endpoint to provider diagnostics.
Selecting a model absent from the guest catalog fails normally rather than
falling back to a remote model.

## Naming Review

The existing entry-point groups already follow the canonical rule and remain
unchanged: `toolang.tool`, `toolang.channel`, `toolang.sandbox`,
`toolang.model_catalog`, and `toolang.model_adapter`. Entry-point names also use
stable snake-case identifiers and remain unchanged.

Factory callable names are not plugin identities: an entry point references the
callable explicitly, so the public contract is its one-mapping signature and
returned protocol, not a mechanically derived Python function name. Built-in
factories may retain precise names such as `create_tool_set`, `create_channel`,
`create_hosting`, `create_ollama_catalog`, and `create_model_adapter`. Plugin
documentation must name factories that actually exist and must not imply that
external packages need an identical function name.

Several Python implementation names are internally inconsistent but do not
affect configuration or entry-point identity: `ModelsDevModels`, `OllamaModels`,
and `LlamaCppModels` implement `ModelCatalog`; tool-set implementations use a
generic `Plugin` suffix; `create_channel_plugin` loads rather than creates; and
sandbox implementations use the internal `Hosting` vocabulary. Renaming these
symbols is a behavior-preserving refactor with a wider import surface and is not
part of this feature. The implementation must not add further variants, and a
separate refactor may normalize them to role-bearing names such as
`OllamaModelCatalog`, `FilesystemToolSet`, and `load_channel`.

## Design Touchpoints

- `src/toolang/plugin/sandboxes/docker.py`: inspect the effective local Docker
  environment, resolve concrete launch arguments, add the host marker, serialize
  cross-platform mounts, and write LF scripts.
- `src/toolang/plugin/loading.py`, `src/toolang/plugin/config.py`, and
  `docs/plugins.md`: normalize canonical family configuration roots, provide
  one-cycle legacy parsing, and enforce complete plugin configuration delivery
  after family-owned binding keys are consumed.
- `src/toolang/up/hosting.py` and `src/toolang/up/process.py`: stop
  re-instantiating configured sandbox plugins with an unconditional empty
  mapping during lifecycle inspection and recovery.
- `src/toolang/plugin/models/local.py`: let the Ollama and llama.cpp catalog
  plugins parse their own optional `docker` table and select their host or guest
  default from the marker.
- `src/toolang/plugin/models/config.py`,
  `src/toolang/plugin/models/loading.py`, and `src/toolang/setup/watcher.py`:
  consume `[model_catalog]` and `[model_adapter]`, preserve nested mappings, and
  deliver the resolved per-entry-point config to both built-in and external
  factories.
- `src/toolang/plugin/models/views.py` and `docs/models.md`: report and document
  the concrete endpoint, plugin-owned Docker override, Linux listener
  requirement, and exact endpoint behavior.
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
6. Generic loader tests cover canonical `[tool]`, `[channel]`, `[sandbox]`,
   `[model_catalog]`, and `[model_adapter]` roots; prove that root/agent
   configuration is merged, declared environment references are resolved,
   nested plugin-owned mappings survive, and the corresponding built-in or
   external factory receives a fresh complete mapping. An unconfigured plugin
   receives `{}`.
7. Legacy `[tools]`, `[channels]`, `[models.catalogs]`, and `[sandbox.config]`
   inputs work for one compatibility cycle with actionable warnings. A
   same-layer canonical/legacy collision for one plugin or binding fails as
   ambiguous.
8. Sandbox launch, running, status, stop, release, and recovery instantiate the
   selected plugin with current resolved configuration. Hosting state contains
   no plugin configuration or resolved secrets.
9. A Docker AgentServer reloads the mounted catalog configuration and passes the
   same plugin-owned `docker` mapping to the Ollama or llama.cpp factory; the
   sandbox does not copy or reinterpret that mapping.
10. Each local catalog ignores its Docker config without the marker and follows
   the documented Docker precedence with the marker. Docker, catalog, and
   provider exact endpoints remain byte-for-byte unchanged.
11. Loopback, localhost, and wildcard provider environment values preserve all
   URL components except the translated hostname. Non-loopback values remain
   unchanged.
12. Ollama discovery, Ollama calls, llama.cpp discovery, and llama.cpp calls use
   the same resolved endpoint. Generic protocol adapters contain no Docker
   branch.
13. Windows host mount sources retain drive letters and backslashes while all
   guest paths are POSIX. Sources containing spaces remain one CLI argument,
   and staged scripts contain LF without CRLF.
14. Linux loopback-only provider failure reports the concrete guest endpoint and
   the listener-bind remedy without changing the host process.
15. An opt-in offline Docker integration test starts deterministic fake Ollama
    and llama.cpp endpoints on the host, starts a sandbox AgentServer, discovers
    and calls each endpoint, and cleans up the container.
16. Opt-in `live_provider` checks discover and call one real Ollama and one real
    llama.cpp model without hard-coded model IDs.
17. Manual platform checks cover macOS Docker Desktop or OrbStack, Windows
    Docker Desktop in Linux-container mode, and native Linux Docker before
    implementation is declared supported. Colima is claimed only after the same
    check passes.
18. The default verification suite passes without Docker or a local model
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
- Renaming public configuration roots can confuse existing users; the bounded
  compatibility parser, warnings, conflict error, and canonical-only docs make
  the migration explicit without keeping aliases indefinitely.

## Open Questions

None.
