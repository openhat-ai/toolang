# Define Phase 1 Flow Module Discovery And Next-Run Visibility

## Status

Approved.

## Goal

Discover `agents/<agent>/flows/<name>.too` as independent Toolang program
modules, validate them in layers, and publish them through the existing
immutable prepared-state pipeline.

After a valid file change, the next root run can list, select, and execute the
flow. An active root run and its descendants continue using the `AgentState`
captured at root acceptance.

## Success Criteria

- `agent.too` and every flow module use the same complete Toolang parse, lower,
  and semantic-validation rules.
- A flow file is a complete program, not a fragment, and receives no implicit
  declarations from `agent.too`.
- A flow module exports one flow whose authored name is either absent or
  exactly equal to the filename stem.
- An unnamed exported flow derives its public name from the filename, so
  renaming the file alone renames the public flow.
- Other declarations remain private to the flow module.
- Flow modules are captured in the immutable home prepared version and
  contribute to the `AgentState` fingerprint.
- A valid change is visible to executable listings and the next root run
  without a process restart or state-apply action.
- Invalid source, extension-contract errors, or public catalog conflicts leave
  the watcher's last valid `AgentState` active.
- Existing `agent.too` behavior and root-run snapshot isolation remain
  unchanged.
- `${TOOLANG_ROOT}/flows/` is not discovered in this phase.
- The default offline verification suite passes.

## Current Behavior

Home preparation parses one `agents/<agent>/agent.too` into
`AgentState.program`. Source scanning, prepared cache documents, runnable
resolution, executable listings, and execution all assume that one program.

`StateWatcher.refresh()` replaces its current state only after preparation
returns, but an invalid changed source currently escapes the watch loop instead
of becoming a recoverable rejected candidate.

Each accepted root run already receives an immutable `AgentState`, and its
children inherit that object. This phase preserves that rule.

## Scope

This phase includes:

- agent-home flow discovery;
- shared Program validation for `agent.too` and flow modules;
- flow filename and export binding;
- a merged public runnable catalog;
- home prepared-cache persistence and loading for flow modules;
- watcher last-valid publication and diagnostics;
- module-aware root-run resolution, input validation, and execution;
- Chat TUI, local, remote, and HTTP executable listings;
- next-root-run visibility; and
- documentation and tests.

This phase does not include:

- `${TOOLANG_ROOT}/flows/`;
- config-wired or `with`-referenced shared flow modules;
- agent-authored flow file tools;
- reserved `__toolang_*` internal model actions;
- mid-root state application or state controls;
- runnable catalog instructions for models;
- agic-produced child runnable calls;
- quoted dynamic Flow syntax;
- changes to `tree-sitter-toolang`;
- module imports or cross-module static types; or
- a flow marketplace or installation format.

## Terminology

This feature reuses the existing `root`, `home`, and `here` vocabulary only for
the meanings it already has. It does not use those values as program-module
kinds.

| Concept | Values | Meaning |
| --- | --- | --- |
| Program module kind | `agent`, `flow` | The role of `agent.too` or `flows/<name>.too` |
| Authored placement | `home` | Both Phase 1 module kinds live in one agent home |
| Cap runtime scope | `root`, `home`, `here` | Existing cap availability and precedence |
| `here` | current program module | Inline and referenced caps from whichever module is executing |
| Run root | root run | The root of an execution tree, unrelated to Toolang root placement |

The design uses `authored_path` for the home-relative authored file and
`prepared_path` for its immutable cached copy. It does not use a generic
`source_path` field for program-module identity.

The two module kinds are:

- **agent module**: the special `agent.too` program;
- **flow module**: one complete program under `flows/<name>.too`.

## Authored Layout And Discovery

The only new authored path is:

```text
${TOOLANG_ROOT}/agents/<agent>/flows/<name>.too
```

Only direct, case-sensitive `*.too` files are candidates. Nested files and
files with other extensions are ignored. Existing authored-source path and
symlink safety rules continue to apply.

`${TOOLANG_ROOT}/flows/` is deliberately ignored. Shared flows will later use
explicit attachment mechanisms analogous to cap wiring in `config.toml` and
module references through `with` declarations. That later design avoids an
implicit root-directory overlay and gives shared modules explicit identity and
provenance.

