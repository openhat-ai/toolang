# Hide the Implicit Lab Directory

Status: Approved in the 2026-10-15 implementation request, with compatibility and
migration explicitly excluded.

## Goal and scope

Keep generated workspace files out of an ordinary `tree` of agent home by backing
the implicit `lab` workspace with `<agent home>/.lab`. Preserve the workspace name
and `lab://` URIs. This supersedes the physical directory decision in
[the original lab plan](rename-implicit-scratch-workspace-to-lab.md).

## Design

- Define the physical directory separately from `IMPLICIT_WORKSPACE_NAME`; use it
  for scratch directory preparation and local workspace grants.
- Preserve workspace ordering, configured-name collision handling, workdir
  selection, directory validation, and per-agent isolation.
- Hosted execution mounts the new host directory using the existing workspace
  mapping rules; the ordinary guest mount remains `.workspaces/lab`.
- Add no compatibility aliases, migration, fallback to the old directory, or new
  configuration. Existing `lab/` data is outside this change.

## Implementation touchpoints

- `src/toolang/common/layout.py` and `src/toolang/setup/types.py` own the physical
  root; `src/toolang/up/mounts.py` consumes the scratch preparation helper.
- Update physical-path fixtures and expectations in layout, setup, mounting,
  workspace inspection, CLI, and execution tests.
- Update current workspace documentation and the changelog.

## Acceptance criteria

- Preparing a workspace creates or reuses `.lab`; local `lab://` writes land
  there, and configured `lab` entries cannot override it.
- Hosted mount capture maps the `.lab` host root to the existing guest workspace
  location. Missing directories, files, and symlinks retain their current rules.
- Workspace declarations, saved URI workdirs, ordering, and default selection
  retain their existing values. The default offline verification passes.
- Ordinary `tree` hides the new directory; explicit `tree .lab` can inspect it,
  while `tree -a` includes hidden files as usual.

## Risks and open questions

Absolute paths to the previous directory change; compatibility and migration are
intentionally excluded. Existing `lab/` directories remain visible until the user
handles them. There are no open design questions for this scope.
