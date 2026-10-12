# Unified file lock naming

Status: Proposed; awaiting human approval. Definition only.

## Goal and scope

Keep only necessary Toolang advisory locks and name them after what they protect.
Do not introduce `.locks/` or other directories. Cover execution, compaction,
IDs, State, authored files, caches, runtime status, sandbox control, and Hub
hosting. Exclude SQLite/third-party files, cleanup commands, PID metadata, and
a new locking framework. Data formats and public commands stay unchanged.

## Necessity audit

SQLite transactions protect database mutations; atomic replacement protects
readers from partial files. Neither automatically serializes a workflow spanning
multiple transactions, filesystem changes, or external calls. Keep a file lock
only where a cooperating cross-process workflow still needs exclusion.

| Current lock/use | Decision and verified reason |
| --- | --- |
| Database initialization | Keep: `_init_schema` activates WAL before `BEGIN IMMEDIATE`; transaction exclusion starts too late for concurrent openers. |
| Thread fork/rewind | Remove outer file guards: store methods already use `write_transaction` (`BEGIN IMMEDIATE`). Include dependent head reads in the same transaction. |
| Unique incoming Talk thread | Remove lookup/reuse and its guard: each incoming message-handling batch creates a new `talk` thread. No store-wide uniqueness contract remains. |
| Compaction | Keep: the permit spans history checks, model execution, and publication; a SQLite write transaction cannot remain open through this workflow. |
| ID allocator | Keep the existing descriptor lock on `ids.json`; `_write_snapshot` updates that same inode in place. Do not add a sidecar. |
| Config and home authoring | Keep: TOML read/modify/write and expected-digest checks followed by save must exclude other writers; atomic replacement alone permits lost updates. |
| Authored caps/chores | Keep: existence checks, creates/removes, skill-directory changes, and consistent chore listing span multiple filesystem operations. |
| State prepare/check | Keep: revision directory installation/repair and current-pointer publication form one workflow shared by multiple CLI/host processes. |
| Generic document-cache writes | Remove: `store_document` constructs a complete envelope before the guard and atomically replaces it, with no protected read/modify/write or cross-file invariant. No production caller currently uses it. |
| Sandbox management and state reads | Keep: start/stop/register/remove share lifecycle exclusion; locked loads wait for management completion while explicit snapshots remain nonblocking. |
| Runtime status | Keep and make all writers participate: stop performs conditional read/modify/write, but `write_runtime_state` currently bypasses its guard. Atomic replacement alone cannot protect that comparison. |
| Hub process | Keep: a process-lifetime guard prevents two live hosts for one root; database transactions cannot enforce this lifetime. |

The evidence is in `execution/{store,threads}.py`,
`execution/executor/runs/compact.py`, `common/{ids,cache}.py`,
`catalog/{config,cap,chore}.py`, `execution/tools/state/storage.py`,
`state/{config,cache,prepare}.py`, and `up/{process,records,sandbox,hub}.py`.
This is an implementation audit, not a claim that concurrency tests have run.

For fork/rewind, retain source validation, request uniqueness, head comparison,
commit/event ordering, and existing rejection behavior. Ensure all dependent
database reads are inside the mutation transaction before removing the outer
guard; keep model/network work outside transactions.

Remove `ThreadManager.talk_thread` and change its only production caller,
`MessagingLoop.handle`, to `create(prefix=ThreadPrefix.TALK)` for each handled
batch. This deliberately stops inheriting the shared thread's full model history.
Retain the existing per-conversation checkpoint context (last 20 messages and
previous result), cursor handling, runnable selection, and reply behavior. Do not
merge, delete, or migrate old threads/runs. This concerns agent incoming-message
execution, not the human Talk CLI's input-history files.

## Naming and ownership

For a whole file, append `.lock` to its complete filename. For an operation,
append `.<operation>.lock` to the actual protected object. Include a database
name only when the protected operation is scoped to that database.

| Protected object | Lock filename in its owning directory |
| --- | --- |
| Authored file contents | `config.toml.lock`, `agent.too.lock` |
| Authored collections | `caps.lock`, `chores.lock`, `runnables.lock` |
| Database initialization | `runs.db.init.lock` |
| Thread compaction | `<thread-id>.compact.lock` |
| State preparation/check | `prepare.lock`, `check.lock` |
| Runtime status file | `status.json.lock` |
| Sandbox state file | `state.json.lock` |
| Hub process lifetime | `hub.lock` |

Keep existing owning directories from [the filesystem layout](../layout.md),
including agent/runtime instances, State layers, and sandbox homes.
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
Keep ID reads/updates within the existing data-descriptor guard; no new
`ids.json.lock` is created. Do not hold SQLite transactions while waiting for
compaction. Remove cache and all `runs.db.threads.lock` acquisition, leaving any
old sidecars untouched.

Keep empty sidecars after release. Unlock and close the descriptor; never unlink,
replace, or rename the sidecar during normal operation. Existence, age, and size
do not indicate ownership. No automatic pruning is included.

Stop all writers sharing affected storage, upgrade together, then restart.
Mixed versions cannot coordinate where paths change. Leave legacy sidecars
untouched; optional manual cleanup requires the entire affected scope to remain
offline. No aliases, dual locking, or data migration are needed.

## Implementation and acceptance

Likely files: `common/{files,cache}.py`, `catalog/{config,cap,chore}.py`,
`state/{config,cache}.py`, `execution/tools/state/storage.py`, `execution/store.py`,
`execution/threads.py`, `execution/executor/runs/compact.py`, `execution/compaction.py`, and
`work/messaging.py` and `up/{process,records,sandbox,hub}.py`. Update relevant tests, `docs/layout.md`,
and the canonical changelog when implementing.

- Inventory: every lock names its protected object in the owning directory;
  file locks retain complete filenames, with no new directories or hidden prefix.
- Shared writers: catalog/home writers and sandbox readers/writers agree;
  database initialization remains database-scoped.
- Database controls: concurrent fork/rewind preserve invariants, head checks,
  request uniqueness, and commit/event ordering without acquiring file locks.
- Incoming messaging: separate batches create distinct `talk` threads without
  lookup/reuse or a thread sidecar; checkpoint context, cursors, and replies
  remain intact, and existing runs stay readable.
- Atomic cache: concurrent whole-envelope replacements remain readable and
  require no sidecar; no read/modify/write guarantee is introduced.
- Runtime status: conditional stops cannot overwrite a new registration;
  registration and stop use the same guard.
- Compaction: the same thread ID and directory serialize regardless of database
  name; different IDs/directories can proceed independently. Cancellation,
  nonblocking failures, and progress retain their existing outcomes.
- IDs: concurrent allocation remains unique after reopening existing state,
  with no new sidecar and no change to in-place persistence.
- Filesystem: release preserves the lock inode, legacy files remain untouched,
  ownership is correct, and home authoring rejects unsafe lock paths.
- Source stability: sidecars change no source manifest or State revision.
- Upgrade: existing data works with the new names after all old writers stop.

Run default repository verification for implementation. Risks are missed writers,
mixed-version operation, sidecar-name conflicts, and continued per-thread file
growth. Same thread IDs in separate stores within one directory intentionally
share exclusion. No open design questions remain; human approval of this
definition is required before implementation.
