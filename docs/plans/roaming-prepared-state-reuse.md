# Placement layout, fetch, mount, and watch

Approved in conversation on 2026-10-30; implementation follows in this PR. Fetch external
inputs at startup, share placement-local sources/State, and keep one layout,
mount builder, and watcher behavior for all placements.

## Roots and execution

`ROOT` is the selected host Toolang policy/discovery root; `DIR/FILE.too` is the
canonical local source; `NAME` is its normalized agent name. `R8`, `P10`, and `P19`
retain their existing identity rules. Paths below are complete templates; braces
enumerate directories. One `AgentLayout` owns all path accessors.

| Host agent root (AR) | Host runtime root (RR) | Explanation |
| --- | --- | --- |
| `ROOT` | `ROOT` | Resident. |
| `/tmp/toolang/R8/P10` | `/tmp/toolang/R8/P10` | Visiting: stable requested URL/ref identity. |
| `DIR/.toolang` | `DIR/.toolang/R8/P19` | Roaming: shared sources/State; retained runtime per P19. |

| Host agent home (AH) | Guest agent home (GH) | Explanation |
| --- | --- | --- |
| `ROOT/agents/NAME` | `GR/agents/NAME` | Resident. |
| `/tmp/toolang/R8/P10/agents/NAME` | `GR/agents/NAME` | Visiting. |
| `DIR/.toolang/agents/NAME` | `GR/agents/NAME` | Roaming. |

Guest Toolang root = agent root = runtime root = `GR`, default `/root/.toolang`;
`GH = GR/agents/NAME`. Host execution uses host paths directly. Guest execution
uses the same host fetch/preparation, then mounts the directories below. Watchers
use the resolved layout of their own process; guest AR/AH resolve to GR/GH.

## Root files

No `config/` directory or legacy lookup. Root/home use the same file names.

| Host root file | Guest file | Explanation |
| --- | --- | --- |
| `ROOT/config.toml` | `GR/config.toml` | Resident root config and host policy. |
| `/tmp/toolang/R8/P10/config.toml` | `GR/config.toml` | Visiting local root config, if present. |
| `DIR/.toolang/config.toml` | `GR/config.toml` | Roaming hardlink to selected project TOML. |
| `AR/catalog.json` | `GR/catalog.json` | Optional root catalog. |
| `AR/source.json` | `GR/source.json` | Source origins; guest launch adds captured config identity. |
| `AH/source.json` | `GH/source.json` | Requested/resolved visiting source or roaming Program ownership. |

## Startup fetch

Resident has no startup fetch; new/clone supplies authored sources. Visiting keeps
its URL/ref cache policy and fetches only the selected remote Program. Roaming
checks local inputs on every actual startup, including retained-P19 restart.
Attaching to an existing executor does not fetch. Fetch belongs to startup
orchestration, never watchers; guest execution does not repeat it.

| Input | Placement output | Explanation |
| --- | --- | --- |
| `URL/ref` | `/tmp/toolang/R8/P10/agents/NAME/agent.too` | Fetch remote Program bytes, using the existing cache. |
| `URL/ref` | `/tmp/toolang/R8/P10/agents/NAME/source.json` | Record requested/resolved ref, entry, and digest. |
| `DIR/FILE.too` | `DIR/.toolang/agents/NAME/agent.too` | Hardlink the Program without changing its bytes. |
| `DIR/FILE.too` | `DIR/.toolang/agents/NAME/source.json` | Record canonical Program ownership/digest; reject normalized-name conflicts. |
| `SELECTED_TOML`, `SELECTED_CATALOG` | `DIR/.toolang/source.json` | Record original config/catalog paths in their shared owning scope. |
| `SELECTED_TOML` | `DIR/.toolang/config.toml` | Hardlink the nearest `toolang.toml`; do not merge other project TOMLs. |
| `SELECTED_CATALOG` | `DIR/.toolang/catalog.json` | Hardlink the selected catalog without copying or merging its contents. |

