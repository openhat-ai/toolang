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

- root state contains root config and shared caps;
- home state contains agent config, `agent.too`, and private caps.

`AgentState` is one exact root/home pair. An executor takes an `AgentState`
snapshot at the beginning of each top-level run and keeps using that snapshot
for the entire run.


## Layout

Root generations live under `${TOOLANG_ROOT}/.prepared`. Home generations live
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
        referenced/
        wired/
```

Root generations omit `agent.too`. Directories that have no files may also be
absent.

`current` contains the hexadecimal version of the published generation.
`prepare.lock` serializes writers for only that scope. Readers load immutable
generation directories and do not hold the writer lock.


## Generation Documents

`source.json` is a coarse filesystem snapshot. It contains a sorted tree of
names, node types, modification times in nanoseconds, sizes, and children. It
does not classify paths as programs, caps, or skill directories. Directory
metadata detects additions, removals, and renames; file metadata detects normal
content changes. A change that deliberately preserves both size and mtime may
be missed, which is an accepted tradeoff for this fast check.

`resolved.json` records remote-reference resolution and the hashes of
materialized files. A normal source check reuses this resolution. A wired ref
that names a mutable branch is therefore refreshed only when its authored
declaration changes or an explicit refresh is requested. The watcher performs
no implicit network polling.

Callers request that network refresh through `refresh_agent_state()`. Normal
startup and watcher updates use `prepare_agent_state()` and therefore reuse an
unchanged authored ref without contacting its remote source.

`prepared.json` contains the Toolang version, parsed config, parsed program AST
for home state, and prepared cap metadata projections. Cap bodies are not
embedded in this document; runtime consumers read the corresponding immutable
file only when needed. A new process can construct
`AgentState` from the three generation documents and `files/` without parsing
authored source again.

Loading validates document schemas, scope, version, and referenced file
existence and sizes. If the current generation is incomplete or invalid,
preparation quarantines that directory and atomically recreates the same
content-addressed version. Quarantined directories are left for the future
generation garbage collector rather than deleted on the prepare hot path.

`files/` makes a generation self-contained:

- `authored/` copies authored cap files;
- `inline/` contains materialized caps declared inside `agent.too`;
- `referenced/` contains materialized program references;
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

Generations do not share content-addressed objects. Identical files may be
duplicated between versions to keep storage and loading simple. Preparation
does not delete old generations on its hot path; a separate garbage collector
may later retain the most recent generations.


## Prepare and Publish

Preparation first compares the current generation's source tree. If it matches,
the generation is reused without resolving remote references or reparsing
source. The Toolang version is currently metadata only, as described above.

When rebuilding, a writer:

1. acquires the scope's `prepare.lock`;
2. checks the current generation again so concurrent processes reuse completed
   work;
3. scans source metadata and captures source files;
4. resolves and materializes referenced and wired caps;
5. parses config, program, and caps;
6. verifies that source metadata did not change during the build;
7. writes a complete temporary generation and atomically renames it;
8. atomically publishes `current`.

Root and home locks are independent. Multiple foreground CLI processes and
background agents can prepare concurrently, while all agents safely share the
root generation.


## Watching

The state watcher observes root and home authored source plus both `current`
pointers. A relevant local change triggers preparation and publishes a new
immutable `AgentState` only when the resulting version changes. Changes inside
prepared generation directories are not treated as authored source changes.
The watcher also performs the same metadata-only source check on its configured
local interval so a change in the startup-to-watch registration window, or a
missed filesystem event, is recovered. This local check reuses unchanged remote
materialization and is not remote polling.
