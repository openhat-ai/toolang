# File lock simplification

Status: Approved on 2026-10-12.

Historical decisions below: authored/State guards are superseded by
[State lock layout](state-lock-layout.md), ID-file locking by
[SQLite ID allocation](sqlite-id-allocator.md), and runtime persistence/claims by
[unified runtime status](unified-runtime-status.md).

## Goal

Keep necessary cross-process locks and name them after the protected object in
its existing directory. No `.locks/` directory, hidden prefix, new locking
framework, or database/data migration. SQLite and third-party locks are outside
scope. CLI history paths are defined [separately](cli-input-history-layout.md).

## Rename and retain

| Current name | New name | Protects |
| --- | --- | --- |
| `.config.toml.lock` | `config.toml.lock` | Config read/modify/write |
| `.agent.too.lock` | `agent.too.lock` | Expected-digest check and source update |
| `.caps.lock`, `.chores.lock`, `.runnables.lock` | `caps.lock`, `chores.lock`, `runnables.lock` | Collection mutation and consistent reads |
| `<db-name>.<id>.compact.lock` | `<id>.compact.lock` | Thread compaction across model calls and publication |
| `status.lock` | `status.json.lock` | Conditional runtime-status update |
| `state.lock` | `state.json.lock` | Sandbox lifecycle and persisted reference |

Compaction uses the store's parent directory and validated thread ID verbatim.
The same ID in the same directory shares exclusion regardless of database name;
different IDs/directories remain independent. All runtime-status writers must
share its guard; startup currently bypasses the stop operation's lock.

## Use database transactions

Remove file guards around thread fork/rewind. Their store mutations already use
`BEGIN IMMEDIATE`; include dependent source/head reads in that same transaction,
including the path without a publication stream. Allocate IDs before entering
it and notify listeners after commit. Preserve request uniqueness, head checks,
and stream/event ordering; keep model/network work outside transactions.

## Remove unnecessary logic or locks

- Remove `ThreadManager.talk_thread` lookup/reuse. Its only production caller,
  `MessagingLoop.handle`, creates a fresh `talk` thread per handled batch.
  This eliminates `runs.db.threads.lock` entirely. Preserve checkpoint context
  (last 20 messages and previous result), cursors, runnable selection, replies,
  and old records. Full model history from the shared thread is no longer inherited.
- Remove the generic `store_document` cache-write lock. It constructs a complete
  envelope before atomic replacement, with no read/modify/write invariant; it
  currently has no production caller. Retain atomic replacement.

## Retain unchanged

| Lock | Why it remains necessary |
| --- | --- |
| `<db-name>.init.lock` | WAL activation precedes the write transaction; retain the guard rather than redesign initialization |
| `prepare.lock`, `check.lock` | State directory installation/repair and publication span multiple files |
| `hub.lock` | Excludes concurrent Hub processes for one root throughout their lifetimes |
| Descriptor lock on `ids.json` | Serializes allocator read/update of the same inode; no `ids.json.lock` sidecar is needed |

Preserve reentrancy, lock order, wait progress, compaction cancellation and
nonblocking contention, owner inheritance, and home-authoring path validation.
Sandbox locked loads continue waiting for lifecycle operations; explicit atomic
snapshots remain nonblocking. Retained sidecars persist after release: unlock and
close, never unlink/replace/rename them. Lock activity must not affect source state.

## Upgrade, verification, and risks

Stop all writers sharing affected storage, upgrade together, then restart.
Leave legacy sidecars untouched; manual cleanup requires the entire scope to
remain offline. Mixed versions do not coordinate where names change.

Implementation touches `common/{files,cache}.py`, `catalog/{config,cap,chore}.py`,
`state/{config,cache}.py`, `execution/{store,threads,compaction}.py`,
`execution/executor/runs/compact.py`, `execution/tools/state/storage.py`,
`work/messaging.py`, and `up/{process,records,sandbox,hub}.py`. Keep
`file_lock_path(path)` a pure formatter appending `.lock`; callers select concrete
file paths or logical object stems. Update relevant tests, [layout](../layout.md),
and the canonical changelog when implementing.

Acceptance: verify shared writer paths, transactional fork/rewind under
contention, fresh incoming-message threads with checkpoint context, readable
concurrent cache replacements, conditional status updates, per-ID compaction
exclusion/cancellation, unique IDs without a sidecar, unchanged source revisions,
lock inode/ownership safety, and offline upgrade. Run default repository checks.

Risks: missed cooperating writers, mixed versions, sidecar-name conflicts,
intentional compaction exclusion for matching IDs across same-directory stores,
persistent per-thread file growth, and reduced incoming-thread model continuity.
No open design questions remain; approval is required before implementation.
