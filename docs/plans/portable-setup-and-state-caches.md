# Define Portable Setup and State Caches

## Status

Proposed.

## Goal

Reuse unchanged Setup and State inputs across process restarts and host/guest
mounts without rebuilding model collections or rematerializing Agent State.
Persistent identities are portable content revisions; filesystem metadata is
only a process-local observation optimization.

This refines the cache portion of `agent-setup-resource-publication.md`. It does
not change `AgentSetup`, `StatePublication`, collection queries, execution
snapshot capture, or State reload.

## Success Criteria

- A cache produced below a host root or home is reusable when those anchors are
  mounted at different absolute guest paths.
- Persistent identities contain no absolute anchor, device, inode, directory
  metadata, secret, credential, plugin instance, or pickled runtime object.
- Setup distinguishes a root context with no agent from each agent context;
  distinct `--models` and effective `allow.models` variants coexist.
- A warm 7,250-model load does not parse the source catalog or rediscover model
  query facts from raw records.
- Setup still probes dynamic catalogs every five seconds. Ordinary turns,
  requests, and run acceptance only call `current()`.
- State retains its root, home, and composed-agent layers. Metadata-only changes
  do not change their semantic revisions.
- Warm remounted State preparation may verify source bytes but performs no
  Program parse, remote resolution, materialization, or layer publication.
- Missing, corrupt, incompatible, unsafe, or partially written cache data is a
  safe miss; a cache write failure never rejects a valid publication.
- The offline suite passes without a specialized model table renderer.

## Scope

Included:

- root/agent Setup model-cache placement, keys, values, and reuse;
- static catalog artifacts and derived model-context projections;
- `--models`, `allow.models`, and guest source mounting;
- portable State source manifests and watcher observations;
- State schema compatibility and linked-source mounting; and
- deterministic call-count, remount, corruption, secrecy, and performance tests.

Excluded:

- persisting `AgentSetup`, `ModelTarget`, tools, adapters, plugin instances,
  environment values, credentials, or API keys;
- cached fallback for a failed dynamic catalog probe;
- a shared Setup/State persistence schema or refresh transaction;
- changes to watcher cadence, query grammar, aliases, table rendering, or
  execution semantics; and
- cache pruning or cache-management commands.

## Portable Identity

`root` and `home` are logical anchors. A sandbox may change their absolute
locations, but preserves every path relative to its anchor:

```text
host_root / relative  <->  guest_root / relative
host_home / relative  <->  guest_home / relative
```

Each owner separates:

```text
observation fingerprint   process-local path and stat facts used to skip reads
semantic revision         portable digest of canonical content and behavior
```

Absolute paths, device, inode, platform, sandbox, and working directory may
appear only in process-local observations. A new process, including a guest,
may stream and hash source bytes once to address a cache entry. That is
validation, not rebuilding: cached content is not re-parsed or re-derived.
Later watcher cycles reuse observations and filesystem events.

Entries are immutable, schema-versioned, content-addressed, validated on load,
and atomically installed. Equivalent concurrent writers produce the same
entry. Setup addresses entries directly and needs no mutable `current` pointer;
State keeps `current` because it publishes durable execution revisions.

## Setup Cache

### Placement And Contexts

```text
${TOOLANG_ROOT}/.setup/models/
  catalogs/revs/<catalog-artifact-key>/catalog.json
  contexts/revs/<root-context-revision>/models.json

${TOOLANG_ROOT}/agents/<agent>/.setup/models/
  contexts/revs/<agent-context-revision>/models.json
```

The shared catalog store serves all agents and root inspection. A root context
is used only when no agent is selected and reads no fabricated
`agents/default`. An agent context layers its Setup projection over the root
projection and is stored below that home. Content-addressed revisions replace
the current single `projection.json`, so variants do not overwrite one another.

### Catalog Artifact

The selected static catalog is streamed to compute a byte digest. A catalog
artifact key covers the cache schema, source digest, and parser/schema
revision, so it is computable before catalog parsing. The artifact contains the
normalized validated snapshot, its semantic snapshot revision, and its own
canonical document digest. Exact Decimal values are preserved.

Source provenance may record an anchor kind and relative path for diagnostics,
but path does not define equality. Identical bytes selected from root, home,
`--models`, or a guest mount reuse one artifact. A path/device/inode/mtime/size
fingerprint may avoid another read in the same process but is never persisted.

