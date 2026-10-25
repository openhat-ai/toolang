# Hub Clients, Registration, and Local Process State

Status: Proposed definition based on the agreed discussion; awaiting plan approval.
No implementation is included.

## Goal and success criteria

Agents and human tools connect to a local, containerized, or remote Hub using
client configuration. Hub owns its backend and learns agents through registration,
without client process records or HOME access. A successful local Hub stop leaves
no running record for the stopped process.

## Scope

- In: separate client/server configuration, network identity discovery,
  client-declared human identity, registration-driven membership, Docker client
  inputs, local process cleanup, and accurate Hub inspection.
- Out: authentication, authorization, TLS termination, deployment automation,
  backend data migration, and new permanent-member-removal APIs or commands.

This supersedes file-based discovery and filesystem-driven membership in
[agent Hub transport](agent-hub-transport.md), [teaming](teaming.md), and the
client-discovery decision in [Hub files](hub-files-directory.md). Keep the existing
Hub artifact directory. Human participants and agent ownership remain supported.

## Verified baseline

- Agent clients read `hub/hub.json`; human clients use
  `HubProcess.connection()`. Both obtain endpoint, human, and backend identity
  from the local record. Setup also constructs backend configuration for clients.
- Managed registration requires the Hub scanner to find HOME. Two missing scans
  can remove offline members and their GC memberships. Docker agents mount the
  host Hub directory to obtain discovery data.
- `HubProcess.stop()` waits for process exit but leaves record cleanup to an
  outer `serve()` finally block. An isolated subprocess using actual Hub hosting
  code reproduced successful stop with a dead process and a `running` record:
  installed Uvicorn 0.52.1 replays SIGTERM after ASGI shutdown, bypassing that block.

## Configuration contract

Use only each deployment's existing `ROOT/config.toml`, with two optional sections:

```toml
[teaming]
human = "bryan"
enabled = true
hub = "http://127.0.0.1:7001"

[hub]
host = "127.0.0.1"
port = 7001

[hub.backend]
url = "redis://localhost:6379/0"
```

| Setting | Consumer | Default |
| --- | --- | --- |
| `teaming.human` | Client identity and agent owner declaration | OS username |
| `teaming.enabled` | Client participation | `true` |
| `teaming.hub` | Client target URL | `http://127.0.0.1:7001` |
| `hub.host` | Hub listener | `127.0.0.1` |
| `hub.port` | Hub listener | `7001` |
| `hub.backend.url` | Hub backend | `redis://localhost:6379/0` |

The client loader consumes only `[teaming]`; the server loader consumes only
`[hub]`. Missing sections use defaults. Semantically invalid unrelated settings
in otherwise valid TOML do not block the other consumer. Clients never construct
backend configuration, derive its identity, access Redis, or read `ROOT/hub/`
for connection information. Hub startup does not resolve a local human identity
or depend on client enablement or target URL.

Root `teaming.enabled=false` disables agent registration, heartbeat, message
consumption, conversation tools, and event export. Human messaging commands
report disabled without connecting. Existing HOME `[teaming] enabled=false`
remains an additional agent opt-out; a home cannot re-enable a disabled root.
Inspection remains available and local execution is independent of teaming.
Human and Hub URL belong to the selected root; home/project/other-place settings
cannot override them. No roster mode, directory, or scan configuration is added.

Resolve the client URL as `TOOLANG_HUB_URL` > `teaming.hub` > its default. Reject
an empty/invalid selected value without fallback. Accept absolute HTTP/HTTPS
URLs with optional proxy path prefixes; normalize trailing slashes and reject
userinfo, queries, fragments, wildcard destinations, and invalid ports.

Keep `--port` and add `--host` to `hub start/serve`. Listener precedence is CLI >
`TOOLANG_HUB_HOST` or `TOOLANG_HUB_PORT` > the corresponding `[hub]` field >
default. Support explicit IPv4/IPv6 binds and retain busy-port failure without
fallback. Neither bind settings nor process records change the client URL;
a custom server port also requires the corresponding `teaming.hub` URL.

For a Docker Hub, the container's root file contains its own `[hub]` listener
and `[hub.backend]` settings. For example, bind `0.0.0.0:7001` in the container
and publish host port `17001`. The host's root file needs only:

```toml
[teaming]
hub = "http://127.0.0.1:17001"
```

Host clients need no server or backend configuration and share no Hub files
with the container.

## Identity and connection lifecycle

- Extend `/info` to `{version, api_version, identity}`. Identity is an opaque
  Hub dataset identifier; preserve its existing server-derived value for an
  unchanged backend/root. Return no human, backend URL, credentials, root path,
  root code, or storage keys. The initial handshake requires no expected identity,
  performs no participant registration, and remains available if backend access
  fails after startup. `/healthz` continues to report backend readiness.
- Establish connections asynchronously before application mutations. Transport
  receives concrete target settings from CLI/setup/launcher boundaries, without
  implicit file or environment discovery. Reuse a connection per operation or
  stream. Client connection state contains endpoint and dataset identity;
  actor/owner is supplied separately from client settings.