`SELECTED_TOML` is the first existing `toolang.toml` found from DIR upward,
stopping at the nearest Git worktree root; outside Git, inspect DIR only. Stop
when selected: an empty TOML still counts, and invalid/unreadable input fails
rather than falling back to a more distant file. Use only the selected TOML's
contents. Keep the existing catalog-selection precedence and link only its
selected file, `SELECTED_CATALOG`.

Fetch preserves source bytes, changing only the placement path/name. Record
original config/catalog paths in `AR/source.json` and pass resolved origins to
readers, so relative paths retain their source-directory meaning on host and
guest. A selected ancestor TOML still does not grant ancestor workspaces. Normal
home configuration remains a separate scope. Consumers read placement-local
files; remove only obsolete fetch-owned inputs. Visiting cache freshness retains
its existing policy. CLI/env catalog overrides stay invocation-local.

At each startup, retain each hardlink if it already names the source file;
otherwise create a new hardlink and atomically replace the placement entry.
Require file-hardlink support and a common filesystem; fail clearly if linking
is unavailable. Upstream atomic replacement during execution may leave the
placement using the old file until another startup refreshes the shared entry.
In-place writes through either name affect the same file. State/Setup periodic
checks must detect such writes even without a placement-directory event. Hardlink
creation does not change source bytes or permissions.

Roaming hardlinked inputs share in-place edits with their originals; replacing
either directory entry atomically separates it from the other. Visiting local
edits have no automatic remote writeback. Explicit project-editing commands still
use canonical `source_path`. State preparation generates/reuses immutable
revisions and pointers for host and guest alike; hardlinked inputs do not make
State revisions mutable. Mounting does not rewrite Program/config/catalog or
create guest-only State.

## Guest mounts

All placements emit the same mounts. Create and retain stable directory anchors;
atomic replacement of contained files remains visible. Root config/catalog use
read-only file binds from private launch hardlinks as startup snapshots. This
keeps Docker Desktop binds readable after source-entry replacement; restart
selects the new inode without copying catalog contents.

| Host directory | Guest directory | Explanation |
| --- | --- | --- |
| `AH/` | `GH/` | RW; Mount 1. Includes Program, receipt, home config/catalog, authored sources, and home/agent State. |
| `RR/agents/NAME/.runtime/` | `GH/.runtime/` | RW; Mount 2, always after Mount 1, even when it selects the home's own runtime. |
| `AR/config.toml` | `GR/config.toml` | RO, when present; startup snapshot. |
| `AR/catalog.json` | `GR/catalog.json` | RO, when present; startup snapshot. |
| Generated launch `source.json` | `GR/source.json` | RO; original origins plus config snapshot identity. |
| `AR/psyches/` | `GR/psyches/` | RW; root psyches. |
| `AR/skills/` | `GR/skills/` | RW; root skills. |
| `AR/services/` | `GR/services/` | RW; root services. |
| `AR/prompts/` | `GR/prompts/` | RW; root prompts. |
| `AR/.state/root/` | `GR/.state/root/` | RW; root State, pointer, and guards. |
| `AR/workspaces/NAME/lab/` | `GR/workspaces/NAME/lab/` | RW; roaming lab is shared across P19s/ROOTs. |
| `WORKSPACE_PATH` | `GR/workspaces/NAME/WORKSPACE/` | RW; one mount per explicit grant. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/` | `GR/.runtime/active/NAME/` | RO; immutable private launch inputs. |

`WORKSPACE_PATH` is each granted host directory; `L` derives from `launch_id`.
Mount assembly has no placement branches. Do not mount whole host roots,
workspace parents, sibling agents, other instances, or namespace ownership records.
Authored symlinks must resolve through selected directory mounts; reject targets
unavailable in the guest, including through intermediate links, instead of adding
mutable file overlays.
Instance runtime contains `status.json`, `runs.db`, `stats.db`, `agent.log`,
`logs/`, and `tools/`; runtime control/observation remains separate.

## Guest launch files

The private launch directory above contains these files. Environment aliases may
retain RO file mounts because this snapshot is immutable for the launch.

| Host file | Guest file | Explanation |
| --- | --- | --- |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/info.json` | `GR/.runtime/active/NAME/info.json` | Generate guest-path discovery metadata for this launch. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/guest.env` | `GR/.runtime/active/NAME/guest.env` | Generate selected environment, including configured workspace path corrections in the config patch. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/source.json` | `GR/source.json` | Generate source origins and config snapshot identity for all guest commands. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/inputs/K/FILE` | `GR/config.toml`, `GR/catalog.json`, or `GR/.inputs/catalog.json` | Hardlink selected read-only file inputs at stable private entries; K hashes the guest mount target. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/guest.sh` | `GR/.runtime/active/NAME/guest.sh` | Copy the packaged bootstrap unchanged. |
| `ROOT/.runtime/active/ADDRESS/docker/L/active/DEV.whl` | `GR/.runtime/active/NAME/DEV.whl` | Optional development wheel, copied unchanged. |

