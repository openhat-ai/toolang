# Rename the Implicit Scratch Workspace to `lab`

Status: Approved by PR #624 (merged 2026-09-29).

## Goal

Make `lab` Toolang's implicit scratch workspace identifier and use `<agent home>/lab` as
its backing directory in local runs, hosted sandboxes, and model-facing declarations. Do
not migrate existing scratch data.

## Current behavior

Toolang creates an implicit scratch directory at `<agent home>/.tmp`, exposes it under the
workspace name `tmp`, and lists it before configured workspaces. A configured `tmp` grant
cannot replace that implicit grant. The initial workdir is the last available workspace;
therefore it is the implicit workspace only when there are no other available workspaces.
Hosted mounts, workspace declarations, and persisted workdir controls also use the current
implicit workspace name.

## Design

- Define the implicit workspace identifier once as `lab`. Use it for grants, host/guest
  mount capture, available-workspace lists, initial-workdir validation, invalidated-workdir
  recovery, and protocol examples. Rename the filesystem helper to a name independent of the
  former identifier, such as `ensure_scratch_workspace`, and have it create and validate
  `<agent home>/lab`.
- Do not migrate, inspect, authorize, copy, rename, merge, overwrite, or delete the existing
  `<agent home>/.tmp` directory or its contents. The implicit grant is only
  `<agent home>/lab`; the old directory remains untouched.
- Preserve current workspace ordering and initial-workdir selection: `lab` is first in the
  usable workspace list, configured workspaces follow in their existing order, and the last
  available workspace remains the initial default when additional workspaces are present.
- Reserve `lab` for the implicit grant; a configured `lab` entry cannot replace it. Do not
  provide an implicit URI alias for the former workspace name. A configured workspace using
  that former name follows ordinary configured-workspace behavior. Previously recorded
  workdirs using the former name resolve only if that ordinary configured workspace is
  currently available; otherwise existing invalid-workdir/default-workdir fallback applies.
- Remove references to the former implicit workspace name from current user-facing
  workspace documentation and examples. Do not rename unrelated operating-system `/tmp`
  paths or Python `tmp_path` test fixtures.

## Scope

This changes the implicit workspace identifier and physical directory and updates their
implementation, tests, and documentation. It does not migrate existing scratch data,
change workspace ordering, initial workdir selection, authorization rules, or generic
temporary-directory behavior. It adds no legacy URI alias or configuration setting.

## Likely files

- Implementation: `src/toolang/common/layout.py`, `src/toolang/state/prepare.py`,
  `src/toolang/setup/types.py`, `src/toolang/up/mounts.py`,
  `src/toolang/execution/executor/executor.py`, and the related fallback comment in
  `src/toolang/execution/records.py`.
- Runtime protocol and docs: `src/toolang/execution/assembly/prompts/protocol.md`,
  `docs/tools.md`, `docs/executor.md`, `docs/program.md`, and
  `docs/plans/workspace-declaration-and-scratch.md`.
- Focused tests: `tests/integration/execution/test_run_workspace_location.py`,
  `tests/integration/execution/test_resource_visibility.py`,
  `tests/integration/cli/test_chat_local_execution.py`,
  `tests/integration/cli/test_chat_remote_execution.py`,
  `tests/integration/api/test_remote_chat_support.py`,
  `tests/integration/up/test_docker_sandbox_lifecycle.py`,
  `tests/unit/setup/test_types.py`, and `tests/unit/up/test_mounts.py`, plus any directly
  affected callers found during implementation.

## Acceptance criteria

- Local and hosted execution expose the implicit workspace as `lab`, backed by
  `<agent home>/lab`. The directory is created when absent; an existing path must be a
  directory and not a symlink. Existing per-agent isolation checks remain enforced.
- Preparation does not inspect or change `<agent home>/.tmp`. Tests prove that existing
  contents there remain untouched and are not implicitly accessible through `lab`.
- The usable workspace declaration lists `lab` first. With no configured workspaces, the
  initial workdir and invalidation fallback are `lab://`. With configured workspaces, the
  existing ordering and last-available-workspace selection remain unchanged.
- A configured `lab` entry cannot replace the implicit root. The former name is not an
  implicit workspace or URI alias; an explicit configured workspace under that name follows
  ordinary configured-workspace behavior.
- `lab://` resolves consistently in local tools and hosted sandboxes. Previously recorded
  workdirs using the former name fall back unless that name is currently available as an
  explicitly configured workspace.
- Current runtime protocol and user-facing workspace documentation identify `lab` as the
  implicit workspace and do not describe the former name as a workspace. Generic `/tmp`
  paths and `tmp_path` fixtures remain unchanged.
- Focused tests cover directory creation and validation, no-migration behavior, grant
  collision behavior, host/guest mappings, model-facing declarations, initial and recovered
  workdirs, and unavailable-workspace failure. The default code verification passes.

## Risks

- Existing scratch contents remain in `<agent home>/.tmp` and are not available through the
  new implicit workspace. Users who want those files in `lab` must move them themselves.
- Existing instructions or persisted workdirs that refer to the former implicit workspace
  name must be updated; absent an explicitly configured workspace under that name, saved
  workdirs use the existing fallback behavior.
- A configured workspace under the former name, previously ignored as a collision, becomes
  an ordinary configured grant. This may expose a path that was present in configuration
  but not previously available, so tests and release communication must call out the change.
- The new directory must preserve the existing per-agent isolation guarantees.

## Open questions

None for the proposed scope. The `<agent home>/lab` location, no-migration behavior, absence
of an implicit legacy alias, behavior of an explicitly configured former name, and unchanged
initial-workdir selection are deliberate decisions in this draft; approve or revise them
before implementation.