- Human requests select their actor through the existing percent-encoded
  `X-Toolang-Human` header. Require a valid human actor on human messaging/team
  routes; missing/invalid values fail before storage access. Agent actors remain
  route-derived and agent requests do not need that header. Remove Hub's single
  configured-human binding and startup registration of a local default human.
- Every application request and SSE connection pins the dataset through the
  existing `X-Toolang-Backend` header. The value is opaque to clients; the header
  remains a consistency check. Pin each admitted Talk batch and all its tools.
  Identity mismatch rejects the operation before mutation, without replay.
- Root configuration refresh applies to new connection/admission boundaries.
  Reconnect may establish a new identity for future work; old batches, cursors,
  and pending progress cannot cross datasets. Human Talk views require reopening
  after target/identity/actor changes. Preserve draft isolation, lease fencing,
  event recovery, bounded backoff, and uncertain-send handling.
- Diagnostics identify the attempted URL and distinguish configuration errors,
  unreachability, incompatible metadata, backend failure, and identity changes.
  Never auto-start a local Hub, connect directly to Redis, or replay a message
  to repair a communication failure. Hub outages do not stop local runs.

## Registration and membership

Hub uses the same registration protocol in every deployment. Remove startup and
periodic HOME scans, managed-HOME registration checks, missing-scan counters,
filesystem-based membership removal, and the proposed `roster` mode switch.

`PUT /agents/{agent}/lease` accepts `{owner, endpoint}`. `owner` is the canonical
`human:<name>` derived from the registering client's `teaming.human`; remove the
`managed` field. Hub validates the typed owner, preserves existing owner/name
conflict checks, and atomically creates/updates directory membership and the
matching lease. It never substitutes a human from its own root configuration.
Human requests register their own participant as needed; metadata/health requests
and server startup register no human. The existing same-owner system-conversation
membership behavior and conversation rules remain intact.

A never-registered agent is unknown to Hub. First registration makes it a known
member; lease release or expiry makes it offline while retaining its directory
entry, memberships, messages, cursors, and history. Offline registered agents can
still be message targets. Re-registration restores presence with existing fencing;
a different owner or competing live lease cannot silently take over the name.
Agent names remain unique within the Hub dataset, including across client roots.

No scan, process shutdown, local HOME deletion, or `too remove` operation implies
permanent Hub membership deletion. Permanent deletion requires a separately
defined explicit management operation; this plan adds no such API or command.
Local CLI listings may continue to discover unstarted agents from local files,
without publishing those discoveries to Hub.

Preserve existing backend schema, namespace guards, directory records, and data.
Legacy discovery/roster rows cease to drive registration or cleanup and remain
inert; do not reset storage or infer deletion from those rows. The namespace is
owned by the Hub's canonical root, not by a client's root path.

## Local process control and inspection

`ROOT/hub/hub.json` is an `up`-owned local process record containing only
`pid`, `created`, bind `host`/`port`, and `starting|running`. Remove human and
backend identity. Keep atomic mode-0600 writes, `hub.lock`, and `hub.log`.
PID and creation-time verification remains mandatory; published status can be stale.

The stopped representation is an absent record. Remove an owned record during
ASGI lifespan teardown before Uvicorn replays a signal, retaining outer cleanup
for startup failures. After confirmed exit, stop and failed/timed-out startup
also remove any surviving record for that child, including after forced kill.

The child compares identity while holding its lifetime lock. Controller cleanup
takes the released lock and compares the current record before removal; it never
removes a replacement's state. Success requires the old record absent or replaced.
Allow up to five seconds to reconcile contention with a competing startup, then
report cleanup failure. A nonforced stop timeout retains state and fails.
Reconcile parseable dead records under the same lock; corrupt/unverifiable records
produce diagnostics without signaling a guessed PID. Locally probe wildcard binds
through the matching loopback address without changing client configuration.

`too hub` probes the configured client target and shows its endpoint, actual
availability, and server version separately from local process state. Preserve
the existing local agent tree and diagnostics, including unstarted local agents.
Do not collapse starting/unavailable/unknown into a misleading stopped label.
Local record corruption cannot prevent remote target inspection. `hub start/stop`
manage only the verified local process, regardless of the client target URL.

## Docker agent inputs

Project only the selected root's effective `[teaming]` client settings into the
guest root, including its resolved human so a guest OS username does not change
ownership. Preserve HOME enablement opt-out. Replace copied PLACE client settings
with the selected ROOT values and strip copied `[hub]` server/backend settings.
Remove the `R/hub/ -> G/hub/` mount and its directory-creation side effect.

Apply existing host-gateway translation to a selected loopback URL at the guest
boundary. Preserve scheme, port, and path; leave explicit service/remote hosts
unchanged. Pass concrete launcher inputs without broadening environment grants.
A reachable listener still must be exposed by the deployment. Hub communication
uses outbound HTTP/SSE and requires no access to client homes or Agent APIs.

## Implementation steps and touchpoints