The public exports are:

| Module | Public runnables |
| --- | --- |
| agent module | all authored agics and flows, plus the existing implicit default agic when applicable |
| flow module | its single filename-bound entry flow |

The home source scanner includes the direct flow candidates in source metadata.
Addition, modification, deletion, and rename therefore change the home
prepared version. Root source scanning is unchanged.

## Flow Entry Binding

Toolang already accepts an unnamed declaration such as `flow:` and currently
gives it the module-local fallback name `main`. Phase 1 preserves whether a
Flow name was explicitly authored while retaining existing local Program
semantics.

For `flows/research.too`, the extension contract selects exactly one entry:

| Authored declarations | Result |
| --- | --- |
| one unnamed `flow:` | valid; public name `research`, module-local name `main` |
| `flow research:` | valid; public and module-local name `research` |
| only `flow other:` | invalid; no entry matches the file |
| both unnamed `flow:` and `flow research:` | invalid; entry is ambiguous |

An unnamed entry keeps its module-local fallback name so existing static
validation remains unchanged. The public catalog stores `research` separately
from the declaration's local name. Renaming `research.too` to `report.too`
therefore changes only the public name.

Other valid agics and flows are private helpers. They may be referenced by
static statements within the same module but never appear in public listings.

No `tree-sitter-toolang` grammar change is required. Lowering and AST
serialization must preserve whether the optional Flow name was authored rather
than inferring it from the normalized `main` name.

## Prepared Program Modules

A prepared module contains enough information to execute without reading
authored source:

```text
PreparedProgramModule
  identity          stable agent or flow module identity
  kind              agent | flow
  authored_path     path relative to the agent home
  prepared_path     path below the immutable prepared version
  digest            SHA-256 of authored bytes
  program           independently parsed Program
  export            public name and local declaration identity, if a flow module
  here_caps         inline and referenced caps owned by this module
```

`AgentState` gains the immutable module set and a derived public runnable
catalog. Existing `program` and `program_source` compatibility accessors keep
referring to the agent module while runtime call sites migrate to module-aware
resolution.

A public catalog entry identifies its public name, declaration kind, owner
module, and module-local declaration. Resolution returns that complete value,
not a bare `AgicDecl` or `FlowDecl`.

## Layered Validation

Candidate preparation performs three ordered layers. A later layer does not run
while the preceding layer has errors. Diagnostics within one layer are sorted
by `authored_path` and source location.

### Layer 1: Program Validity

The agent module and every flow module independently use the same complete
Toolang parse, lower, and semantic validator.

The validator receives only that module's source. Consequently:

- local runnable references must resolve within the file;
- contexts, instructs, structs, and inline caps follow normal Program rules;
- duplicate declarations fail normally; and
- no declaration is borrowed from the agent module or another flow module.

The shared language path supplies the same defaults for both module kinds. The
only extra lowering fact is whether an optional Flow name was explicit, which
Layer 2 needs for entry binding.

Module `here` caps and `with` cap references are materialized only after the
Program passes language validation. A materialization failure is a module
preparation error and prevents publication.

### Layer 2: Flow Extension Contract

After Program validation, each flow module must satisfy:

- the filename is `<name>.too`, where `<name>` is a canonical runnable name;
- exactly one Flow declaration is either unnamed or explicitly named `<name>`;
  and
- an explicitly named entry matches the filename with exact case.

The selected declaration is the only public export. All other declarations are
private. A missing candidate, an explicit mismatch without an unnamed
candidate, or multiple candidates rejects the module.

### Layer 3: Home AgentState Composition

After every module passes Layers 1 and 2, home preparation merges the agent
module and all flow-module exports into one public catalog.

Public names must be unique across agics and flows. There is no module
shadowing. A collision, including one with the implicit default agic, rejects
the complete home candidate before its `current` pointer is published.

After Layer 3 succeeds, the root and home prepared values compose the new
`AgentState` normally.

## Cap And Type Isolation

The existing cap scopes remain authoritative:

