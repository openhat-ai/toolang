# Docker Hub access and consistent mount layout

Status: proposed; updated to reuse existing local-service routing and preserve
relative filesystem layout. No implementation approval has been given.

## Goal and success criteria

Docker agents must reach their selected host Hub. Host and guest root/place
bases may differ; paths below each base keep their names, scope, and relative
layout. Successful Hub access means a live lease, Talk processing, conversation
tools, and event export.

## Existing behavior and scope

Docker already supplies `TOOLANG_HOST_GATEWAY` and the Docker `host-gateway`
entry. Ollama/llama.cpp use `replace_guest_loopback` in
`src/toolang/plugin/catalogs/_local.py`; reuse that URL transformation through a
package-neutral helper, preserving those plugins' explicit-endpoint behavior.

The existing `ROOT/hub/hub.json` also owns PID/create-time validation, readiness,
and the actual port after CLI/environment overrides. Retain this necessary
runtime record and mount its directory at the same relative guest path. Keep
`ROOT/config.toml` as the authored configuration source. Do not introduce a
second listener, guest discovery format, proxy, or capability-token system.
This supersedes the sandbox-network exclusion in the previous
[Hub transport definition](agent-hub-transport.md).

This definition covers local Docker Hub access and mount mapping. Rust shell and
image-environment bugs require separate minimal fixes and regression tests.
Historical State cleanup requires a separate retention decision; this work does
not delete or migrate retained data.

## Hub connection

- Use the existing Hub HTTP API and lease/session/recovery behavior. Resolve
  configured defaults at the host boundary; preserve the actual running
  human/backend identity from the existing record, including for containers with
  different usernames and filesystem roots.
- Agent polling, `convo`, and export resolve the same selected Toolang root and
  connection provider. A nonresident agent's place is not its Toolang root.
- Read `hub/hub.json` through the mapped root. Translate its host-local endpoint
  with the existing gateway rule at the Docker boundary. Shared clients receive
  concrete paths/routing values; they do not infer Docker from ambient state.
- Keep one Hub listener. Add ordinary `teaming.hub.host` configuration and
  `too hub start/serve --host` using existing CLI/config factory patterns;
  default to `127.0.0.1`. Existing port precedence remains unchanged. Discovery
  includes the actual advertised host, using loopback for wildcard binds, and
  retains compatibility with existing records lacking that field.
- Docker Desktop gateway access to the current loopback Hub is verified on this
  Mac. Linux bridge access requires the operator to bind the same service to a
  reachable host interface, as with Ollama. Do not silently widen the default
  listener or add Docker-specific forwarding. Document an explicit bind-address
  example and the existing trusted-local-client access model.
- Mount `ROOT/hub/` read-only as `guest_ROOT/hub/`, including the existing
  record/lock/log names. Mount the directory so atomic record replacement is
  visible on Linux. Guest clients do not validate host PIDs. Create the source
  directory when preparing a mount, without starting Hub implicitly.
- Missing/stopped Hub remains a recoverable communication failure. Existing
  agents reconnect when Hub starts/restarts, including a changed actual port;
  record identity/session pinning keeps cursors isolated. Local runs continue.

## Mount mapping decisions

Define explicit host/guest root, place, and home pairs at orchestration startup.
A resident has root equal to place; nonresident guest bases must remain distinct.
Use the existing sandbox root for the guest Toolang root and the existing
`/tmp/toolang/<root_code>/<place_code>` placement shape for nonresident guest
places. Pass canonical host identity separately from mapped filesystem paths.

Reuse a covering mount before creating an additional mount. Match the most
specific owning base, preserve its suffix, and retain current read/write intent.

| Host source | Guest destination / decision |
| --- | --- |
| `ROOT/config.toml` and root capability directories | Same suffix below guest root; do not substitute place/home inputs. |
| `PLACE/config.toml`, capability directories, and State | Same suffix below guest place; coalesce with root when equal. |
| `HOME/` | Same `agents/<name>/` layout below guest place, read-write. |
| Root/place `catalog.json` | Same filename and owning-root/place suffix, read-only; remove the extra home `.runtime/model-catalog.json` alias. |
| Home `catalog.json` | Reuse the home mount at `HOME/catalog.json`; preserve selected source priority. |
| External or bundled selected catalog | One read-only mapping under `guest_PLACE/.inputs/models/<path-hash>/<original-basename>` only when no existing mount covers it. |
| `ROOT/hub/` | `guest_ROOT/hub/`, read-only. |
| `ROOT/.runtime/active/<address>/` | Same suffix below guest root, read-write for registration; protect `launches/` with the read-only subtree mount below. Do not expose other addresses or rename it to home `.runtime/registration`. |
| `ROOT/.runtime/active/<address>/launches/` | Same suffix below guest root, read-only, including all launch generations. Replaces `PLACE/.sandbox/<name>/launches/` and the home `.runtime/sandbox` alias. |
| `HOME/.lab/` | Reuse `guest_HOME/.lab` through the home mount; remove the duplicate `.workspaces/lab` mount and point logical `lab` there. |
| External workspace grant | Retain the existing `.workspaces/<name>` mount when no root/place/home mount covers it; preserve nested workspace relationships. |