## Watch directories and current pointers

Every source subscription uses `recursive=False`. Enumerate existing nested
source directories and subscribe individually. Mount coverage never determines
watch scope. Events, an independent periodic check, and explicit State sync use
the same serialized State checking/publication path; unrelated activity cannot
starve checks. Setup retains its refresh/publication ownership and probing.

| Host path | Guest path | Explanation |
| --- | --- | --- |
| `AR/` | `GR/` | Root `config.toml`, `catalog.json`, `source.json`, and optional source directory creation. |
| `AH/` | `GH/` | Watch `agent.too`, `config.toml`, `catalog.json`, `source.json`, and relevant directory creation/removal. |
| `AR/{psyches,skills,services,prompts}/` | `GR/{psyches,skills,services,prompts}/` | State sources; shallow-watch each existing directory and authored subdirectory. |
| `AH/{psyches,skills,services,prompts,runnables,chores}/` | `GH/{psyches,skills,services,prompts,runnables,chores}/` | State sources; same shallow enumeration. |
| `AR/.state/root/v24/current` | `GR/.state/root/v24/current` | Shallow-watch the parent; detect external atomic replacement. |
| `AH/.state/home/v24/current` | `GH/.state/home/v24/current` | Shallow-watch the parent; detect external publication. |
| `AH/.state/agent/v24/current` | `GH/.state/agent/v24/current` | Shallow-watch the parent; reconcile even without source-byte changes. |
| `AR/.state/`, `AR/.state/root/`, `AH/.state/`, `AH/.state/{home,agent}/` | `GR/.state/`, `GR/.state/root/`, `GH/.state/`, `GH/.state/{home,agent}/` | Discover missing pointer parents; use nearest existing relevant ancestor, rebuild subscriptions, then rescan. |
| `RR/agents/NAME/.runtime/` | `GH/.runtime/` | No State/Setup subscription or traversal inside. |
| `AR/workspaces/NAME/` | `GR/workspaces/NAME/` | No State/Setup subscription or traversal inside workspace trees. |
| `AR/.state/root/v24/revs/`, `AH/.state/{home,agent}/v24/revs/` | `GR/.state/root/v24/revs/`, `GH/.state/{home,agent}/v24/revs/` | No subscription or recursive traversal; immutable revisions are loaded by reference. |
| `AR/.state/root/v24/revs/REV/layer.json` | `GR/.state/root/v24/revs/REV/layer.json` | Immutable root-layer record, not watched. |
| `AH/.state/home/v24/revs/REV/layer.json` | `GH/.state/home/v24/revs/REV/layer.json` | Immutable home-layer record, not watched. |
| `AH/.state/agent/v24/revs/REV/layers.json` | `GH/.state/agent/v24/revs/REV/layers.json` | Immutable agent composition, not watched. |

Validate/reconcile external pointer changes while preserving invocation-local
grants/overrides.
Agent `current` selects authored State; future acceptance uses new
generations, while accepted Runs keep their snapshots. Receipt changes validate
source ownership/provenance and refresh origin-dependent bindings, without
triggering upstream fetch. An origin change can affect configuration even when
the selected TOML bytes are identical.

## Runtime configuration and command arguments

