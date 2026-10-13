# SQLite ID allocator

Status: approved by the user for implementation on 2026-10-13. Rollout supports
fresh data only, with no legacy counter migration.

## Goal and scope

Replace `.runtime/ids.json` with a small allocator table in the same `runs.db`
used by execution history. A fresh runtime must allocate process-safe IDs without
creating a JSON snapshot or an allocator lock file.

Preserve the [ID model](../ids.md): codecs, agent keys, prefixes, `local`/`run`
families, hourly buckets, clock rollback, exhaustion, and duplicate checks.
Generated chore filenames continue to share the `local` sequence with threads;
name-derived chore IDs and backend conversation IDs remain unchanged.
No history reconstruction, counter import, new CLI command, or general storage
framework is included.

## Storage and allocation

`RunStore` owns database schema initialization. Name the table `seqs`, short for
sequences, consistent with `last_seq` and the existing short plural table names.
Add:

```sql
CREATE TABLE seqs (
    family TEXT PRIMARY KEY NOT NULL,
    last_tick INTEGER NOT NULL CHECK (last_tick >= -1),
    last_seq INTEGER NOT NULL CHECK (last_seq >= -1),
    CHECK (last_tick != -1 OR last_seq = -1)
);
```

Rows are created on first successful reservation, normally one each for `local`
and `run`. A missing row represents `AllocatorState()`. Validate stored state
before use; invalid values fail instead of resetting counters. No foreign keys
or cleanup cascade connects allocation state to history.

Keep codec and allocation behavior in `common/ids.py`. `allocate_id` accepts an
explicit database path, opens an existing database with `mode=rw`, and closes
its dedicated connection on every call. Under `BEGIN IMMEDIATE`, read the family
row, call the existing `reserve_next_id`, upsert the next state, and commit before
returning the ID. Retain the existing 30-second SQLite timeout. Errors roll back
and propagate; never fall back to another allocator.

Use `synchronous=FULL` on the allocation connection to preserve durable
reservations: SQLite's existing WAL/NORMAL execution setting can lose committed
transactions after a power loss ([SQLite documentation](https://www.sqlite.org/pragma.html#pragma_synchronous)).
Other execution connections retain their current settings.

Reservations commit independently of run/thread/chore creation. A later failure
leaves a gap and never returns an issued ID to the pool. All callers reserve
before opening their business write transaction, avoiding a second connection
waiting on a lock held by the same operation. Read-only inspection never opens
an allocation connection or initializes the table.

`IdIssuer(db_path, agent_name=...)` preserves `issue_run()` and
`issue_thread(prefix)`. Remove JSON snapshot persistence and POSIX allocator
locking, including `AllocatorSnapshot` and `AgentLayout.id_state`; retain pure
state and codec helpers. Composition sites pass `layout.run_store` and
`layout.name`, initializing `RunStore` first. Generated chore-name authoring
must initialize the database even when no execution has occurred yet; retain
its catalog lock and duplicate callback.

## Fresh-data rollout

Bump the writable execution schema from 56 to 57. Accept only an empty database
or schema 57 for writing; preserve existing read-only support for schemas 52–56
and add 57. An incompatible database must fail before mutation. Do not upgrade
an old database by creating an empty allocator table.

At writable runtime initialization, reject an adjacent legacy `ids.json` without
reading, importing, or deleting it. Report that the runtime requires fresh data.
Operators stop all writers, preserve the complete old runtime, and explicitly
start with a fresh runtime. Old and new writers must not share allocation state.
Existing IDs are not rewritten; resetting state does not guarantee disjoint IDs
if old and fresh runtimes with the same agent identity coexist.

## Touchpoints

- `common/ids.py`, `common/layout.py`, and `execution/store.py`.
- Issuer construction in `up/core.py`, `cli/common/execution.py`, local Chat,
  script execution, and `work/authoring.py`; existing runtime consumers reuse it.
- Affected test constructors and fixtures, allocator unit tests, store schema
  tests, chore authoring tests, and execution multiprocessing scenarios.
- Update `docs/ids.md`, `docs/layout.md`, and `docs/execution.md` for the final
  storage contract. Preserve historical plans and changelog entries; generate
  the new Unreleased compatibility entry through the changelog runnable.

## Acceptance and verification

| Scenario | Pass condition |
| --- | --- |
| Fresh initialization | Execution and generated chore-name creation initialize the full supported database; allocation creates family rows and no JSON file. |
| Existing ID behavior | Codec vectors, prefixes, family sharing, rollover, rollback, exhaustion, and duplicate checks remain unchanged. |
| Concurrent allocation | Multiple processes sharing one database issue unique run/thread suffixes with monotonic family state. |
| Failed creation | A committed reservation survives restart and downstream business rollback; the next allocation advances. |
| Failed reservation | Invalid state, callback failure, exhaustion, or SQLite failure returns no ID and leaves the prior state intact. |
| Transaction ordering | Fork, spawn, compaction, scheduling, and chore authoring reserve outside business write transactions and complete without self-lock timeout. |
| Legacy state | Old writable schemas and adjacent legacy JSON fail without modification; no import, deletion, or history scan occurs. |
| Read-only inspection | Supported old/new stores remain readable without initializing allocation state or changing database contents. |

Run focused tests, then the default ruff, formatting, ty, and offline pytest
checks. Review actual host/sandbox sharing where available; filesystem support
for SQLite WAL and its shared-memory files remains a deployment prerequisite.

## Risks and open questions

Allocation now competes with history and chore writes for the database write
lock. Keep reservation transactions short. Database loss or replacement loses
both history and counter state; deleting history rows must not reset counters.
Backing up only the main database file while WAL writes are active is insufficient.

No unresolved design choices remain.
