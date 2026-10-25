# Hub Connection Configuration and Local Process State

Status: Proposed; awaiting human approval. Definition only; no implementation.

## Goal and success criteria

Root agents and human tools can use a local, containerized, or remote Hub through
its configured HTTP URL, without reading Hub process files or backend settings.
A successful local stop leaves no running record for the stopped process.

This replaces file-based client discovery in [agent Hub transport](agent-hub-transport.md)
and the corresponding discovery decision in [Hub files](hub-files-directory.md).
The Hub artifact directory remains unchanged.

## Verified baseline

- `teaming/discovery.py` reads `ROOT/hub/hub.json` to construct agent connections;
  human tools instead call `up/hub.py::HubProcess.connection()`. Both obtain the
  endpoint, human, and backend identity from that record.
- Root `[teaming]` currently combines human identity, backend configuration, and
  the Hub port. Client setup constructs `BackendConfig` even though only Hub
  accesses Redis/Valkey. Docker guests mount the host Hub directory read-only.
- `HubProcess.stop()` signals a verified PID/creation-time pair and waits, but
  does not remove the record. `serve()` removes it outside `server.run()`.
  Installed Uvicorn 0.52.1 replays SIGTERM after ASGI shutdown, bypassing that
  outer cleanup. An isolated subprocess using the actual Hub hosting code and
  an offline ASGI app reproduced: stop succeeds, the process is gone, and the
  record still says `running`.
- Managed agent registration requires the Hub's roster scanner to find the
  agent HOME. A remote Hub with no client filesystem cannot satisfy that check.

## Scope

Define client configuration, server-only configuration, identity discovery,
local record cleanup, CLI inspection, Docker client inputs, and remote roster
registration. Preserve message semantics, lease fencing, event recovery, and
independent local execution during Hub outages.

Authentication, authorization, TLS termination, deployment automation,
multi-Hub routing, and backend data migration are outside this definition.
Remote examples assume a reachable, externally managed endpoint.

The requested human removal applies to `hub.json`. Human message participants
and existing human ownership semantics remain supported.

## Configuration and ownership

### Client inputs

`ROOT/config.toml` contains the connection configuration shared by that root's
agents and human tools:

```toml
[teaming.hub]
url = "http://127.0.0.1:7001"
```

Resolve URL as `TOOLANG_HUB_URL` > `[teaming.hub].url` >
`http://127.0.0.1:7001`. A present but empty or invalid override is an error.
Accept absolute HTTP/HTTPS URLs, including an optional proxy path prefix;
normalize trailing slashes and reject userinfo, queries, fragments, wildcard
destination addresses, and invalid ports. Preserve HTTPS certificate checking.

All clients use the same resolver: presence, export, conversation tools and
rules, Talk, and team observation. Home configuration retains only the existing
`[teaming] enabled` opt-out; it cannot override the root's Hub target.

Keep `[teaming].human` as the human CLI's own identity override, defaulting to the
OS username. It is separate from connection resolution. Agents neither resolve
that default nor use a Hub-provided human identity. Human tools obtain their
actor locally, rather than from a process record or handshake.

Client setup never reads `ROOT/hub/`, creates a `BackendConfig`, computes a
backend identity, or opens a backend connection. An absent or invalid server
configuration cannot prevent clients from using a valid remote Hub.

### Server inputs

Move server settings into `ROOT/hub/config.toml`, read only by `hub start/serve`:

```toml
human = "bryan"
host = "127.0.0.1"
port = 7001
roster = "local"

[backend]
url = "redis://localhost:6379/0"
```

Defaults retain the current human OS-username fallback, backend URL, and port;
host defaults to loopback and roster to `local`. The configured human remains
the server-side owner under the existing messaging contract. Human requests
must use that identity; supporting independent human owners is outside scope.

Add `--host` to `hub start/serve`. Resolve bind host as CLI >
`TOOLANG_HUB_HOST` > server configuration > `127.0.0.1`; resolve bind port as
CLI `--port` > `TOOLANG_HUB_PORT` > server configuration > `7001`.
Support explicit IPv4/IPv6 binds and retain exclusive startup and busy-port
failure. Wildcard binds are listener settings, never client URLs.