1. `here`: inline or referenced by the currently executing module;
2. `home`: file-backed or wired for the agent home;
3. `root`: file-backed or wired for the Toolang root.

Both module kinds are authored in `home`, but that does not make their inline
or referenced caps `home` scoped. Those caps are `here` scoped and travel only
with their owner module.

Selecting a flow module changes the effective `here` layer to that module. Its
`here` caps override same-identity home or root caps only during that module's
execution and never leak into the agent module or another flow module.

Structs, contexts, instructs, and private runnables are also module-local. A
public flow's input is coerced using structs from its owner module. This phase
adds no cross-module static calls or shared custom-type namespace.

## Prepared Cache Changes

`HomePrepared` stores the agent module and flow modules. `RootPrepared` remains
unchanged and contains no program modules.

The immutable home snapshot contains:

```text
files/agent.too
files/flows/<name>.too
files/modules/<module-identity>/inline/...
files/modules/<module-identity>/cited/...
```

Generated `here` cap files are namespaced by module identity, so equal private
cap names in different modules do not collide.

Prepared document and version schemas are incremented together. Existing
version directories remain immutable. An old home cache is treated as a cache
miss and rebuilt from authored source; there is no in-place migration. If the
shared schema constant requires a mechanical root cache rebuild, root document
shape and behavior still remain unchanged.

The home `current` pointer is published only after all three validation layers
and complete prepared-version loading succeed.

## Watcher Publication And Diagnostics

`StateWatcher` owns:

- `current`: the last valid immutable `AgentState`;
- `diagnostics`: errors from the latest observed candidate.

A valid changed fingerprint atomically replaces `current`, clears diagnostics,
and yields the new state. An invalid changed candidate preserves `current`,
records diagnostics, logs the rejection, and keeps watching. Repairing the file
causes a later normal refresh.

If startup has no valid state to retain, preparation records the diagnostic and
fails startup.

Each diagnostic contains:

```text
layer          program | flow-extension | state-composition
module_kind    agent | flow
authored_path  home-relative path when available
line           optional one-based source line
code           stable machine-readable reason
message        concise human-readable error
```

Diagnostics describe the rejected candidate. They are not embedded in the last
valid `AgentState`, do not change its fingerprint, and need not be persisted
separately.

Normal watcher polling does not add remote refresh. A newly changed cap
reference keeps its existing materialization behavior.

## Next-Run Visibility

The process composition layer requests a non-forced watcher refresh before
resolving each new root-run request. The refresh uses the existing metadata
fast path and does not refresh unchanged remote refs. This closes the race
between a file write and the background notification.

If refresh publishes a valid state, that state is passed into `RunSpec`. If it
rejects a candidate, the request continues against the last valid state and the
diagnostic remains observable. A requested flow present only in the rejected
candidate fails as not found.

Child-run acceptance never refreshes the watcher. Every descendant retains its
root run's captured state. Mid-root changes belong to Phase 2.

## Module-Aware Runtime Resolution

The shared runnable resolver accepts `AgentState` or its public catalog and
returns the declaration together with its owner module and public name.

External references preserve existing forms:

```text
name
agic:name
flow:name
```

Kind-qualified references enforce kind. Unqualified references remain
deterministic because catalog composition prohibits collisions.

For a selected flow module:

- durable and public identity uses the filename-bound public name;
- input structs, directives, contexts, and instructs use the owner module;
- resource preparation applies that module's effective `here` caps;
- static Flow statements resolve only within the owner module; and
- private helper runs remain normal child runs but are not publicly resolvable.

Authored Flow statement syntax is unchanged in this phase.

## User Surfaces

All executable surfaces read the same public catalog:

- local Chat TUI `:agic`, `:flow`, and `:runnable`;
- remote Chat TUI executable lists;
- `GET /api/v1/agics` and `GET /api/v1/flows`;
- direct run, script, task, and chore request resolution; and
- runnable inspection.

Existing list response shapes remain unchanged. Private helpers are omitted.
Module metadata stays internal in this phase.

A user can add or rename a valid home flow file, select its public name in Chat
TUI, and execute it in the next submission. No new command is required.

## Durable Execution Boundary

