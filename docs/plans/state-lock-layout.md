# State edit, build, and publish locks

Status: Approved on 2026-10-14; implementation accompanies this plan.

## Goal and scope

Use one edit lock per authored scope, create it only on the first write operation,
and name State locks by their responsibilities. Success means reads leave a fresh
scope free of edit locks, cooperating writers share exclusion, and State build
and publication retain their existing concurrency guarantees.

In scope: State file tools, authored/configured catalogs, workspace configuration,
roaming configuration projection, State lock names, and their tests and docs.
Out of scope: runtime, sandbox, Hub, SQLite, allocator, and compaction locks;
new batch-edit transactions; generic formatters, external editors, and initial
agent copying; changes to watcher scheduling, revisions, CLI flags, or schemas.
This supersedes only the authored sidecars and retained State lock names in
[file-lock-layout.md](file-lock-layout.md); its other decisions remain in force.

## Paths and responsibilities

```text
<root>/
  .state/root/
    edit.lock                 # Shared root source edits
    build.lock                # Root layer build and publication
  agents/<name>/.state/
    home/
      edit.lock               # This home's source edits
      build.lock              # Home layer build and publication
    agent/
      publish.lock            # Complete agent preparation and publication
```

Root caps and root `config.toml` share the root edit lock. Home `agent.too`,
runnables, caps, skill assets, chores, and `config.toml` share the home edit lock.
Catalog and CLI mutations must use the same paths as the State tools, including
read/modify/write configuration updates and generated roaming-home config writes.
Roaming `agent.too` continues editing its validated source target under its home
lock; edits to the source directory's shared `toolang.toml` use the roaming root
edit lock so different scripts sharing that file coordinate. Configuration
projection that makes no change must not create an edit lock.

Rename each layer's `prepare.lock` to `build.lock` and the agent's `check.lock`
to `publish.lock` in their existing directories. Preserve their current coverage:
build includes revision installation/repair and layer-current publication;
publish covers the entire preparation, composition, and agent-current publication,
not just the final pointer write. Cached layers still bypass the build lock.

## Edit lock contract

- Resolve concrete lock paths through layout-aware call sites and pass them into
  mutation stores. Keep path calculation in `common/layout.py`, filesystem locking
  in `common/files.py`, and parsing independent of State/runtime services.
  Preserve `file_lock_path()` as the generic `.lock` formatter for unrelated users.
- Writers create parent directories and the lock before state-dependent validation,
  digest/existence checks, or read/modify/write work, and retain it through the
  mutation. Failed or identical-content write attempts may leave the lock behind;
  "first write" means the first mutation attempt, not the first successful commit.
- Reads with an existing lock acquire it without creating or replacing anything.
  Reads with no lock read optimistically and check again afterward. If the first
  writer created the lock during that attempt, discard the result or transient
  I/O failure and repeat under the existing lock. Otherwise preserve the result
  or error. Apply this to State `list/get` and participating catalog/workspace
  readers; a State list holds one home lock for enumeration and all metadata.
- Reading must not create `.state` directories. Retain process/thread exclusion,
  reentrancy for nested store calls, atomic source writes, and digest mismatch
  behavior. Preserve existing permission/ownership and lock-wait reporting.
- Reject symbolic links and non-directory ancestors on edit-lock paths, and
  symbolic links or non-regular lock files, before following or writing them.
  Preserve the existing roaming-program exception and source-path checks.
- After release, retain the same lock inode. Never unlink, replace, or rename a
  live lock; cache cleanup must preserve the lock files and their directories.

## Watcher and ordering

The watcher does not acquire edit locks. Keep source observations, snapshot
verification/retries, and last-valid-State behavior as implemented in
[state/prepare.py](../../src/toolang/state/prepare.py) and
[state/watcher.py](../../src/toolang/state/watcher.py). Edit-lock activity must be
excluded from source manifests, revisions, and watcher invalidation.

