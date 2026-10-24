# Consistent Docker mounts and runtime directories

Status: implemented; approved in conversation. Environment policy is separate.

## Goal and approved scope

Preserve host placement while giving every Docker guest the same filesystem
structure. Current contracts are documented once in:

- [Layout](../design/layout.md#agent-placement): host/guest ROOT, PLACE, HOME,
  lab, and per-address runtime records.
- [Hosting](../design/hosting.md#docker-filesystem): mounts, access modes,
  catalog scope, generated configuration, and guest bootstrap inputs.
- [Workspaces](../design/workspaces.md): ROOT/HOME precedence, captured grants,
  source-directory access, and host/guest path mapping.

The host owns registration and resource release. Guests update HOME status;
read-only active inputs use their own path view. Reuse an address directory only
after verified release, and fence cleanup against replacement launches. Preserve
history and lab. No old-layout migration or migration prompts are included.

Environment contents, selection, filtering, masking, and loading are outside
scope. Hub listener/networking, historical State cleanup, and atomic replacement
semantics for mounted config/catalog files are also outside scope.

## Acceptance and implementation

| Scenario | Pass condition |
| --- | --- |
| Placement | All guest homes work, including roaming without its source-directory grant. |
| Runtime views | Full address and generation agree; home/source paths match each reader; config and active mount sources are disjoint. |
| Grants | All placements create and mount lab at the documented place-relative path; nested directories and aliases retain distinct writable names; agent removal preserves lab. |
| Configuration | Exact ROOT name/ref/path selection, HOME fallback, State reload, and guest paths agree without rewriting host files; CLI additions persist absolute host paths. |
| Catalogs | Covered catalogs reuse their mounts and owning scope; external files retain their basename. |
| Recovery | Failed/stopped workloads release only owned inputs; uncertain resources remain recoverable; stale cleanup preserves replacements. |
| Boundaries | Environment policy, Docker ownership, shared source locks, output, and history retain their behavior. |

Touchpoints: `up/{mounts,records,registry,discovery,watcher,sandbox}.py`, common
layout, Setup grants, sandbox request types, Docker adapter/bootstrap, and serve
bindings. Verify focused unit/integration tests, the default suite, and an
isolated Docker mount/lifecycle probe.
