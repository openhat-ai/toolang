# File lock simplification

Status: Original scope approved on 2026-10-12. SQLite initialization revision
proposed on 2026-10-13; awaiting human confirmation before implementation.

## Goal

Keep necessary cross-process locks and name them after the protected object in
its existing directory. No `.locks/` directory, hidden prefix, new locking
framework, or database/data migration. SQLite's own locks and third-party locks
are outside scope; remove Toolang's initialization sidecar as defined below.
CLI history paths are defined [separately](cli-input-history-layout.md).

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

### Initialize RunStore without a file lock

Remove `<db-name>.init.lock` creation/use and the `file_write_lock` import from
`execution/store.py`. Keep the connection's `RLock`, existing supported schema
versions, schema SQL, and public constructor unchanged. This revision covers
initialization only; do not change runtime transactions or chore scheduling.

Safety must cover all three process arrangements against one database:

| Arrangement | Minimum concurrent participants |
| --- | --- |
| Host only | Two independent host processes |
| Container only | Two independent processes inside one container |
| Host and container | Two host processes plus two container processes |

Use the production directory mount, with the database, `-wal`, `-shm`, and any
rollback journal shared together. Different host/guest paths must refer to the
same files, with compatible permissions, cross-boundary byte-range locks, and
coherent mmap access to the WAL index ([SQLite WAL contract](https://www.sqlite.org/wal.html)).
Mounting only the database or synchronizing separate file copies is insufficient.
Native Linux containers share the host kernel; macOS containers run in a VM,
so host-only tests cannot establish host/guest safety ([Docker Desktop architecture](https://docs.docker.com/desktop/features/networking/)).
Validate the actual engine, file-sharing backend, mount options, and SQLite builds
on Linux and macOS. A returned `wal` mode alone does not prove these semantics.

Writable opening follows one sequence:

1. Enable connection-local foreign keys, then acquire `BEGIN IMMEDIATE` before
   any schema reads. Check `user_version` and, for version zero, whether user
   schema exists. Reject unsupported or nonempty unversioned databases before
   changing persistent state. Remove the duplicate pre-transaction validation.
2. Execute the existing schema creation/upgrade and set `user_version` in that
   transaction. Commit on success; roll back on every exception. Schema and
   version become visible together; another initializer validates the committed
   result after acquiring its own write transaction.
3. Outside the transaction, execute `PRAGMA journal_mode=WAL` and consume/close
   its result. Require the returned mode to be `wal`; another returned mode is
   an immediate `sqlite3.OperationalError`, never a silent fallback. Only then
   set `synchronous=NORMAL` and run the existing statistics backfill.

Schema-first ordering eliminates preflight reads and their race while preserving
rejection without database modification. WAL activation cannot run inside a
transaction ([SQLite journal mode contract](https://www.sqlite.org/pragma.html#pragma_journal_mode)).
If WAL activation fails after commit, the valid schema remains committed; close
the connection and report failure. A later open safely resumes at the same
sequence. Read-only opens keep their existing validation/query-only behavior,
without write transactions, WAL changes, backfill, or sidecar access.

Use at most one private, initialization-only SQL retry helper for acquiring
`BEGIN IMMEDIATE` and activating WAL. Each call has a 30-second monotonic deadline;
set SQLite's busy timeout to the remaining budget before each attempt. Retry
only `sqlite3.OperationalError` whose primary `sqlite_errorcode` is `SQLITE_BUSY`
(`code & 0xff`), with a fixed 10 ms pause capped by the remaining budget. On
expiry, re-raise the last busy error. Restore the normal 30-second busy timeout
after each call. SQLite supplies normal contention waiting; the small outer
loop covers early busy returns ([busy-handler contract](https://www.sqlite.org/c3ref/busy_handler.html)).
Do not retry `SQLITE_LOCKED`, schema errors, corruption, permissions, or I/O
errors; do not match exception text. Schema statements and commit retain
SQLite's normal busy timeout, with rollback on failure and no transaction replay.
No reconnect loop, backoff/jitter policy, new configuration, generic retry
framework, or replacement lock. The two admission/activation budgets are
separate; neither bounds schema execution or statistics backfill.

Implementation touches `src/toolang/execution/store.py`,
`tests/unit/execution/test_store_schema.py`,
`tests/integration/execution/test_multiprocess_scenarios.py`,
`tests/integration/up/test_docker_sandbox_lifecycle.py`, and
`docs/layout.md` (remove its initialization-lock row). Update the changelog
through the repository runnable when implementing. Leave existing `.init.lock`
files untouched; newly opened stores neither create nor depend on them.

Acceptance checks:

- In each arrangement, gated independent processes simultaneously open an absent
  database, upgrade a supported older schema, and reopen the current schema;
  all see the current version and WAL, existing records survive, and no init
  sidecar appears. Retain the thread test as additional coverage.
- A writer blocks transaction admission; a reader blocks DELETE-to-WAL
  activation. Release each through explicit test gates: opening succeeds after
  release, and persistent contention fails within its retry budget. Holding a
  legacy sidecar does not block opening.
- A controlled interleaving between openers cannot misclassify a newly initialized
  database as nonempty/unversioned. Unsupported databases retain their bytes,
  version, records, and journal mode; read-only opening remains non-mutating.
- Injected schema failure leaves no partial schema/version update; reopening
  succeeds. WAL failure after schema commit reports failure, preserves the
  committed schema, and permits a later successful open.
- Inject early/extended `BUSY`, timeout, non-`BUSY` errors, and a non-WAL pragma
  result: verify retry selection, bounded waiting, timeout restoration, and
  failure cleanup using a controlled clock rather than 30-second sleeps.
- In each arrangement, hold a write transaction and prove contenders cannot
  acquire one until release; test both host-to-guest and guest-to-host contention.
  Concurrent commits remain mutually visible with exact record counts and no
  lost/duplicate writes. Checkpoint while readers/writers are active, terminate
  a process with an uncommitted transaction, then reopen from both sides:
  committed records survive, uncommitted records are absent, and integrity checks
  pass. Synchronize through explicit IPC gates, without model-provider calls.

Container cases use the existing `live_docker` opt-in and production mount
construction; the default suite stays offline. Before completing implementation,
run the Docker cases on Linux and macOS with
`uv run pytest tests/integration/up/test_docker_sandbox_lifecycle.py -m live_docker --live-docker`
and record the environment above. Skips or host-only passes do not satisfy this
gate. If a required mount cannot provide SQLite's locking/mmap semantics, revisit
the storage design with the human before shipping lock removal; do not silently
exclude host/guest access or add a sidecar fallback. Neither an init sidecar nor
BUSY retries can repair incorrect runtime filesystem coordination.

Success requires a shorter initialization flow with one schema validation point
and no file-lock dependency. Risks are WAL conversion starvation under sustained
readers, a committed schema before WAL activation, and incompatible VM file
sharing; bounded failure and safe reopening are intentional, while cross-boundary
storage compatibility must pass the gate above. Upgrade all writers together
under the existing offline-upgrade rule. This revision needs human
confirmation before implementation. Run the default checks when implementing.

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