This phase does not add root state controls or mid-run state heads. A root run
persists its normal canonical public runnable binding and immutable resource
snapshot. Its in-memory execution uses the captured module-bearing
`AgentState`.

Prepared versions remain on disk under current retention behavior. Exact
state-control pinning and changed-state retry reconstruction belong to Phase 2.

## Future Shared Flow Attachment

Phase 1 intentionally does not infer shared flows from `${TOOLANG_ROOT}/flows/`.
A later phase may attach shared flow modules explicitly through:

- root or home config wiring analogous to wired caps; and
- source-level `with` declarations analogous to referenced caps.

Those mechanisms should reuse the same Program validation, extension contract,
prepared module, public catalog, and `here` semantics defined here.

## Implementation Touchpoints

- `src/toolang/lang/ast.py`, lowering, and serialization: retain whether a Flow
  name was explicit without changing grammar;
- `src/toolang/state/source.py`: home flow scanning, authored capture, and
  source-path classification;
- `src/toolang/state/cache.py`: prepared module schema, loading, validation, and
  cache rebuild behavior;
- `src/toolang/state/prepare.py`: independent module preparation, layered
  validation, cap namespacing, and snapshot writing;
- `src/toolang/state/state.py`: module and public catalog vocabulary, `here`
  caps, and `AgentState` composition;
- `src/toolang/state/watcher.py`: last-valid state, diagnostics, and recovery;
- `src/toolang/execution/runnables.py`, `calls.py`, executor preparation,
  resources, and Flow child resolution: module-aware runnable ownership;
- local and remote Chat TUI executable providers and API agent routes;
- state, execution, CLI, API, and watcher tests; and
- prepared-state, program, caps, executor, and user documentation.

## Acceptance Tests

1. A valid home `flows/research.too` with `flow research:` is prepared, listed,
   and executable as `flow:research` in the next root run.
2. An unnamed `flow:` in `flows/research.too` is exported as
   `flow:research`; renaming only the file changes the public name.
3. The agent module and every flow module pass through the same language
   validator and receive the same local defaults.
4. A Program syntax or semantic error is reported at the program layer and
   prevents extension validation and home publication.
5. A valid Program with no unnamed or filename-matching Flow is rejected at the
   extension layer.
6. A module containing both an unnamed Flow and a filename-matching Flow is
   rejected as ambiguous.
7. Extra agics, flows, structs, contexts, instructs, and caps remain private and
   support static execution inside the exported flow.
8. A public name collision between the agent module, a flow module, another
   flow module, or the implicit default agic rejects home state composition.
9. Invalid changes retain the previous watcher fingerprint and catalog;
   diagnostics identify the layer and `authored_path`; repair publishes a new
   fingerprint.
10. Adding, editing, deleting, and renaming a direct home `.too` file changes
    the home prepared version and `AgentState` fingerprint.
11. Nested files, non-`.too` files, and `${TOOLANG_ROOT}/flows/*.too` are not
    discovered.
12. Prepared modules reload from immutable cache without reading or parsing
    current authored files.
13. Old prepared cache schemas rebuild without mutating existing version
    directories.
14. A top-level request refreshes local source before resolution, while an
    active root run and all descendants retain captured state.
15. Chat TUI and HTTP lists show the same public flows and omit private helpers.
16. Input coercion uses the selected module's structs, and static helper calls
    resolve within that module.
17. `here` caps from one module do not leak into another module and follow
    existing `here`, `home`, `root` precedence.
18. The default offline suite performs no new provider or unchanged-remote-ref
    calls.
19. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- `FlowDecl.name` currently normalizes an unnamed Flow to `main`; explicitness
  must survive AST serialization without changing existing agent-module
  semantics.
- Home prepared schema currently assumes one Program; loaders must reject
  partial module snapshots and rebuild old versions atomically.
- Many execution call sites accept a bare `Program`; incomplete migration could
  resolve input types, helpers, or `here` caps from the wrong module.
- First-time materialization of a cap referenced by a new module can fail after
  Program validation; it must reject the candidate and preserve active state.
- Refresh before root acceptance must reuse the metadata fast path and must not
  turn each chat submission into remote polling.

## Open Questions

None.