### Context Key And Value

The context revision is the digest of a canonical, secret-free input document:

- cache and model-collection schema revisions;
- root scope, or agent scope plus stable agent identity;
- ordered catalog names and semantic snapshot revisions;
- Setup-owned model/provider/catalog/adapter config projection;
- relevant catalog and adapter entry-point provenance;
- environment readiness names and booleans, never values;
- normalized effective `allow.models`, including a startup override; and
- the selected static catalog revision, including `--models`.

`allow.tools`, cap allow, defaults, limits, sandbox/UI/channel config, and
State-owned config do not affect the model context key. Defaults and limits are
resolved after hydration; tools, adapters, and plugin instances remain
process-local.

The cached value contains all catalog sizes, including more than 512 models:

- ordered candidate and effective model facts;
- effective provider metadata;
- model query rows and stable keys; and
- exact-ref and candidate-key index inputs.

It never contains a `ModelTarget`, credential, environment value, runtime
header, or secret-bearing endpoint. Hydration binds current adapters, routes,
readiness, and credentials, then builds the immutable collection indexes from
cached query facts rather than raw catalog records. `SetupWatcher` and
`load_catalog_inspection()` share this codec; `AgentSetup` still exposes only
its filtered models and providers.

### Refresh And Mounting

One Setup refresh remains:

```text
observe inputs -> load changed semantic config -> reuse plugin instances
  -> load/hash static artifact -> probe dynamic catalogs concurrently
  -> load/hydrate context -> resolve remaining Setup values -> publish
```

Every five-second cycle still invokes dynamic catalog snapshots. Their
revisions participate in the context key, so unchanged probe results reuse the
projection. Exceptions retain the process-local last-known-good Setup; an
explicit offline snapshot is valid. Persistent data never replaces a required
probe.

Root `.setup` is explicitly mounted and the whole home mount includes agent
`.setup`. Sources below root/home use anchor-relative descriptors. A packaged
source uses its schema and content digest. An external `--models` file is
mounted read-only for a guest and sandbox orchestration passes the hosted path;
the guest must not receive the host-only absolute path in
`TOOLANG_MODEL_CATALOG`. Its cache identity remains its content revision.

## State Cache

### Layers And Sources

The layout remains:

```text
root/.state/root       shared root layer
home/.state/home       agent-home layer
home/.state/agent      root + home composition
```

Root `.state` and root cap directories are explicit mounts. The whole home
mount supplies home `.state`, programs, flows, config, and caps. Loaded cap
paths continue to be rebound from the active revision directory.

Replace persisted metadata `SourceTree` with:

```text
SourceObservation   process-local selected paths, listings, and stat metadata
SourceManifest      sorted relative file path, byte size, and SHA-256
```

Directories contribute only their descendant path set; directory size and
timestamps are excluded. State-owned config is canonicalized before hashing,
so Setup-only changes preserve the manifest. Symlinked files use their logical
relative path and target bytes; symlink directories remain rejected.

Within one watcher, observation-equal files reuse their prior digest. Changed
or added files alone are rehashed; a filesystem event or explicit refresh
invalidates the affected digest even when observed size and mtime are
unchanged. A new process verifies all selected bytes once. Metadata-only
changes then retain the same manifest, while added, removed, renamed, or
content-changed files change it even when size and mtime are preserved.

### Matching, Migration, And Watching

The portable manifest replaces `SourceTree` in canonical `layer.json` and
participates in the root/home layer revision. The existing State config,
resolutions, caps, programs, and materialized-file manifest remain unchanged.

```text
observe -> compute/reuse manifest -> compare with current layer
  -> load on match
  -> otherwise capture, parse, resolve, materialize, verify, and publish
```

`force=True` still refreshes remote refs when authored content is unchanged.
Remote output changes remain represented by resolutions, caps, and files and
therefore create a new layer revision.

Source and layer schemas are bumped. A legacy current layer is not a match and
is rebuilt. Exact historical layers remain loadable: execution does not need
their metadata tree, so the compatibility decoder accepts it for loading but
never treats it as a portable current manifest.

`StateWatcher` owns one observation and manifest per root/home scope. Events
and periodic safety checks compare observations first, hash changed files, and
invoke preparation only for a changed manifest, changed pointer, explicit
refresh, or force refresh. Setup and State remain independently published.

