# Define Durable Agent State Revisions

## Status

Approved. Persisted formats and module layout follow the final
[State layer format](state-layer-format.md).

## Goal

Make Agent State a durable, content-addressed runtime value. Every State
revision recorded by execution must resolve to one exact root/home layer pair,
and each layer must support explicit verification of all persisted files
without reading current authored source or contacting remote providers.

The change also removes overloaded State vocabulary and reserves `cont` for
model-provider continuation data.

## Success Criteria

- `root`, `home`, `agent`, and `here` have one State meaning each.
- Root and home layers persist as canonical `layer.json` documents whose exact
  SHA-256 is their revision.
- Agent State persists as canonical `layers.json` whose exact SHA-256 is the
  State revision stored by execution records.
- Loading a revision never consults authored source, remote caps, or a
  `current` fallback.
- A watcher publishes only fully persisted Agent State and keeps
  the last valid revision when a changed candidate is invalid.
- Normal revision loading trusts persisted content; integrity validation is an
  explicit operation with no implicit runtime callers.
- Cap forms are `authored`, `inline`, `configured`, and `referenced`;
  materialized paths follow acquisition identity.
- Module identities derive from captured source paths independently of
  materialized directory names.
- Execution `state` means only Agent State revision; model continuation uses
  `cont` throughout code, protocols, and durable model-step data.
- The default offline verification suite passes.

## Scope

This change includes:

- State terminology, types, paths, schemas, and revision calculation;
- canonical root/home layer persistence and validation;
- canonical Agent State composition persistence and reverse loading;
- last-known-good watcher publication and diagnostics;
- a process-owned State access boundary for future internal `_me` tools;
- cap-form and module-name normalization;
- execution-record State revision validation; and
- a runs database migration from model continuation `state` to `cont`.

This change does not include:

- compatibility with or migration of the old `.state` cache;
- the public `agent_state` to `_me` toolset rename;
- authored flow tools, dynamic runnable calls, or `state_apply`;
- same-root-run State switching;
- tree-sitter changes; or
- independent `tasks/*.md` and `chores/*.md` in Agent State.

Old State caches are rebuildable and ignored in place. Existing runs databases
are not rebuildable and receive an explicit schema migration.

## Vocabulary

| Concept | Values | Meaning |
| --- | --- | --- |
| State layer | `root`, `home` | Independently prepared persistent inputs |
| Agent State | `agent` | One exact root/home layer composition |
| Cap scope | `root`, `home`, `here` | Availability with precedence `root < home < here` |
| Cap form | `authored`, `inline`, `configured`, `referenced` | How a cap enters State |
| Revision | lowercase SHA-256 hex | Immutable content identity |
| Version | positive integer | Document or database schema |
| Fingerprint | SHA-256 or metadata snapshot | Source-change detection only |
| Continuation | `cont` | Model/provider continuation data only |

`root` caps are available to every agent. `home` caps belong to one agent home.
`here` caps belong to the currently executing agent or flow module. Root and
home allow authored and configured caps; here allows inline and referenced
caps.

The prepared lifecycle remains a verb: authored source is resolved and
materialized to produce a State layer. It is not a persistent type prefix.

The principal type changes are:

- `RootPrepared` to `RootLayer`;
- `HomePrepared` to `HomeLayer`;
- `PreparedCap` to `StateCap`;
- `PreparedProgramModule` to `StateModule`;
- `PreparedVisibility` to `CapScope`;
- `AuthoredSource` and `AuthoredFile` to `SourceSnapshot` and `SourceFile`;
- `WiredCaps` to `ConfiguredCaps`; and
- `ResolvedFile` to `MaterializedFile`.

State values use `revision`, `root_revision`, `home_revision`, and
`revision_dir`. State has no `fingerprint` compatibility alias and does not
persist Toolang version or observation timestamps.

## Persistence and Modules

The [State layer format](state-layer-format.md) defines the current directory
layout, canonical documents, timestamps, revision identity, module/Cap records,
and offline integrity checks. It is the persistence contract for this plan.

State loads the exact root/home pair from `layers.json` and reconstructs config,
capabilities, Programs, and public runnable indexes from captured JSON values.
The agent store installs a complete revision before publishing `agent/current`;
re-publishing the current revision is a no-op. Execution can therefore reference
only a State revision whose transitive content is already durable.

## State Watcher

On startup, the watcher attempts to load the new-format `agent/current` as its
last-known-good State, then checks the current authored candidate. A valid
candidate is persisted and atomically published before replacing the in-memory
State. An invalid candidate records structured diagnostics and retains the
previous State. Startup fails when neither the current candidate nor a prior
new-format Agent State is loadable.

The watcher owns one filesystem monitor and one serialized check/publication
path. File events and timeout checks use that path. `refresh()` requests one
additional check and waits for that request's check to finish; concurrent
refreshes are serialized and each receives a later completed check. Internal
`current` publication does not retrigger preparation when the published
revision and checked source metadata are unchanged.

The full check/publication transaction holds a per-agent file lock across
processes. Different agents remain independent and contend only on the root
layer writer lock when root source actually requires rebuilding. Layer writer
locks remain scoped to root or one home. Locks are acquired in the fixed order
agent check, root layer writer, then home layer writer.

The watcher exposes process-owned `current`, `load`, `refresh`, and
`diagnostics` operations. Future `_me` tools will use this boundary, while
authored writes remain owned by catalog/source services. The generic external
tool context does not receive the watcher.

## Execution Records And Model Continuation

Preparation controls continue storing one `state` string. It is validated as
canonical lowercase SHA-256 and means only Agent State revision. Root runs and
their descendants use the root-bound revision until a future explicit State
application feature changes that rule. No duplicate State columns are added to
runs, threads, or steps.

All model/provider continuation values use `cont`:

- `ModelCall.cont`;
- `ModelCallResult.cont`;
- `ModelCallRefs.cont`;
- `ModelStepNoted.cont`; and
- `_AgicState.cont`.

The complete type name remains `ModelContinuation`. The run-store schema is
bumped and migrates only model-step `given` and `noted` continuation keys from
`state` to `cont`; preparation-control Agent State fields remain `state`.

## Acceptance Tests

- Canonical JSON is stable and explicit validation rejects non-canonical bytes.
- Layer and Agent State directory names equal their document hashes.
- Explicit validation rejects missing, additional, size-mismatched, and
  modified files; ordinary revision loading does not hash them.
- Root/home/agent revisions round-trip without authored or network access.
- Cap form literals equal their materialized directory segments.
- Module names and case-fold collision checks are portable.
- Concurrent writers publish one complete immutable revision.
- Invalid candidates retain the published Agent State and diagnostics clear
  after repair.
- The watcher has one monitor and one serialized check path; `refresh()` waits
  for its own check and self-publication does not loop.
- Same-agent processes serialize complete checks while different agents retain
  independent check locks and share only root-layer preparation.
- New root runs see a newly published State while active run trees retain their
  bound revision.
- Execution rejects malformed State revision strings.
- The runs database migration changes model continuation keys without changing
  preparation-control State fields.
- Ruff, formatting, type checking, and the default pytest suite pass.

## Risks

- This intentionally cold-rebuilds every existing State cache after upgrade.
- Canonical JSON and array ordering become durable protocol behavior and need
  focused golden tests.
- Broad vocabulary changes touch state, catalog, execution, plugin adapters,
  docs, and fixtures; changes must remain mechanical outside the new storage
  and watcher behavior.
