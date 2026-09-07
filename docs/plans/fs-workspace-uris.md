# Filesystem workspace URIs

## Goal and scope

Let every fs operation address configured workspaces through
`workspace://<name>/<path>`, including roots outside agent home. Remove implicit
home and working-directory access; agent-state operations belong to `me`.
Keep `workspace` plus root-relative `path` as an alternative spelling with the
same grants and URI results. Temporary cwd workspaces, shell, mounts, runspaces,
State adoption policy, and record schemas are out of scope. Reserve the full
`runspace://` scheme for later; it is not supported by this change.

## Contract

- Match workspace names exactly. An omitted path means `/`. Decode UTF-8 percent
  escapes once; encode returned paths canonically. Reject malformed escapes,
  query strings, fragments, and a simultaneous `workspace` argument.
- `fs.list("workspace://")` lists names, root URIs, and availability from the current
  publication, without exposing physical roots. Other operations require a name.
- Resolve within the selected existing workspace directory. Reject traversal and
  symlinks escaping it. Do not create unavailable workspace roots or fall back to
  home. Never delete a workspace root.
- All operations return URI paths, including list/glob entries. Preserve file
  contents. Glob patterns cannot escape the selected subtree; traversal must not
  follow directory symlinks. Listing must not inspect targets outside the root.
- Publish stable fs-specific protocol when fs tools are available, independently
  of authored instruct and runtime-tool availability. Discover the changing
  workspace list through fs.list rather than a static catalog.

## State and rules

Each Tool Step uses its captured StatePublication for discovery, resolution, and
execution. Already-started operations retain their prepared paths; later Steps
see adopted additions, removals, and remapping. Workspace publications may share
the same durable State revision: do not cache mappings by revision.

Keep workspace name and relative path through preflight and honor. Authorize
all rule reads against the selected workspace, without an additional home grant.
Each honor/retry Step resolves against its own publication. Persist recalled text
through existing controls; no new State reader, control, or assembly mechanism is
needed.

## Touchpoints and acceptance

- `base/utils/workspace_paths.py`: URI codec and explicit workspace-root resolution.
- `plugin/toolsets/filesystem.py`: preparation, namespace listing, bounded
  enumeration, and URI results; retain the existing tool factory.
- Executor rules/honor and protocol preparation: external workspace rules and
  model-facing path contract.
- Unit tests: all fs operations, encoding, root availability, traversal,
  symlinks, glob, output round trips, no implicit home access, and unchanged
  shell path preparation.
- Integration tests: external rules and retries; same-revision publications;
  add/remove/remap within a Run; updates during an operation and between honor
  and retry; parallel children and recorded-call replay.
- Run Ruff, formatting, ty, and the complete offline pytest suite.

## Risks

This is tool-level path authorization, not an OS sandbox. Preserve existing
prepared-operation semantics without claiming protection against adversarial
concurrent filesystem mutation. A stale model-visible listing is not authority:
every new call resolves against its Step publication.