Binding and dialing are independent. A custom `--port` does not change client
defaults, write client configuration, or publish a discovery fallback. Start
output identifies the local listener and explains the client URL setting for a
nondefault port. Service commands operate on the local root even when its client
URL points elsewhere.

Only Hub constructs backend configuration and derives its namespace from its
own canonical root. Clients receive no backend URL, credentials, root path,
root code, storage keys, or driver configuration.

### Compatibility

Keep `hub start`, `serve`, `stop`, `stop --force`, and the existing port flag.
Replace root `[teaming.backend]` and `[teaming.hub].port` with the two configuration
files above. Reject legacy server fields in client configuration with a specific
move-to-`hub/config.toml` diagnostic; do not silently read them or rewrite files.
`TOOLANG_HUB_PORT` becomes server-only; custom-port clients use a URL.
Keep the human CLI override in root configuration and copy an existing explicit
Hub owner into server configuration so upgrading does not change ownership.

Stop old Hubs before upgrade and restart clients with the new version. New
clients require the new metadata contract and report an incompatible Hub rather
than falling back to old process records. No backend data or namespace migration
is implied by moving configuration.
Local process control may decode the known legacy record shape to stop or
reconcile an old process; discard its human/identity fields. Never rewrite those
fields or use that record for client connections.

## Connection identity and failure handling

Extend `GET /info` to return `{version, api_version, identity}`. `identity` is an
opaque dataset identifier generated by Hub; preserve the existing server-derived
value so unchanged deployments keep cursor and draft identity. Do not expose
`human` or backend configuration in this response.

The initial `/info` request needs no expected identity and performs no storage
mutation or implicit human registration. Keep metadata available if the backend
becomes unavailable after startup. `/healthz` remains the readiness/backend probe.
Validate metadata and `api_version` before any client mutation.

A connected client holds `{endpoint, identity}` and a separately supplied actor.
Every application request and SSE connection sends the pinned identity through
the existing `X-Toolang-Backend` consistency header. Its name remains for wire
compatibility; clients treat its value as opaque. Agent requests omit
`X-Toolang-Human`; human requests may supply their locally selected actor through
the existing consistency header. Neither header is authentication.

Connection establishment is asynchronous and explicit. Resolve configuration
and environment at CLI, setup, and launcher boundaries; pass concrete target
values into transport code. Remove AgentClient's implicit environment/file
discovery. Reuse the resulting connection within an operation or stream rather
than adding an `/info` request to every HTTP request.

Background lifecycles pick up root URL changes on their next connection cycle;
setup refresh invalidates the old connection for new admissions. A server
`hub_changed` response can trigger a fresh handshake for future batches, but
never replay the rejected operation. Pin the old connection for an admitted
Talk batch and all of its tools. Preserve dataset-separated cursors, exporter
recovery, and task/root isolation. A different dataset cannot receive old pending
cursor writes or a pinned batch's side effects. Human Talk views require reopening
after a target or dataset change, retaining existing draft isolation.

Errors name the attempted Hub URL and distinguish invalid configuration,
unreachable service, incompatible metadata, identity change, and backend
unavailability. Remote failures suggest checking the target, without assuming
that starting a local Hub will repair it. Preserve bounded background backoff,
local execution, and uncertain-send behavior; never auto-start Hub, fall back to
Redis, or automatically resend a message.

## Local process record and stop contract

`ROOT/hub/hub.json` becomes a private local control record owned by `up`:

```json
{"pid": 10402, "created": 1792890576.804421, "host": "127.0.0.1", "port": 7001, "status": "running"}
```

Store only PID, creation time, bind host/port, and `starting|running`. Remove
human and backend identity. Keep atomic writes, mode `0600`, the existing
`hub.lock` lifetime lock, and `hub.log`. A record describes a process's last
publication; live PID/creation-time validation remains necessary.

The terminal representation is an absent record, rather than a persisted
`stopped` status. Implement both cleanup paths:

1. Remove the owned record during ASGI lifespan teardown, before Uvicorn replays
   a termination signal. Retain outer cleanup for startup failures.