### Runtime directory ownership and lifetime

Launch staging belongs to the selected Toolang root, including for visiting and
roaming agents. Consolidate it with the existing address registration:

```text
ROOT/.runtime/active/<address>/
  <address>.json
  register.lock
  launches/<launch_id>/
    guest.env
    docker_guest.sh
    docker_guest.py
    <development-wheel>.whl   # Only for development launches.
```

Use the full address, including its place code when present, rather than only
the agent name. Resolve both host and guest staging paths at orchestration and
pass concrete values to the sandbox plugin; it must not derive them from PLACE.
Keep the existing registration filename and lock. The reference remains a
locator to authoritative `HOME/.runtime/status.json`; do not copy status here.

Mount the whole `launches/` subtree read-only over the writable address mount,
so its parent cannot provide a writable route to bootstrap files. Keep a unique
launch directory until its associated container is released. Registration
withdrawal removes only the matching reference; resource cleanup removes only
the matching launch directory, then removes empty parents under the existing
guards. An old launch must never remove a replacement's reference or staging.
Directory existence alone does not indicate an active workload.

Persist the exact staging locator before preparing resources and retain it for
recovery. Existing recorded paths remain usable for stop/release; do not move
live mounts. Preserve rejection of unreferenced staging in both the old and
new locations until associated resources can be verified.

Keep generated environment data in the launch's own staging path. Preserve
PR #942's fix for Docker-created empty dotenv files and the required masking of
an existing authored home dotenv; never stage the whole host root environment.
Keep captured configuration rebasing and linked-source overrides only where
needed to resolve an external host path; do not rename logical config files.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Gateway reuse | Hub and existing local-model URL tests share transformation behavior; Ollama's authored explicit endpoints retain their current meaning. |
| Real Docker communication | On macOS and Linux with an explicitly reachable Hub binding, an isolated guest registers, handles a local-stub Talk batch, replies, and exports events. |
| Late/restarted Hub | No implicit Hub startup; existing guests reconnect through directory-mounted current records without advancing another backend's cursor. |
| Root/place identity | Resident and nonresident clients read their selected root Hub, with unchanged canonical host-root identity and correct human defaults. |
| Mapping table | Every listed source preserves its owning-base suffix and filename; covered catalogs/lab have no duplicate aliases. |
| Runtime root ownership | Resident, visiting, and roaming launches stage under the selected root's full address; different places with the same agent name do not collide. |
| Staging lifetime and permissions | Guest registration works while `launches/` remains read-only; failed startup and stop/restart clean only owned resources, and delayed old-launch cleanup preserves the replacement. Staging without a reference is not reported as running. |
| Existing resource recovery | An existing recorded staging path remains stoppable; neither live mounts nor unverified orphan directories are moved or deleted. |
| Source priority | Root, home, explicit external, and packaged catalogs select the same data on host and guest. |
| Existing contracts | Private registration access, nested workspaces, symlinks, config rebasing, dotenv restrictions, historical workspace URIs, and bounded shutdown still pass. |

Default tests stay offline. Opt-in Docker/Redis/Valkey checks use isolated
conversations and a local model stub, never user conversations or live providers.
Update focused hosting, layout, messaging, and model documentation and generate
the user-facing changelog through `too aid.too update_changelog`.

## Touchpoints and remaining boundaries

Likely files: `up/{hub,sandbox,mounts,server,registry,discovery}.py`,
`common/layout.py`, `base/types/sandbox.py`, Docker sandbox/bootstrap,
`teaming/{config,discovery,agent_client}.py`, `setup/teaming.py`, `convo` tool
factory, a neutral URL helper, and focused unit/integration tests.

The existing single-file config/catalog binds have separate Linux atomic-replace
limitations: guest root-config writes fail with `EBUSY`, and host replacement
can leave a native Linux file bind on old content. Path renaming does not fix
those semantics. Define the required update/write contract before changing that
mount strategy; do not mount all peer homes or credentials as a shortcut.

Root State currently exposes legacy unversioned revisions alongside active
versioned data. Audit and retention are separate from path normalization. No
legacy cache deletion, automatic migration, listener widening, or live agent
restart is authorized by this proposed definition.