`TOOLANG_CONFIG_PATCH` is a JSON object with the same tables and value types as
config.toml. Host and guest command boundaries load it after dotenv selection.
It overrides runtime configuration without changing authored files or shared
State. Guest generation corrects configured workspace paths only; no root/home
wrappers, binding records, temporary flags, or source metadata enter the patch.
Source origins, config snapshots, host human fallback, and the prepared revision
belong in guest `GR/source.json`.

Remove `--workspace-bindings` and its record type. Workspace/workdir options are
translated while building guest argv; logical workdirs remain unchanged. Lab
uses layout paths and its ordinary mount, with no patch or startup record.
Mounts remain fixed for a launch. Accepted Runs retain their grants; stale path
corrections must not revive revoked or rebound configured workspaces.

Acceptance covers plain host/guest patches through ordinary commands and serve,
unchanged shared State, CLI temporary workspace/workdir translation, implicit lab,
config revocation/rebinding, restart, and all placement fetch/mount/watch cases.

## Environment and ownership boundaries

Use the [environment contract](../design/setup.md#environments) uniformly:
root commands resolve `ROOT/.env` or caller defaults. Agent commands receive
`keep(root env)` plus one group: `AR/.NAME.env`, otherwise `AH/.env`, otherwise
`pick(root env)`. Higher-priority groups replace lower ones, even when empty.
Sandbox startup computes `guest.env` and mounts it at `GR/.NAME.env`; `_serve`
uses the ordinary loader without an environment-file argument. Selection remains
startup-only; credentials stay out of shared State caches.

Catalog selection reuses existing root/home mount coverage, including explicit
selections. Only uncovered files need an independent read-only mount at
`GR/.inputs/catalog.json`; the guest uses that mapped path.
A guest whose root config snapshot differs from newer shared State rejects
preparation with a restart diagnostic, preserving its accepted State and shared
pointers. Direct guest commands enforce this through `GR/source.json` too.

`/tmp/toolang/R8/root.json` (visiting) and `DIR/.toolang/R8/root.json` (roaming)
bind the full canonical ROOT. Host discovery stays at
`ROOT/.runtime/active/ADDRESS/info.json`; Hub owns `ROOT/hub/pid.json`.
These records are outside State/Setup watch and are not mounted as host ownership
records. Preserve host ROOT, R8/P10/P19, source identity, and launch identity;
never derive host codes from GR. Bare roaming allocates P19; explicit `@P19`
reuses it; every startup has a new `launch_id`. No State pointer lives in runtime.

Coordination keeps `AR/.state/root/edit.lock`, `AH/.state/home/edit.lock`, layer
`v24/build.lock`, and agent `v24/publish.lock`; lock events are not source/pointer
invalidations. Startup fetch acquires root edit, then home edit, so Program updates
serialize with home authoring. Roaming allocation owns host-side `AR/.state/launch.lock`.

## Acceptance and scope

Verify all placements on host and guest: no-op/new/reused fetches, visiting cache
freshness, ownership conflicts, local authoring/sync, home config/catalog writes,
root snapshot restart, all three external pointers without source edits, absent/nested directories and
subscription races, noise-independent checks, concurrent preparation and runtime
history isolation, and atomic Program/config/catalog replacement without rollback.
Verify nearest-only TOML selection with three ancestor candidates, empty/invalid
nearest TOML, origin-sensitive relative paths without ancestor workspace grants,
no-op linking, upstream atomic replacement retaining old inputs, Program relinking
visible to existing guests, root config/catalog relinking visible after restart,
shared in-place writes, and clear hardlink-support/
cross-filesystem failures. Cover Program, TOML, and catalog hardlinks.
Docker filesystem probes cover atomic file replacement within stable directories
and uniform home/runtime mounts. A catalog hardlink probe covers guest visibility,
upstream atomic replacement, relinking, and shared writes. Application acceptance
covers shared State and running guests after relinking.

Touchpoints: `common/layout.py`, source/environment and CLI orchestration,
State/Setup source discovery/watch, guest mounts, and design docs. Risks: source
ownership races, subscription gaps, mount-anchor replacement. Preserve public
CLI/ID formats, environment policy, placement lifetimes, and Setup storage policy;
exclude legacy lookup/migration and pruning. Run [default checks](../../CONTRIBUTING.md).
