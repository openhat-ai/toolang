# Consistent Docker mounts and runtime directories

Status: implementation approved in conversation; environment handling is owned
by a separate change.

## Scope and decisions

- Preserve host ROOT, PLACE, and HOME ownership and relative input filenames.
  Each guest hosts one agent with ROOT = PLACE at the configured guest root,
  defaulting to `/root/.toolang`. Canonical host identity and placement remain
  separate from mapped filesystem paths. Source-directory grants are optional.
- Reuse covering mounts for catalogs, keeping `catalog.json` in its owning scope.
  External selected catalogs retain their filename under
  `PLACE/.inputs/models/<path-hash>/`.
- Keep host and guest lab at `PLACE/workspaces/<agent>/lab`. Mount granted
  workspaces in that agent's guest workspace directory, independently of HOME
  and read-only input overlays. Every grant uses its existing unique name in
  the guest workspace namespace. Removing an agent preserves lab. Ignore old
  `HOME/.lab` without compatibility handling or migration messages.
- Read HOME `[workspaces]` and the matching ROOT `[workspaces.KEY]`, merging by
  name with ROOT precedence. KEY is the resident name, canonical visiting ref,
  or canonical roaming source path. Resolve relative paths at their declaring
  file and capture ROOT overrides in immutable State with reload support.
  Recommend absolute host paths in ROOT and HOME; CLI additions persist canonical
  absolute paths. Keep lab path derivation shared across preparation and hosting.
  Generate a guest `config.toml` using captured guest paths; preserve host source
  identities for Run authority and client compatibility.
- Mount the selected `ROOT/hub/` read-only at the corresponding guest path.
  Hub networking/listener changes are outside this implementation.
- One complete address owns `ROOT/.runtime/active/<address>/info.json`. Format 6
  stores `version`, `address`, absolute `home`, `launch`, `location`, and
  `sandbox`; source file/ref metadata is included when available. Derive the fixed status filename
  from HOME. Keep authoritative home status format 5 and legacy references
  readable without moving live resources.
- Docker stages guest configuration at `<address>/docker/config.toml` and its
  active inputs at `<address>/docker/active/`: `info.json`, `guest.env`,
  `guest.sh`, `guest.py`, and an optional development wheel. Mount only `active/`
  read-only as guest `ROOT/.runtime/active/<name>/`, and bind `config.toml`
  read-only at guest ROOT. Both info files carry the same launch and execution
  location, with paths expressed in their own
  filesystem. Source paths reuse existing grants; no new source-directory grant
  is implied.
- The host owns registration. Guests update home status without a writable root
  registry mount; host observation retires stopped registrations. Bootstrap
  inputs remain recoverable until container release. Reuse the fixed address
  directory only after verified release, and fence cleanup against replacement
  launches. Remove the address directory when its owned resources are gone.
- Preserve dotenv contents, source selection, filtering, masking, and loading.
  This change moves bootstrap inputs but does not implement the separate
  environment policy work. Historical State cleanup and single-file config/
  catalog atomic-write semantics are also outside scope.

## Acceptance and implementation

| Scenario | Pass condition |
| --- | --- |
| All placements | Host placement remains unchanged; each guest has ROOT = PLACE and correct HOME, including roaming without a source-directory grant. |
| Runtime views | Both info files retain the full address and locate their respective homes; guest active uses NAME. Config and active mounts have disjoint sources. |
| Workspaces | All placements create and mount lab at the documented place-relative path; named grants stay writable; nested and alias sources keep distinct names; removal retains lab. |
| Configuration | ROOT overrides select the exact name/ref/path; HOME supplies other grants; State captures, reloads, and preserves prior grants without reacting to unrelated ROOT Setup edits. Guest config contains mapped paths while Run/client authority retains host sources; CLI additions persist absolute host paths. |
| Catalog reuse | Selected catalogs keep their owning scope without additional home aliases. |
| Registration | Discovery recognizes current info and legacy references without duplicate membership; guests cannot write peer registrations. |
| Recovery | Failed startup and stopped workloads release owned inputs; stale cleanup cannot delete replacement metadata or bootstrap. Unknown resources remain recoverable. |
| Existing behavior | Env policy, Docker ownership labels, shared source locks, foreground/background output, and history remain unchanged. |

Touchpoints: `up/{mounts,records,registry,discovery,watcher,sandbox}.py`,
common layout, Setup grants, sandbox request types, Docker bootstrap/adapter, hidden serve
binding, and focused unit/integration tests. Run the default repository checks
and an isolated Docker mount/lifecycle probe before handoff.
