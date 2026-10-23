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
| Launch-generated `.<name>.env` | Always mount read-only as `guest_ROOT/.<name>.env`; use the generated-environment contract below. |
| Root/place `catalog.json` | Same filename and owning-root/place suffix, read-only; remove the extra home `.runtime/model-catalog.json` alias. |
| Home `catalog.json` | Reuse the home mount at `HOME/catalog.json`; preserve selected source priority. |
| External or bundled selected catalog | One read-only mapping under `guest_PLACE/.inputs/models/<path-hash>/<original-basename>` only when no existing mount covers it. |
| `ROOT/hub/` | `guest_ROOT/hub/`, read-only. |
| `ROOT/.runtime/active/<address>.json` | Host-owned current-launch reference; no guest mount or guest mutation is needed. |
| `ROOT/.runtime/active/<address>/` | Temporary Docker inputs, mounted read-only at the same suffix below guest root. No `launches/` or launch-ID subdirectory; replaces `PLACE/.sandbox/<name>/launches/<id>/` and the home `.runtime/sandbox` alias. |
| `HOME/.lab/` | Reuse `guest_HOME/.lab` through the home mount; remove the duplicate `.workspaces/lab` mount and point logical `lab` there. |
| External workspace grant | Retain the existing `.workspaces/<name>` mount when no root/place/home mount covers it; preserve nested workspace relationships. |

### Runtime directory ownership and lifetime

Each full address selects one home with at most one executor owner. Resident
`hak` and visiting `lex@<t_code>` each have one current owner. New executions of
the same roaming source receive distinct `check@<s_code>` addresses; restarting
a retained address preserves that address. None requires another instance or
launch directory in the registry.

```text
ROOT/.runtime/active/
  hak.json
  lex@<t_code>.json
  check@<s_code>.json
  hak/                     # Temporary Docker inputs, only when needed.
    .hak.env
    docker_guest.sh
    docker_guest.py
    <development-wheel>.whl # Only for development launches.
```

Create one `<address>.json` at launch. Preserve the existing reference fields
`version`, `status`, and `launch_id`; add explicit `location` (`host` or `guest`)
and the concrete `sandbox` selection. Derive address from the filename and
validate it against the referenced runtime identity. `status` still locates
authoritative `HOME/.runtime/status.json`; do not duplicate changing status here.
Keep the generation fence in the content, not in the directory hierarchy.

The host publishes and retires references under a per-address host file guard.
Guests update only their mounted home runtime status and do not write the root
registry. Explicit host shutdown retires the matching reference; after guest
self-exit, the existing host watcher retires it on its next verified refresh.
A record's presence never proves liveness. This removes the guest registry mount
without exposing peer records or relying on single-file guest writes.

Docker still needs file inputs for bootstrap. Use one temporary directory named
by the full address alongside the reference; a host execution needs no such
directory. Resolve concrete host/guest input paths at orchestration and pass
them to the plugin. These inputs are neither additional instances nor retained
launch history. Reuse the fixed directory only after the previous container and
pending creation helpers are conclusively released under management guards.
Before removing or replacing inputs or references, recheck the expected launch
against authoritative status under the existing guards. Delayed cleanup from an
old launch must leave its replacement untouched.

Persist the exact bootstrap locator before preparation for recovery. Keep
existing recorded paths and nested registration formats readable for normal
stop/release, publishing only the flat reference for new launches. Do not move
live mounts or delete unverified orphan inputs in old or new locations.

Keep captured configuration rebasing and linked-source overrides only where
needed to resolve an external host path; do not rename logical config files.

### Generated guest environment

Resolve host root/agent dotenv inputs once using the existing
[environment policy](agent-environment-policy.md#agent-environment), including
source priority, explicit empty files, and ordered `pick`. Preserve its
host-side source identity for resident, roaming, and visiting agents. Stage only
the resolved guest grants and existing mapped workload values, never the whole
host root environment or host-only `keep` values.

Always create `.<name>.env` in the temporary Docker input directory and mount
that file read-only as `guest_ROOT/.<name>.env`, including when there are no granted
variables. This is a generated runtime input for the current agent in its own
container; it does not change host policy filenames or source selection. Pass
its concrete guest path to bootstrap and use it as the sole guest dotenv input.
Merge its resolved bindings with the native image/entrypoint environment under
the guest environment policy; keep runtime controls authoritative. Guest startup
must not rerun `pick`, merge lower-priority dotenv files, or lose the captured
environment during setup refreshes, model calls, or tool subprocess launches.

Keep the complete home mount. An authored `HOME/.env` may remain visible and
writable through that mount, but contributes no additional guest environment
values. Do not mask it with generated data or an empty file. Do not directly
mount authored root dotenv inputs. In particular, never create an absent host
`HOME/.env` as a mount target; preserve PR #942's fix for that side effect.
This contract deliberately permits tools to read an existing home dotenv file;
it controls environment selection, not file secrecy.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Gateway reuse | Hub and existing local-model URL tests share transformation behavior; Ollama's authored explicit endpoints retain their current meaning. |
| Real Docker communication | On macOS and Linux with an explicitly reachable Hub binding, an isolated guest registers, handles a local-stub Talk batch, replies, and exports events. |
| Late/restarted Hub | No implicit Hub startup; existing guests reconnect through directory-mounted current records without advancing another backend's cursor. |
| Root/place identity | Resident and nonresident clients read their selected root Hub, with unchanged canonical host-root identity and correct human defaults. |
| Mapping table | Every listed source preserves its owning-base suffix and filename; covered catalogs/lab have no duplicate aliases. |
| Runtime root ownership | Docker inputs belong to the selected root and full address; different places with the same agent name do not collide. Direct host execution creates no Docker input directory. |
| Current execution record | Each full address has one flat JSON reference with explicit location/sandbox and an in-content launch ID; no registry launch hierarchy. Existing formats remain discoverable and stoppable without duplicate publication. |
| Registry ownership | Guests have no root registry write mount. Host stop removes only the matching reference; guest self-exit is retired on the next verified host refresh and is never reported alive merely because its record exists. |
| Input lifetime and permissions | Docker inputs are read-only in the guest. Fixed paths are reused only after verified release; failed startup and delayed cleanup preserve replacement inputs and references. |
| Existing resource recovery | An existing recorded staging path remains stoppable; neither live mounts nor unverified orphan directories are moved or deleted. |
| Source priority | Root, home, explicit external, and packaged catalogs select the same data on host and guest. |
| Generated dotenv entry | With a picked variable, explicit agent file, explicit empty file, or no available credential, a read-only `guest_ROOT/.<name>.env` always exists and contains only the resolved guest values. Resident and nonresident launches retain their host policy identity. |
| Home dotenv precedence | A conflicting or lower-priority `HOME/.env` remains unchanged and visible but adds no values during guest startup, setup refresh, model calls, or tool launches. An empty generated root file never falls back to it or reruns `pick`. |
| No host dotenv artifacts | Starting, stopping, failing, and restarting a guest never creates an absent host home/root agent dotenv file and never modifies an existing authored one. |
| Existing contracts | Nested workspaces, symlinks, config rebasing, dotenv restrictions, historical workspace URIs, and bounded shutdown still pass. |

Default tests stay offline. Opt-in Docker/Redis/Valkey checks use isolated
conversations and a local model stub, never user conversations or live providers.
Update focused hosting, layout, messaging, and model documentation and generate
the user-facing changelog through `too aid.too update_changelog`.

## Touchpoints and remaining boundaries

Likely files: `up/{hub,sandbox,mounts,server,registry,discovery,watcher,workload}.py`,
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