- [ ] Split client/server formats and loaders in `teaming/config.py`,
  `setup/teaming.py`, and `up/config.py`; update setup refresh and root/home policy.
- [ ] Add metadata connection establishment and independent actors in
  `teaming/{schemas,client,agent_client,api,lifecycle}.py` and
  `cli/common/messaging.py`; remove file-based connection discovery.
- [ ] Replace scanner-driven registration in `teaming/{roster,agent_api,runtime,types}.py`
  and backend protocol/Valkey operations with client-owned registration; update
  Talk/export hosting, keep stored ownership checks, and remove dead scan paths.
- [ ] Fix owned-record publication/cleanup in `up/{hub,records}.py`; update
  `cli/toolang/commands/{hub,hub_status}.py` for independent control and inspection.
- [ ] Update `up/{mounts,sandbox,server}.py`, `plugin/toolsets/convo.py`, and the
  Docker adapter to pass concrete client settings and remove Hub directory access.
- [ ] Add the acceptance coverage below to setup, messaging, CLI, process,
  mount/sandbox, and opt-in container/wire tests. Keep default tests offline.
- [ ] Update focused messaging/hosting/layout docs and generate the behavior-change
  changelog through `too aid.too update_changelog`; require maintainer verification.
- [ ] Run default Ruff, formatting, type, and pytest checks before implementation
  commits; fetch/rebase on current `origin/main`, reverify, and push before handoff.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| Configuration boundary | Absent/empty sections use defaults. Each loader validates only its section; invalid unrelated fields in valid TOML do not block it. Changing bind port never changes client URL. |
| Enablement and identity | Root disablement gates clients without affecting Hub startup; HOME opt-out remains. Client human/URL cannot be overridden by other scopes. |
| Remote and Docker Hub | Host clients using only `teaming.hub` can register, message, observe, and export through a published/remote URL without Hub files or backend factories. |
| Guest projection | ROOT differs from PLACE, guest username differs, and host uses a URL override: selected client identity/URL reach the guest, server fields and Hub mounts do not. Gateway translation preserves explicit remote hosts. |
| No filesystem authority | Starting Hub with arbitrary agent directories creates no members. Registration works with no server-visible HOME. Deleting HOME or stopping a process never deletes Hub membership. |
| Registration and conflicts | First registration creates the declared human/agent membership; same-owner reconnect preserves history, different-owner or competing-live-lease registration fails before mutation. |
| Offline delivery | Lease release/expiry marks registered agents offline while retaining membership and accepting messages for later consumption. Never-registered targets remain unknown. |
| Human routing | Different configured client humans operate as their own actors; server startup and `/info` create none. Missing/invalid human actors fail on human routes; agents use route/lease identity. |
| Dataset isolation | Invalid/incompatible metadata prevents application mutations. Restart/URL refresh recovers future work, while pinned batches and pending cursor writes reject another dataset. No uncertain message is automatically replayed. |
| Stop regression | Real isolated SIGTERM shutdown fails the old behavior and passes after the fix: stop succeeds, process is gone, and its record is absent. |
| Control failures and races | Forced kill/startup failure/timeout clean owned state; nonforced stop timeout fails. Old cleanup cannot delete a replacement record or signal an unrelated PID; use synchronization rather than sleeps. |
| Independent inspection | Remote service may run with no local process, or fail while a local process runs. Corrupt local records do not block remote status; local stop affects only its process. |
| Upgrade safety | Existing schema/history, owners, cursors, and namespaces survive configuration moves; old scan rows cannot remove members. Legacy process records support verified local cleanup only. |

Use temporary roots, isolated local subprocesses, and HTTP test transports in the
default suite. Opt-in container/wire tests cover both a Docker Hub with no client
HOME mounts and a Docker agent with no Hub directory mount.

## Compatibility, risks, and open questions

- Move `[teaming.backend]` to `[hub.backend]` and old `[teaming.hub].port` to
  `[hub].port`; `teaming.hub` becomes a URL string. Keep `teaming.human` and HOME
  `teaming.enabled`; add the root enablement gate. Reject legacy keys/shapes with
  source-specific migration diagnostics, without automatic rewrites or fallback.
- Upgrade Hub and clients together after stopping old processes. New clients
  require metadata identity; old registration bodies are rejected with a clear
  protocol diagnostic. Local control may decode legacy process records only to
  verify/stop/reconcile them, discarding human/identity fields.
- Keep current backend data and its server-derived identity. Moving the Hub's
  canonical root still changes namespace; containers must keep that root stable.
- Removing scans intentionally retains offline/previously discovered directory
  entries and stops HOME-based GC removal. New unstarted agents become reachable
  only after first registration. Permanent cleanup is a separate explicit feature.
- All clients of an endpoint join its dataset and share its agent-name namespace;
  a client root is not a separate tenant. Authentication remains outside scope.

No unresolved design questions. Implementation requires approval of this plan.
Definition-only validation checks source references, TOML examples, links, and
`git diff --check`; the product code and full test suite remain unchanged here.
