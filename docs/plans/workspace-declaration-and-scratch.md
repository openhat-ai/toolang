# Workspace Declaration and Workdir

Status: Draft for approval on 2026-09-27. Refines
[shell-workspace-scoped-access.md](./shell-workspace-scoped-access.md).

## Goal

Make workspace availability, workdir initialization, and path syntax consistent across
runtime declarations, Chat sessions, and tools.

## Design

- The model-facing concepts are **workspace** and **workdir**. `cwd` is only shorthand for
the current workdir, not a separate concept.
- Paths have three forms: OS-absolute paths inside an authorized workspace;
  relative paths resolved from the current workdir and confined to its workspace; and
  `workspace://path`, where `workspace://` means that workspace's root.
- Every Model Call carries one `<toolang:workspace list="lab,repo1,repo2"/>` and one
  `<toolang:workdir path="..."/>`. The list contains only usable workspace names, starts
  with `lab`, then preserves workspace configuration insertion order. The last listed name
  is the runtime default; do not explain that selection rule in the protocol. Merge names
  with first-wins semantics, so a configured `lab` entry is ignored.
- `lab` is an implicit workspace rooted at `<agent home>/lab`, not stored in config. The
  runtime creates and mounts it. Runs fail preparation if `lab` cannot be made usable.
- A Chat session's workdir is memory-only. At session creation, seed it from the selected
  thread's latest root Run final workdir, or from the runtime default if there is no root
  Run. `/cd` changes the session setting. `:workdir` overrides one Run's initial workdir.
  Run completion updates the session setting to that Run's final workdir.
- A Run records its initial workdir and applied `_toolang.chdir` transitions in controls.
  A later Chat session can therefore seed from durable root Run controls without persisting
  a session ID or session setting.

## Modules: current state and required changes

### Protocol and message assembly

**Current:** `protocol.md` describes the `workdir` tag as a “Current Run directory,”
spreads path guidance across rules, and exposes one `workspace-access` tag per workspace.
`prompting.py` injects the current `<toolang:workdir/>` on each Model Call.

**Modify:** Define workspace, workdir, and the three path forms once in `protocol.md`.
Describe `workspace list` as the current usable-name list and keep default selection out of
protocol. Keep `workdir path` as the current Run workdir. Change assembly to inject the
workspace list on every Model Call alongside workdir.

### Workspace config, State, Setup, and mounts

**Current:** Workspace grants and hosted workspace mounts include the implicit `lab` root at
`<agent home>/lab` before configured workspaces. Configured grants retain their configuration
order; a configured `lab` entry cannot replace the implicit grant.

**Modify:** Preserve config insertion order; produce one ordered usable-name list with
implicit `lab` first and configured workspaces after it. Use that list for the workspace
declaration and runtime default; ignore any later configured entry also named `lab`. Add
`<agent home>/lab` as an implicit grant without writing config, create it during agent-home
preparation, and include it in host/guest root mapping and sandbox mount capture. Fail Run
preparation if it cannot be created or mounted.

### fs, shell, and runtime tools

**Current:** fs descriptions repeat path guidance, including a leading-colon workspace form
that the resolver rejects. Shell describes the “current Run directory.” `_toolang.chdir`
contains a misleading workspace-root example. `_toolang.workspaces` separately lists names
and availability.

**Modify:** Remove repeated fs path guidance; define accepted path syntax only in protocol.
Align fs/shell/chdir descriptions with workspace and workdir terminology, without embedding
path syntax. Remove `_toolang.workspaces`; the per-call workspace list provides usable names.

### Chat session and Run controls

**Current:** `SessionSetting` and `RunOverride` have no workdir. Chat has slash-command
infrastructure but no `/cd` setting. `RunControlPayload` stores the accepted `cwd`,
`ChdirControlPayload` stores applied changes, and `RunStore.current_cwd()` reconstructs a
Run's final value. New root Runs currently select a workdir only when exactly one workspace
is configured and available. Child Runs inherit the parent's current value.

**Modify:** Add a workdir field to the in-memory Chat `SessionSetting` and a per-Run workdir
override to `RunOverride`/request parsing. Add `/cd` to update the session setting and
`:workdir` to override a single Run. At Run acceptance, resolve override before the session
setting; initialize a new session from the selected thread's latest root Run final workdir,
otherwise from the runtime default. At Run completion, update the active session setting
from the accepted workdir plus applied workdir controls. Do not persist session state or a
session ID, and do not add an origin/cause field to Run controls. Task/chore root Runs use
their stable work thread's latest root Run workdir when available; otherwise use the runtime
default. Child, retry, and rerun behavior retains its current ownership/anchor semantics.

### Documentation and tests

**Current:** docs and tests encode the present sorted workspace list, single-workspace
initial selection, tool descriptions, and `workspace-access` declarations.

**Modify:** Update `docs/tools.md`, `docs/executor.md`, and `docs/program.md`. Add tests for
insertion ordering; `lab` creation and guest mounts; workspace/workdir tags per Model Call;
valid and invalid path examples; `/cd` and `:workdir` precedence; new-session seeding from
the latest root Run; in-session updates after `_toolang.chdir`; and task/chore continuity.
Update tests that assert removed tools, declarations, or wording. Preserve existing
honor/preflight rule behavior while adapting it to the workspace-list declaration.

## Acceptance criteria

- `lab` is usable in every supported placement and sandbox; failure to provision or mount
  it prevents Run acceptance. A configured `lab` entry is ignored and cannot replace the
  implicit root.
- The workspace list contains only usable names in the defined order; the final name is
  used as runtime default, but this rule is absent from protocol text.
- A Model Call receives the current workdir path and available workspace list; subsequent
  calls reflect `_toolang.chdir` changes.
- A new Chat session on a thread with root Run history seeds from the most recent root Run's
  final control-derived workdir. A thread with no root Run uses the runtime default.
- `/cd` affects future requests in its session only; `:workdir` overrides one Run; the
  completed Run's final workdir updates the active session. Session settings are not
  persisted or shared between live sessions.
- fs guidance contains no unsupported path form; valid paths work and a leading-colon form
  is rejected.
- `workspace-access` and `_toolang.workspaces` are no longer exposed; rule preflight retains
  its existing behavior.

## Risks

- Config insertion order determines the runtime default; adding a workspace can change the
  default for sessions without a prior root Run.
- An implicit `lab` grants default read/write access under each agent home; it must remain
  isolated per agent and must not be cleared while a Run may use it. A configured `lab`
  entry is ignored so it cannot change that root.
