# Isolate State Storage by Format

Status: Proposed; awaiting human confirmation. Definition only.

## Goal and success criteria

Prevent repeated State rebuilding when processes using different State formats
share a place or home. Each format owns a complete root/home/agent State store;
software releases using the same format share those stores.

With unchanged inputs and composition options, preparation, forced refreshes,
and watcher checks converge to one revision per scope and format. A format
upgrade requires an initial preparation, without invalidating another format's
publication or rewriting its data.

## Scope

Include State storage paths, publication and watcher consumers, derived locks,
initial adoption, offline acceptance tests, and State/layout documentation.
Canonical document encoding, revision hashing, public commands, flags, and
source editing behavior retain their existing contracts. Runtime ownership,
API/database compatibility, and garbage collection are separate concerns;
State isolation does not promise general mixed-version execution.

## Decisions

### Complete stores beneath format directories

Use the package-owned `AGENT_STATE_FORMAT`, not the software release version:

```text
PLACE/.state/root/
  edit.lock
  v<format>/
    current
    build.lock
    revs/<revision>/layer.json

HOME/.state/home/
  edit.lock
  v<format>/
    current
    build.lock
    revs/<revision>/layer.json

HOME/.state/agent/v<format>/
  current
  publish.lock
  revs/<revision>/layers.json
```

Layer revisions retain their captured `source/` and `materialized/` directories.
`AgentLayout` keeps the existing unversioned scope anchors; `state/cache.py`
owns the format-specific derived paths. Callers use its helpers. There is no
CLI flag or environment override for choosing a State format.

Every current read, candidate comparison, publication, exact-revision load,
integrity check, and watcher baseline uses only the running format's store.
Watchers ignore derived files and pointers belonging to other formats or the
old unversioned layout, including during periodic checks. Authored source
changes continue reaching every watcher through existing observation rules.

### Lock ownership

Root/home build locks and the agent publication lock live inside each format's
store, retaining the existing acquisition order. Same-format writers serialize;
different formats prepare independently. Source edit locks stay at their existing
shared paths because all formats edit the same authored files. Existing source
snapshot verification remains required during preparation.

### Adoption, upgrades, and history

Treat first adoption as an incompatible State storage-format change: increment
`AGENT_STATE_FORMAT` once, selecting the next unused value at implementation.
Subsequent increments automatically select a new directory; software releases
sharing a format must preserve that format's storage and semantic contracts.

A missing store is prepared normally from authored inputs. New code neither
reads nor writes unversioned State caches or another format's store. There is
no migration, cross-format lookup, fallback to another `current`, dual write,
symlink, or deletion. Existing old binaries can keep their unversioned stores
without invalidating the new store.

Historical loads use the exact revision inside the running format's store.
Absent or incompatible revisions fail without substituting current State or
changing old data. Older-format history remains on disk but is unsupported by
new code, following the existing
[State loading policy](../design/state.md#canonical-documents-and-revisions).

## Implementation touchpoints

- `src/toolang/state/{cache,format,prepare,watcher}.py`: derived paths, the first
  format increment, preparation/loading, event filtering, and watcher baselines.
- `tests/unit/state/{test_cache,test_prepare,test_watcher,test_identity,test_layer_format}.py`:
  storage, preparation, history, concurrency, and watcher regressions.
- Existing State path assertions in integration/CLI/sandbox tests: use the path
  helpers when testing behavior, and keep explicit layout-contract assertions.
- `docs/design/state.md`, `docs/design/layout.md`, and examples of State paths:
  describe the versioned stores and retained source edit-lock paths.

Verify host/guest State mounts and private visiting-home directory handling;
existing scope anchors remain unchanged.

## Acceptance tests

Use temporary local directories and isolated processes for different format
constants. Emulate the legacy writer's unversioned paths without installing an
old binary. Synchronize concurrent workers explicitly; no network, providers,
Docker daemon, or sleeps are required.

1. **Different formats:** alternate and concurrently prepare the same place/home
   with two formats. Each store retains one root/home/agent revision after
   repeated unchanged normal and forced refreshes; foreign stores remain intact.
2. **Existing old binary:** alternate an unversioned writer and a new-format
   writer. Each converges independently, and the new writer never reads or
   changes the legacy pointers, locks, or existing revision bytes.
3. **Same format:** concurrent processes using identical inputs/options return
   the same revisions and create one revision per scope, sharing the same locks.
4. **Watcher isolation:** foreign-format and legacy State activity causes no
   preparation through events or periodic checks. A source edit publishes once
   in each participating format, followed by stable no-op checks.
5. **Adoption and history:** legacy caches remain untouched through first
   preparation and restart. Current-format history round-trips offline without
   a current pointer; foreign-format revisions cannot be loaded as substitutes.
6. **Storage contracts:** document hashes, canonical bytes, atomic publication,
   captured resources, shared source edit locks, and host/guest remount checks
   retain their existing guarantees.

Implementation must pass the repository's default Ruff, formatting, type, and
offline pytest verification before every commit.

## Risks and limits

- Audit all three scopes: isolating root/home while sharing the agent composition
  would leave a publication conflict.
- Each format stores its own captured inputs. Disk usage grows with actual
  formats and source changes; existing accumulated files are not reclaimed.
- First adoption invalidates old State history for the new reader. No historical
  decoder or source reconstruction from old State is added.
- Two already published legacy binaries can still conflict with each other.
  Shared authored syntax and API/run-store compatibility keep existing limits.

## Open questions

None within this scope. Human confirmation is required before implementation.
