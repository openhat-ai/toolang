# Unified file lock naming

Status: Proposed; awaiting human approval. Definition only.

## Goal and scope

Name each Toolang advisory lock after what it protects, next to its owning data.
Do not introduce `.locks/` or other directories. Cover execution, compaction,
IDs, State, authored files, caches, runtime status, sandbox control, and Hub
hosting. Exclude SQLite/third-party files, cleanup commands, PID metadata, and
acquisition policy redesign. Data formats and public commands stay unchanged.

## Naming and ownership

For a whole file, append `.lock` to its complete filename. For an operation,
append `.<operation>.lock` to the actual protected object. Include a database
name only when the protected operation is scoped to that database.

| Protected object | Lock filename in its owning directory |
| --- | --- |
| File contents | `config.toml.lock`, `agent.too.lock`, `ids.json.lock`, `<cached-document-name>.lock` |
| Authored collections | `caps.lock`, `chores.lock`, `runnables.lock` |
| Database initialization | `runs.db.init.lock` |
| Database thread mutations | `runs.db.threads.lock` |
| Thread compaction | `<thread-id>.compact.lock` |
| State preparation/check | `prepare.lock`, `check.lock` |
| Runtime status file | `status.json.lock` |
| Sandbox state file | `state.json.lock` |
| Hub process lifetime | `hub.lock` |

Keep existing owning directories from [the filesystem layout](../layout.md),
including agent/runtime instances, State layers, sandbox homes, and cache parents.
Remove leading dots used only to hide lock sidecars. Catalog and home-authoring
writers must share addresses and retain existing multi-lock acquisition order.

Compaction protects a thread, not a database file. Use the existing store's
parent directory as its runtime/storage scope and the validated thread ID
verbatim: `store.db_path.parent / f"{thread_id}.compact.lock"`. Do not include
`runs.db` or another database basename. The same ID in the same directory shares
one permit even across stores; different IDs or owning directories remain
independent. Historical thread prefixes remain valid.

Keep `common.files.file_lock_path(path)` as a pure one-argument formatter returning
`path.with_name(path.name + ".lock")`. File callers pass the actual file path;
collection/operation callers pass an explicit logical object stem, such as
`home / "caps"` or `runtime / f"{thread_id}.compact"`. Owners choose the stem;
the helper does not infer lock scope, operations, or environment.

## Lifetime and upgrade

Preserve parent-directory ownership, privileged writer inheritance, and existing
home-authoring checks against symbolic links/nonregular lock files. Lock activity
must not enter source manifests, fingerprints, or relevant watcher changes.

Preserve synchronous reentrancy and progress callbacks, async compaction
cancellation and `wait=False`, Hub lifetime exclusion, and lock ordering.
Open/read/update/close `ids.json` within its new sidecar guard. Do not hold SQLite
transactions while waiting for compaction.

Keep empty sidecars after release. Unlock and close the descriptor; never unlink,
replace, or rename the sidecar during normal operation. Existence, age, and size
do not indicate ownership. No automatic pruning is included.

Stop all writers sharing affected storage, upgrade together, then restart.
Mixed versions cannot coordinate where paths change. Leave legacy sidecars
untouched; optional manual cleanup requires the entire affected scope to remain
offline. No aliases, dual locking, or data migration are needed.

## Implementation and acceptance

Likely files: `common/{files,ids,cache}.py`, `catalog/{config,cap,chore}.py`,
`state/{config,cache}.py`, `execution/tools/state/storage.py`, `execution/store.py`,
`execution/executor/runs/compact.py`, `execution/compaction.py`, and
`up/{process,records,sandbox,hub}.py`. Update relevant tests, `docs/layout.md`,
and the canonical changelog when implementing.

- Inventory: every lock names its protected object in the owning directory;
  file locks retain complete filenames, with no new directories or hidden prefix.
- Shared writers: catalog/home writers and sandbox readers/writers agree;
  initialization and thread mutations remain database-scoped.
- Compaction: the same thread ID and directory serialize regardless of database
  name; different IDs/directories can proceed independently. Cancellation,
  nonblocking failures, and progress retain their existing outcomes.
- IDs: concurrent allocation remains unique after reopening existing state.
- Filesystem: release preserves the lock inode, legacy files remain untouched,
  ownership is correct, and home authoring rejects unsafe lock paths.
- Source stability: sidecars change no source manifest or State revision.
- Upgrade: existing data works with the new names after all old writers stop.

Run default repository verification for implementation. Risks are missed writers,
mixed-version operation, sidecar-name conflicts, and continued per-thread file
 growth. Same thread IDs in separate stores within one directory intentionally
share exclusion. No open design questions remain; human approval of this
 definition is required before implementation.