2. After confirmed process exit, `stop()` removes any surviving record belonging
   to that PID/creation-time pair, including after forced kill. Startup failure
   and timeout cleanup apply the same rule.

Child cleanup compares its process identity while already holding the lifetime
lock. Controller cleanup takes that lock after process exit and compares the
current record before removal. If a replacement owns the lock or record,
preserve its state. Compare process identity rather than readiness status.
A stop timeout without force keeps the record and fails; cleanup failure after
exit reports the failure instead of printing unconditional success.
Success requires observing the old record absent or replaced. If a competing
startup holds the lock while the old record remains, allow up to five seconds
for reconciliation, then report a cleanup diagnostic instead of claiming success.

An uncatchable crash can leave stale state. `hub start/stop` reconcile a parseable
record whose process is confirmed dead, under the same lock. Preserve
the existing not-running result when stop finds no live process. Corrupt or
unverifiable records produce diagnostics without signaling a guessed PID.

Local startup readiness uses its concrete listener/probe address and child
identity. A wildcard listener is probed through the matching loopback address;
this address is never substituted into client configuration.

## Inspection and remote registration

- Add read-only `too hub conf`: print the selected root, effective client URL,
  source (`environment|config|default`), and any guest-gateway transformation.
  It performs no network request and reads no Hub server settings or records.
- `too hub` probes the configured target regardless of local process state.
  Show its endpoint, actual availability, and server version, plus a separately
  labeled local process state. Preserve the existing local agent tree and its
  discovery diagnostics. Display `starting`, `unavailable`, `incompatible`, and
  `unknown` accurately instead of collapsing them into `not running`. Local
  record errors cannot prevent probing a remote target.
- `hub start/stop` control only the verified local process. They never send a
  shutdown request to the configured remote target or signal its reported PID.
- Server `roster = "local"` preserves current HOME discovery, registration
  checks, and two-missing-scan removal. `roster = "registered"` disables HOME
  scanning and admits agents through lease registration even when the client
  calls itself managed. Store those entries as registration-managed
  (`managed=false` in the existing roster format), without changing agent
  placement. Lease expiry makes them offline and retains membership/history;
  filesystem absence cannot remove them. Root ownership and lease fencing stay
  enforced. Existing same-root entries are preserved when changing modes and
  converted to the selected registration policy when registering; another root's
  entry is never adopted. Switching back to local policy requires a discoverable
  HOME before an entry can become filesystem-managed.

## Docker and deployment cases

Project the selected root's Hub client settings into guest configuration even
when PLACE differs from ROOT. Project the human CLI's local identity only where
needed by human tools; do not pass server settings or backend configuration.
Remove the `R/hub/ -> G/hub/` mount and its directory-creation side effect.
Propagate the effective URL override through existing explicit launcher inputs,
without broadening general environment grants.

Apply existing `TOOLANG_HOST_GATEWAY` translation only to a selected loopback
URL at the guest boundary. Preserve its scheme, port, and path; leave explicit
service names and remote hosts unchanged. Translation does not make a loopback
listener reachable by itself; the deployment must expose a reachable listener.

| Deployment | Client target | Hub service settings |
| --- | --- | --- |
| Local clients and Hub | Default URL | Default bind, `roster="local"` |
| Docker agent, host Hub | Guest-translated loopback URL or explicit host address | Reachable host bind, local roster if HOME is visible |
| Host agents, Docker Hub | Published host URL | Container bind such as `0.0.0.0`, `roster="registered"` |
| Remote Hub | Explicit URL such as `https://hub.example.com` | Reachable service, `roster="registered"` |

Hub communication uses outbound HTTP/SSE; it does not require Hub to read client
homes or call their Agent APIs. Exposing an agent's execution API remotely is a
separate concern. One endpoint still represents one logical root's dataset.
Keep the Hub's canonical root stable across container restarts to retain the
current namespace; pointing a new root at the same Redis URL does not migrate it.

## Implementation touchpoints

- `teaming/config.py`, `setup/teaming.py`, `setup/watcher.py`: separate client
  settings and refresh from server/backend settings; preserve home opt-out.
