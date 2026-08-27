# Define Durable Agent State Revisions

## Status

Approved.

## Goal

Make Agent State a durable, content-addressed runtime value. Every State
revision recorded by execution must resolve to one exact root/home layer pair,
and each layer must verify all files required to load it without reading
current authored source or contacting remote providers.

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
- A watcher publishes only fully persisted and validated Agent State and keeps
  the last valid revision when a changed candidate is invalid.
- Cap forms, serialized literals, and materialized directory names are exactly
  `authored`, `inline`, `configured`, and `referenced`.
- Program module names are stable, single-segment, and portable across Windows,
  macOS, and Linux.
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

## Layout

```text
<root>/.state/root/
  current
  revs/<root-revision>/
    layer.json
    files/
      config.toml
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...

<home>/.state/home/
  current
  revs/<home-revision>/
    layer.json
    files/
      agent.too
      config.toml
      flows/<flow-name>.too
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...
        inline/<module>/<kind>/<name>/...
        referenced/<module>/<kind>/<name>/...

<home>/.state/agent/
  current
  revs/<state-revision>/
    layers.json
```

Writer locks and temporary paths are implementation details, not durable
format. New code neither reads nor converts the former `.state/current` and
`.state/versions` layout.

## Canonical JSON And Revisions

`layer.json` and `layers.json` are stored as canonical UTF-8 JSON:

- object keys are lexicographically sorted;
- separators are `,` and `:` without whitespace;
- Unicode is not escaped unless JSON requires it;
- no indentation or trailing newline is written; and
- arrays without semantic order are sorted by stable domain keys before
  encoding.

A loader parses and canonically re-encodes each document and requires exact
byte equality. The revision is the lowercase SHA-256 hex digest of those exact
bytes:

```text
layer_revision = sha256(layer.json bytes)
state_revision = sha256(layers.json bytes)
```

No domain separator or separately constructed identity payload is used. The
schema and required document shape define the format.

## Layer Document

`layer.json` is the complete root or home identity document. It contains:

- `schema` and `scope`;
- the source snapshot used for change detection;
- configured and referenced cap resolutions;
- parsed config;
- root/home caps;
- home State modules and their here caps; and
- a sorted manifest of every file below `files/`, including relative path,
  byte size, and SHA-256.

The file manifest must exactly equal the recursive file set: missing,
additional, size-mismatched, or hash-mismatched files invalidate the layer.
Every cap, module, and resolution path must point to one manifest entry.

The four cap-form literals are used directly as directory segments. There is
no mapping to `file`, `wired`, `ref`, `cited`, or `with`. `declared_ref` stores
authored selectors and `resolved_ref` stores canonical resolved selectors.

## Program Modules

The special `agent.too` module is named `agent`. A direct
`flows/<name>.too` module is named `flow_<name>`. The module name is also its
single filesystem segment and is treated as an opaque label rather than parsed
from `_`.

Flow filename stems must match
`^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, must not be Windows reserved device names,
and must be unique under Unicode `casefold()`. The module name comes only from
the source path, not the agent header or the exported flow's local name.

Module-local cap paths are:

```text
files/caps/<inline|referenced>/<module>/<kind>/<name>/...
```

The public runnable catalog maps a public runnable to the opaque module name
and module-local declaration name. It remains derived and is not duplicated in
the layer document.

## Agent State Composition

`layers.json` contains exactly:

```json
{"home_revision":"<sha256>","root_revision":"<sha256>","schema":1}
```

The surrounding revision directory name is the SHA-256 of these exact bytes.
Loading first validates `layers.json`, then loads and validates the referenced
root and home layer revisions, and finally composes config, caps, modules, and
the public runnable catalog.

The Agent State store atomically writes a complete revision before publishing
`agent/current`. A record can therefore reference only a State revision whose
transitive content is already durable.

## State Watcher

On startup, the watcher attempts to load the new-format `agent/current` as its
last-known-good State, then prepares the current authored candidate. A valid
candidate is persisted and atomically published before replacing the in-memory
State. An invalid candidate records structured diagnostics and retains the
previous State. Startup fails when neither the current candidate nor a prior
new-format Agent State is valid.

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

- Canonical JSON is stable and rejects non-canonical bytes.
- Layer and Agent State directory names equal their document hashes.
- File manifests reject missing, additional, size-mismatched, and modified
  files.
- Root/home/agent revisions round-trip without authored or network access.
- Cap form literals equal their materialized directory segments.
- Module names and case-fold collision checks are portable.
- Concurrent writers publish one complete immutable revision.
- Invalid candidates retain the published Agent State and diagnostics clear
  after repair.
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