Escaping file symlinks must remain readable in a guest at the same logical
root/home-relative path. Sandbox preparation extends the current roaming
`agent.too`/`config.toml` nested mounts to every selected State source file.

## Safety And Concurrency

- Cache JSON is canonical and subject to strict schema, shape, size, ordering,
  duplicate, revision, and cross-reference validation.
- Setup serialization structurally rejects secret fields and values.
- Invalid data is a miss. Failed writes are logged and retried later without
  rejecting the in-memory publication.
- Immutable revisions may be read concurrently across host and guest; partial
  directories are never published, and correctness does not rely on a
  cross-environment advisory lock.
- State revisions are never pruned because execution history may reference
  them. Setup revisions are rebuildable but are not automatically pruned in
  this work.
- Cache, parser, collection-schema, or relevant plugin provenance changes are
  valid misses. Absolute anchor changes alone are not.

## Implementation Pull Requests

1. `perf(setup): make model caches layered and portable`
   - implement catalog and root/agent context stores, portable identities,
     large projections, hydration, external `--models` mounts, and Setup tests.
2. `perf(state): make source revisions portable`
   - implement observations/manifests, matching, compatibility loading,
     generalized linked-source mounts, and State tests.

These PRs merge in order, share no new persistence framework, and require no
coordinated Setup/State migration.

Likely Setup touchpoints are `src/toolang/common/layout.py`,
`src/toolang/plugin/models/cache.py`, `catalog.py`, and `collections.py`,
`src/toolang/setup/catalog.py` and `watcher.py`, sandbox mount orchestration,
and Setup/CLI/sandbox tests. Likely State touchpoints are
`src/toolang/state/source.py`, `cache.py`, `prepare.py`, and `watcher.py`, plus
State and sandbox tests. Each implementation updates `docs/models.md`,
`docs/agent-state.md`, and `docs/layout.md` as applicable.

## Acceptance Tests

1. Build Setup below one root, remap root/home absolute paths, and prove a new
   watcher calls neither the static parser nor raw model-query derivation.
2. Exercise root-without-agent and two agents against one static source and
   assert one catalog artifact plus independent context artifacts.
3. Exercise multiple `--models` contents and `allow.models` overrides and
   prove every variant remains reusable after the others run.
4. Prove only model-affecting config, readiness, plugin provenance, catalog
   revisions, and `allow.models` change a context key.
5. Validate all 7,250 cached query facts, Decimal preservation, duplicate
   rejection, corruption fallback, and absence of credentials/env values.
6. Prove dynamic probes run each watcher cycle while static reads, factories,
   and query derivation retain their unchanged call counts.
7. Prove embedded, attached, one-shot, and temporary-guest modes prepare only
   at the process boundaries defined by the existing publication plan.
8. Remap a prepared State root/home and assert identical root, home, and agent
   revisions plus guest-relative materialized cap paths.
9. Change only metadata and assert no State rebuild; change bytes with preserved
   size/mtime and assert the owning layer changes.
10. Cover add/remove/rename, empty/nested files, linked sources, explicit remote
    refresh, legacy exact loads, corrupt entries, and concurrent writers.
11. Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` for each implementation PR.

## Performance Validation

Using the same 7,250-model file and machine, report cold load, warm new host
process, warm remapped guest process, unchanged watcher refresh, and no-match
`too models --query` time. Rich table layout is absent from the no-match case.

Warm catalog-dependent CPU must be at most 25% of cold catalog-dependent CPU,
and the warm no-row CLI must be within 25% of the packaged lightweight-catalog
command. An observation-unchanged watcher refresh performs zero static source
reads and zero query-fact derivations.

For State, report cold, warm host, warm remapped guest, and unchanged watcher
checks. Warm remounted preparation may hash bytes once but performs zero
Program parses, remote resolutions, materializations, layer writes, or pointer
writes. An unchanged established watcher reads zero source bytes.

## Risks

- Cached projection decoding may remain too expensive; the budget applies to
  the complete codec and hydration path, not only source parsing.
- External `--models` and escaping symlinks require correct nested read-only
  mounts and hosted-path rewriting.
- Setup variants accumulate until a separate garbage-collection design exists.
- State current revisions change once; compatibility loading must preserve
  historical references.
- Plugin behavior can change without config changes, so provenance invalidation
  must remain conservative.

## Open Questions

None.
