# Unified file lock layout

Status: Proposed; awaiting human approval. Definition only.

## Goal and scope

Give every Toolang-owned advisory file lock one predictable location and lifetime
without changing concurrency boundaries. Success means runtime directories no
longer mix data with lock sidecars, all cooperating writers resolve the same
lock, and existing exclusion and cancellation behavior remains intact.

Cover execution stores, thread compaction, ID allocation, State preparation and
checks, authored configuration/caps/chores/home tools, document caches, runtime
status, sandbox control, and Hub hosting. Exclude SQLite-managed journal/WAL
files, third-party locks, new cleanup commands, PID metadata, and acquisition
policy redesign. Data formats, public commands, and thread IDs stay unchanged.

## Path contract

Extend the pure `common.files.file_lock_path` formatter with explicit operation
and optional key arguments. Callers pass the concrete resource path; the helper
must not infer a home, runtime, or environment. The default operation is `write`.

| Lock | Canonical path relative to the resource's parent |
| --- | --- |
| Unkeyed | `.locks/<resource-name>/<operation>.lock` |
| Keyed | `.locks/<resource-name>/<operation>/<key>.lock` |

Use the full resource basename, including its extension. Resource, operation,
and key must be nonempty single path components other than `.` or `..`; reject
separators and invalid components before filesystem access. Keep thread keys
verbatim, including historical prefixes. Resource directories prevent collisions
between default and operation-specific locks. Different homes, roots, instances,
and databases retain separate namespaces.

| Existing purpose | New path example |
| --- | --- |
| Store initialization | `.runtime/.locks/runs.db/init.lock` |
| Thread mutations | `.runtime/.locks/runs.db/threads.lock` |
| Per-thread compaction | `.runtime/.locks/runs.db/compact/term_408kpc9t.lock` |
| ID state | `.runtime/.locks/ids.json/write.lock` |
| Runtime status | `.runtime/.locks/status.json/write.lock` |
| Root Hub lifetime | `<root>/.runtime/.locks/hub.json/write.lock` |
| Sandbox control | `<sandbox-home>/.locks/state.json/write.lock` |
| Authored configuration | `<home>/.locks/config.toml/write.lock` |
| Authored collections | `<home>/.locks/caps/write.lock`, `.locks/chores/write.lock` |
| Home program sources | `<home>/.locks/agent.too/write.lock`, `.locks/runnables/write.lock` |
| State preparation | `<state-layer>/.locks/state/prepare.lock` |
| Agent State check | `<agent-state>/.locks/state/check.lock` |
| Cached document | `<cache-parent>/.locks/<document-name>/write.lock` |

`state` is a virtual resource in each existing State directory. Keep the directory
ownership boundaries in [the filesystem layout](../layout.md); do not collect
locks into a global runtime directory. Catalog and home-authoring paths must
share addresses and retain the existing multi-lock acquisition order.

## Acquisition and lifetime

Create lock directories lazily before opening a sidecar. Preserve existing
owner inheritance for privileged writers. Apply home authoring's directory and
regular-file validation to the new lock ancestors and files; reject conflicting
files or symbolic links. Lock activity must not enter authored discovery,
fingerprints, or relevant watcher changes.

Keep existing acquisition mechanisms: synchronous reentrancy and wait progress,
State progress reporting, cancellable asynchronous compaction with `wait=False`,
and the Hub's nonblocking process-lifetime lock. Preserve per-thread compaction
granularity and avoid holding SQLite transactions while waiting.

Move the ID allocator's lock from the data descriptor to its canonical sidecar.
Open, read, update, and close the unchanged `ids.json` format inside that guard;
preserve allocation and failure behavior.

Sidecars remain empty and persistent. Release the descriptor's advisory lock;
never unlink, replace, or rename its file during normal release. Existence, size,
and age do not indicate ownership. Do not automatically prune per-thread locks:
unlinking can allow two processes to lock different inodes for one resource.

## Upgrade and risks

This is a coordinated namespace cutover. Stop every writer sharing an affected
root/home, including hosts, Hub, and sandbox clients; upgrade them together,
then restart. Mixed versions are unsupported because their locks differ. Do not
dual-lock or alias old paths. Leave legacy lock files untouched; optional manual
removal requires the entire affected scope to remain offline. A successful
nonblocking lock attempt alone is insufficient proof that removal is safe.

There is no data migration. Risks are missed writers, mixed-version concurrency,
new directory ownership/path conflicts, and continued growth of persistent
per-thread sidecars. Document these constraints with the implementation.

## Implementation touchpoints

- `common/files.py`, `common/ids.py`, and `common/cache.py`.
- `catalog/{config,cap,chore}.py`, `state/{config,cache}.py`, and
  `execution/tools/state/storage.py`.
- `execution/store.py`, `execution/executor/runs/compact.py`, and
  `execution/compaction.py`.
- `up/{process,records,sandbox,hub}.py`; their lock-path assertions and lifecycle tests.
- `docs/layout.md` and the canonical changelog for the implemented migration.

## Acceptance tests

- Path inventory: every Toolang lock uses the formatter; unsafe components fail,
  shared writers agree, and distinct resources/scopes never collide.
- Process exclusion: initialization, thread writes, State, authored writes,
  cache writes, sandbox control, and Hub retain their existing exclusion.
- Compaction: one thread serializes, different threads proceed independently;
  cancellation and nonblocking contention retain their existing outcomes.
- IDs: concurrent processes allocate unique IDs, including after reopening state.
- Filesystem lifecycle: release preserves the sidecar inode; legacy files remain;
  new directories inherit required ownership and home authoring rejects symlinks.
- Source stability: creating/acquiring locks changes no source manifest or
  published State revision and triggers no relevant authored refresh.
- Offline upgrade: existing data opens and operations succeed with the new
  namespace after all old writers stop.

Implementation must pass the repository's default verification. No unresolved
design questions remain; human approval of this definition is required before
implementation.
