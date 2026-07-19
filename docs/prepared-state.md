# Prepared State

Prepared state is an immutable, self-contained runtime snapshot derived from
authored source. `toolang.state` creates and loads snapshots; it never edits
program source, authored caps, or wired-cap declarations.
The Toolang root and agent home must already exist; catalog or caller code owns
their creation. State only writes beneath their `.prepared` directories. A
missing `agent.too` uses the existing in-memory default program and is not
created by state.


## Scopes

Preparation has two independently versioned scopes:

- `RootPrepared` contains root config and shared `PreparedCap` values;
- `HomePrepared` contains agent config, the parsed `Program`, and private
  `PreparedCap` values.

`AgentState` is the parsed `Program` plus the effective `PreparedCap` values
from one exact root/home version pair. It retains the three version identifiers,
but not the two cache objects or parsed config. Runtime configuration is loaded
through `toolang.config`. An executor takes an `AgentState` snapshot at the
beginning of each top-level run and keeps using that snapshot for the entire
run.


## Layout

Root caches live under `${TOOLANG_ROOT}/.prepared`. Home caches live
under `${TOOLANG_ROOT}/agents/<agent>/.prepared`.

```text
.prepared/
  current
  prepare.lock
  versions/
    <version>/
      source.json
      resolved.json
      prepared.json
      files/
        agent.too
        config.toml
        authored/
        inline/
        cited/
        wired/
```

Root versions omit `agent.too`. Directories that have no files may also be
absent.

`current` contains the hexadecimal version of the published prepared cache.
`prepare.lock` serializes writers for only that scope. Readers load immutable
version directories and do not hold the writer lock.


## Cache Documents

`source.json` is a coarse filesystem snapshot. It contains a sorted tree of
names, node types, modification times in nanoseconds, sizes, and children. It
does not classify paths as programs, caps, or skill directories. Directory
metadata detects additions, removals, and renames; file metadata detects normal
content changes. A change that deliberately preserves both size and mtime may
be missed, which is an accepted tradeoff for this fast check.

`resolved.json` records each `CapResolution` and the hashes of its materialized
files. Caps that require no remote resolution do not have a `CapResolution`.
A normal source check reuses existing prepared output. A wired ref
that names a mutable branch is therefore refreshed only when its authored
declaration changes or an explicit refresh is requested. The watcher performs
no implicit network polling.

Callers request that network refresh through `refresh_agent_state()`. Normal
startup and watcher updates use `prepare_agent_state()` and therefore reuse an
unchanged authored ref without contacting its remote source.

`prepared.json` contains the Toolang version, parsed config, parsed program AST
for home state, and serialized `PreparedCap` values. Cap bodies are not
embedded in this document; runtime consumers read the corresponding immutable
file only when needed. A new process can construct
`RootPrepared` or `HomePrepared` from the three cache documents and `files/`
without parsing
authored source again.

Loading validates document schemas, scope, version, and referenced file
existence and sizes. If the current version is incomplete or invalid,
preparation quarantines that directory and atomically recreates the same
content-addressed version. Quarantined directories are left for the future
cache garbage collector rather than deleted on the prepare hot path.

`files/` makes a prepared version self-contained:

- `authored/` copies authored cap files;
- `inline/` contains materialized caps declared inside `agent.too`;
- `cited/` contains materialized program references;
- `wired/` contains materialized wired caps;
- `agent.too` and `config.toml` preserve the scope inputs when present.

Each inline cap has its own materialized file so runtime consumers can read cap
content lazily without reparsing or reading the entire program.


## Versions

Root and home versions are SHA-256 digests over a domain separator plus:

- schema and scope;
- canonical `source.json` data;
- canonical `resolved.json` data.

The Toolang version is recorded as metadata in `prepared.json` but does not
currently participate in version calculation or cache invalidation.

The `AgentState` version is a separate SHA-256 digest over a domain separator,
the 32-byte root version, and the 32-byte home version. Root and home order is
significant.

Prepared versions do not share content-addressed objects. Identical files may be
duplicated between versions to keep storage and loading simple. Preparation
does not delete old versions on its hot path; a separate garbage collector may
later retain the most recent versions.


## Prepare and Publish

Preparation first compares the current version's `Source`. If it matches, the
prepared cache is reused without resolving remote references or reparsing
source. The Toolang version is currently metadata only, as described above.

When rebuilding, a writer:

1. acquires the scope's `prepare.lock`;
2. checks the current version again so concurrent processes reuse completed
   work;
3. scans source metadata and captures source files;
4. resolves and materializes referenced and wired caps;
5. parses config, program, and caps;
6. verifies that source metadata did not change during the build;
7. writes a complete temporary version and atomically renames it;
8. atomically publishes `current`.

Root and home locks are independent. Multiple foreground CLI processes and
background agents can prepare concurrently, while all agents safely share the
root cache.


## Watching

The state watcher observes root and home authored source plus both `current`
pointers. A relevant local change triggers preparation and publishes a new
immutable `AgentState` only when the resulting version changes. Changes inside
prepared version directories are not treated as authored source changes.
The watcher also performs the same metadata-only source check on its configured
local interval so a change in the startup-to-watch registration window, or a
missed filesystem event, is recovered. This local check reuses unchanged remote
materialization and is not remote polling.