Retain State acquisition order: agent publish, root build, then home build;
root and home build locks are released separately. Release edit locks before
requesting State refresh or synchronization. Operations touching both authored
scopes release root edit before taking home edit, then re-read home inputs; no
cross-scope edit transaction or edit/build/publish lock nesting is introduced.

## Implementation steps

- [x] Add explicit root/home edit paths and an existing-file-only acquisition/read
  helper in `common/{layout,files}.py`; keep the generic lock behavior unchanged.
- [x] Replace home collection guards in `execution/tools/state/{storage,handlers}.py`
  with the single home edit lock and lazy consistent-read behavior.
- [x] Update `catalog/{cap,chore,chores,config,manager,chore_authoring}.py`,
  `state/{config,state}.py`,
  and `cli/{caps/commands,toolang/commands/{workspace,chore,agent}}.py` to pass and
  share the correct lock, including nested reads and read-only construction sites.
- [x] Update roaming projection in `up/process.py` to share the home edit lock,
  recheck pending changes after acquisition, and leave no-change projections lazy.
- [x] Rename State lock paths and internal helpers in `state/{cache,prepare}.py`;
  retain coverage, acquisition order, progress, and watcher semantics.
- [x] Add the acceptance checks below; update [layout.md](../layout.md) and relevant
  State-lock references, and generate the implementation's canonical changelog
  entry with `too aide.too update_changelog` for maintainer verification.
- [x] Run the default offline verification and document the coordinated upgrade.

## Acceptance checks

1. Fresh scopes: repeated State/catalog/workspace reads, missing-file reads, and
   unchanged roaming projections create no edit lock or lock-parent directory;
   State preparation may independently create build/publish locks.
2. First mutation: each writer family creates only its scope's edit lock, leaves
   no authored sidecar, and retains a stable empty inode after success or failure.
3. First-writer race: deterministic process barriers start a write during an
   unlocked list/get; the reader retries under the new lock without a partial
   list, stale digest/content pair, or transient disappearance error.
4. Shared exclusion: State tools, CLI-backed stores, and configuration projection
   block on the same home lock across file categories; root writers share root
   exclusion. Competing digest-based updates yield one success and one mismatch.
5. Independence: another home remains editable while one home is locked; distinct
   root/home edits remain independent; shared roaming `toolang.toml` updates from
   different scripts do not lose changes. Nested store calls do not deadlock.
6. State coverage: same-agent processes serialize complete publication, different
   agents coordinate root builds, and cached-layer reuse remains unchanged; new
   build/publish names are used without creating legacy prepare/check names.
7. Boundaries: lock activity leaves source revisions unchanged; watcher refresh
   does not wait for edit locks and still detects concurrent source changes.
   Unsafe edit-lock files/ancestors cannot cause writes outside the owning scope;
   host/guest paths reach the same lock and retain existing owner inheritance.
8. Upgrade: pre-existing legacy sidecars are neither used nor modified, source and
   cached revision data remain loadable, and runtime/SQLite locks stay unchanged.

Implementation verification: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`, and `uv run pytest -n auto`; new concurrency tests use explicit
barriers/events and remain offline. Plan-only verification is source/link review
and `git diff --check`.

## Upgrade and risks

Stop all host/guest processes and writers sharing the affected root, upgrade
together, then restart. Mixed old/new versions cannot coordinate across renamed
locks and are unsupported. Leave old `agent.too.lock`, `runnables.lock`,
`caps.lock`, `chores.lock`, `config.toml.lock`, `toolang.toml.lock`, `prepare.lock`,
and `check.lock` untouched; optional manual cleanup requires the entire scope
offline. Do not migrate lock inodes or change revision formats.

Risks are missed writers, first-write/read races, shared roaming config ownership,
unsafe `.state` ancestors, and deleting persistent guards during cache cleanup.
The acceptance checks above cover these boundaries. External edits remain outside
advisory-lock coordination and rely on watcher source checks as before.

## Open questions

None. The human approved implementation on 2026-10-14.