- `up/config.py`, `up/records.py`, `up/hub.py`: server configuration and process
  record ownership, listener settings, startup readiness, and conditional cleanup.
- `teaming/{discovery,schemas,client,agent_client,api,lifecycle}.py`: remove
  record-based discovery, add metadata connection establishment, and separate
  actor identity. Retain shared process-token behavior in an appropriate helper.
- `teaming/{roster,agent_api,runtime,types}.py`, `driver/talk.py`, `up/server.py`,
  `plugin/toolsets/convo.py`: registration policy and concrete connection inputs;
  remove unused human-owner plumbing from agent-side transport and Talk loops.
- `cli/common/messaging.py`, `cli/toolang/commands/{hub,hub_status}.py`, Talk
  and team-observation callers: shared target resolution and separate status.
- `up/{mounts,sandbox}.py`, Docker sandbox adapter: client-only guest projection
  and removal of the Hub directory mount.
- Corresponding setup, messaging, Hub process/CLI, mount, and sandbox tests;
  focused messaging/hosting/layout documentation. Generate a behavior-change
  changelog entry through `too aid.too update_changelog` during implementation,
  with maintainer verification before merge.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| Default and overrides | Default, config URL, and environment precedence resolve consistently across agent/human callers; invalid selected values fail without fallback. |
| Client/backend isolation | Remote clients work with no `hub/` directory and no backend settings; denied reads of server files do not affect them and no backend factory is invoked. |
| Human separation | The process record and `/info` contain no human; agent operation requires no human configuration. Human tools use their local actor and existing message behavior remains valid. |
| Metadata contract | Missing identity, invalid metadata, or incompatible API version blocks application mutations; proxy path prefixes work and `/info` does not register a human. |
| Stale or corrupt local record | Configured remote operations still succeed; local inspection reports its own problem and never signals an unrelated process. |
| Legacy record | Local control can safely stop/reconcile the old record shape, and the next publication omits human/identity; clients never read it. |
| Graceful SIGTERM | A real isolated subprocess exits through Uvicorn; stop succeeds and its record is absent. The test must fail on the reproduced implementation. |
| Force and failed startup | Forced kill, failed startup, and startup timeout leave no owned record or child; nonforced timeout retains ownership and reports failure. |
| Replacement race | Late old-process cleanup cannot delete a replacement record or signal its process; synchronize events rather than sleep. |
| Independent status/control | Remote target can be healthy with no local process, or unavailable with a local process running. Local stop affects only that process. |
| Registered roster | Agents with no server-visible HOME can register, renew, message, and export; expiry marks them offline without deleting history. Local roster checks and cross-root rejection still pass. |
| Roster mode change | Same-root registration follows the newly selected policy without losing history; local conversion requires HOME, and foreign root claims are rejected. |
| Guest inputs | A guest with PLACE different from ROOT receives the selected Hub URL and no server/backend data or Hub mount; explicit remote hosts are not rewritten. |
| Bind versus URL | Nondefault port and wildcard/IPv6 bind honor server precedence without altering client URL; occupied ports fail without fallback. |
| Reconnect and dataset change | Hub restart and URL refresh recover background communication without stopping runs; pinned batches reject identity changes and never advance a foreign dataset's cursor. |
| Delivery semantics | Lost acknowledgments preserve uncertain-send results and message IDs; no automatic message replay or direct-backend fallback occurs. |

Keep the default suite offline and deterministic using temporary roots, local
subprocesses, and HTTP test transports. Add opt-in container/wire coverage for a
containerized Hub with no shared client HOME and for a Docker agent without a
Hub directory mount. Implementation must pass the repository's default checks.
For this definition-only change, verify source references, examples, links, and
`git diff --check`.

## Risks and open questions

The split changes experimental configuration and requires an explicit upgrade;
custom-port deployments must configure a client URL. Remote roster mode retains
offline entries because the server cannot infer permanent removal from absent
client files. Moving the Hub's canonical root still selects a different backend
namespace. These behaviors must be visible in migration and deployment docs.

No unresolved design questions. Human approval is required before implementation.
