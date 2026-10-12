# Unified file lock layout

Status: Proposed; awaiting human approval. Definition only.

## Goal and scope

Move all Toolang-owned advisory lock files into an adjacent `.locks/` directory,
without changing lock granularity or acquisition behavior. Cover execution,
compaction, IDs, State, authored files, caches, runtime status, sandbox control,
and Hub hosting. Exclude SQLite/third-party files, cleanup commands, PID metadata,
and a new locking framework. Data formats and public commands stay unchanged.

## One path rule

All sidecars live directly in `<owning-directory>/.locks/`. Keep existing lock
basenames, removing the leading dot used by hidden sidecars. Do not add resource,
operation, or thread subdirectories.

| Existing lock | New location in the same owning directory |
| --- | --- |
| `runs.db.init.lock` | `.locks/runs.db.init.lock` |
| `runs.db.threads.lock` | `.locks/runs.db.threads.lock` |
| `runs.db.term_408kpc9t.compact.lock` | `.locks/runs.db.term_408kpc9t.compact.lock` |
| `.config.toml.lock`, `.caps.lock`, `.chores.lock` | `.locks/config.toml.lock`, `.locks/caps.lock`, `.locks/chores.lock` |
| `.agent.too.lock`, `.runnables.lock` | `.locks/agent.too.lock`, `.locks/runnables.lock` |
| `prepare.lock`, `check.lock` | `.locks/prepare.lock`, `.locks/check.lock` |
| `status.lock`, `state.lock`, `hub.lock` | `.locks/status.lock`, `.locks/state.lock`, `.locks/hub.lock` |
| `.<cached-document-name>.lock` | `.locks/<cached-document-name>.lock` |
| Data descriptor lock on `ids.json` | `.locks/ids.json.lock` |

Keep each existing owning directory from [the filesystem layout](../layout.md),
including runtime instances, State layers, and sandbox homes. Thread IDs remain
verbatim. Different stores retain their database-name prefixes. Catalog and home
writers must resolve the same lock addresses and retain their acquisition order.

Keep `common.files.file_lock_path(path)` as a pure one-argument formatter returning
`path.parent / ".locks" / (path.name + ".lock")`. Callers pass a concrete resource
or logical lock stem, such as `db_path.with_name(db_path.name + ".init")`;
no operation/key API or environment inference is needed. Preserve canonical
stems in the table, including `status`, `state`, and `hub`.

## Lifetime and upgrade

Create `.locks/` lazily, preserving existing owner inheritance and home-authoring
checks against symbolic links/nonregular paths. Locks must not enter authored
source manifests, fingerprints, or relevant watcher changes.

Preserve reentrancy, progress callbacks, async compaction cancellation and
`wait=False`, Hub lifetime exclusion, and existing lock ordering. Open/read/update/
close `ids.json` within its new sidecar guard. Do not hold SQLite transactions
while waiting for compaction.

Keep empty sidecars after release. Unlock and close the descriptor; never unlink,
replace, or rename the sidecar during normal operation. File existence, age, and
size do not indicate ownership. No automatic pruning is included.

Stop all writers sharing the affected storage, upgrade together, then restart.
Mixed old/new versions cannot coordinate through different paths. Leave legacy
locks untouched; optional manual cleanup requires the entire scope to remain
offline. No lock aliases, dual locking, or data migration are needed.

## Implementation and acceptance

Likely files: `common/{files,ids,cache}.py`, `catalog/{config,cap,chore}.py`,
`state/{config,cache}.py`, `execution/tools/state/storage.py`, `execution/store.py`,
`execution/executor/runs/compact.py`, `execution/compaction.py`, and
`up/{process,records,sandbox,hub}.py`. Update their relevant tests, `docs/layout.md`,
and the canonical changelog when implementing.

- Inventory: every owned lock uses the flat layout; shared writers agree and
  distinct stores/scopes remain separate.
- Concurrency: existing process exclusion and per-thread compaction parallelism,
  cancellation, nonblocking failures, and progress behavior remain intact.
- IDs: concurrent allocation remains unique after reopening existing state.
- Filesystem: release preserves the lock inode, legacy files remain untouched,
  ownership is correct, and home authoring rejects unsafe paths.
- Source stability: lock activity changes no source manifest or State revision.
- Upgrade: existing data works after all old writers stop and the new paths open.

Run default repository verification for implementation. Risks are missed writers,
mixed-version operation, `.locks/` ownership/path conflicts, and continued
per-thread file growth. No open design questions remain; human approval of this
definition is required before implementation.
