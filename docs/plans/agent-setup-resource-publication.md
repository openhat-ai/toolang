# Define Agent Setup Resource Publication

## Status

Proposed.

## Goal

Make the Setup and State runtime publications contain the effective resources
needed by execution, with configuration policy applied once by the owner of
each resource. Long-lived runtimes refresh those immutable snapshots in the
background, while run acceptance, Chat, and Script consume `current()` without
reloading catalogs or rebuilding collection-query datasets.

This definition also establishes the cache and process-lifecycle boundaries
needed to keep a 7,250-model catalog out of ordinary conversation and run
latency.

## Success Criteria

- `AgentSetup` publishes effective models and tools after setup-owned allow
  configuration; it exposes neither the raw catalog nor the applied allow.
- The State owner publishes effective module cap indexes after State-owned
  allow configuration; execution does not apply the same config query again.
- Setup-only config changes do not change the semantic State revision, and
  State-only changes do not rebuild Setup resources.
- Model matching, set operations, exact resolution, and indexes are owned by
  one immutable `ModelCollection`; tools use the corresponding collection
  boundary.
- `[default].model` is either one available concrete model ref or absent. There
  is no implicit first-model fallback, `[models].default`, or model alias
  support.
- `SetupWatcher` has one `refresh()` path, one five-second monitoring cadence,
  and reuses unchanged files, plugin instances, catalog snapshots, and derived
  model data.
- A failed refresh retains the last valid Setup. A catalog snapshot explicitly
  reporting offline state is a valid candidate rather than a watcher failure.
- Each root run captures one current Setup and State publication. The complete
  child-run tree retains that Setup; the independent State reload contract is
  unchanged.
- Remote Chat and Script do not construct a local Setup. Ordinary Chat turns
  with concrete selections do not request the full `/models` payload.
- Local one-shot Script constructs Setup and State once and starts no watcher
  loop. Commands that do not execute a run use narrow loaders.
- Cold and warm 7,250-model benchmarks, unchanged refresh benchmarks, and
  repeated Chat-turn call counts are reported by the implementation PRs.
- The default offline verification suite passes without a specialized model
  table renderer.

## Scope

Included:

- Setup/State configuration ownership and semantic projections;
- effective model, tool, and cap publication;
- `RunDefaults`, `ModelCollection`, and `ToolCollection` vocabulary;
- exact model-default semantics and removal of current alias/default fallback
  behavior;
- Setup watcher refresh, reuse, last-known-good, and derived-cache behavior;
- root-run snapshot capture and removal of repeated config/query application;
- AgentServer, embedded Chat, remote Chat, local Script, remote Script, and
  non-run command lifecycle integration; and
- deterministic call-count tests plus representative performance benchmarks.

Excluded:

- `CustomModelCatalog` and model aliases, which require a separate definition;
- a `[models].default` compatibility period;
- hot-swapping Setup inside an active root execution tree;
- changes to the existing State reload control contract;
- new catalog inspection commands or formats;
- a special renderer for the 7,250-row models table; and
- persisting an entire `AgentSetup`, plugin object, credential, or environment
  mapping.

## Configuration Ownership

Root and agent `config.toml` remain unified authored files for user
convenience. They are not one runtime object and do not create a shared
Setup/State revision. Each owner parses an independent semantic projection.

| Configuration | Owner | Published result |
| --- | --- | --- |
| `[models.providers.*]` | Setup | Resolved effective providers and model routes |
| `[plugin.model_catalog.*]` | Setup | Reused catalog plugin instances and snapshots |
| `[plugin.model_adapter.*]` | Setup | Installed adapters |
| `[plugin.toolset.*]` | Setup | Installed tools |
| `[allow].models`, `[allow].tools` | Setup | Filtered model and tool collections |
| `[default].model`, `[default].runnable` | Setup | `RunDefaults` |
| `[limit]` | Setup | `RunLimits` |
| Root/agent dotenv plus fixed process environment | Setup | In-memory `envs` and resolved readiness |
| Configured psyche, skill, service, and prompt tables | State | Materialized caps |
| `[allow].psyches`, `.skills`, `.services`, `.prompts` | State | Filtered per-module cap indexes |
| Agent/Flow source and cap files | State | Durable programs and cap materialization |
| `[sandbox]`, sandbox plugin config, UI, CORS, and channels | Owning orchestration package | Narrow call-site values, not Setup or State config |

An owner ignores known paths belonging to another owner without importing that
owner's schema. Its own parser still rejects malformed or unknown fields in its
projection. Cross-package commands that need orchestration configuration use
explicit narrow loaders rather than reading `AgentState.root_config` or
`home_config`.

Setup and State watchers may both notice one `config.toml` metadata change, but
they compare their semantic projections before rebuilding. A Setup-only edit
therefore causes at most a cheap State check and reuses the existing State
revision. A State-only edit causes at most a cheap Setup check and reuses the
same `AgentSetup` object when its projection is unchanged.

State layer persistence stores a canonical State-owned config projection, not
the complete authored `config.toml`. Raw config path metadata is a watcher
fingerprint and does not contribute to the layer revision. The State layer
schema is bumped, old rebuildable State caches are rebuilt, and configured-cap
definition references point to the canonical projected config artifact. This
amends the config-file portion of the durable Agent State definition without
changing Program or cap content addressing.

### Allow Precedence

Process-start `--allow` and environment overrides are partitioned by resource
owner. A provided field replaces the corresponding root/agent config field
before that owner builds its effective snapshot, so a startup override can
expand or narrow the config result. These frozen overrides are reevaluated only
when their owner's source snapshot refreshes.

Session, request, and authored runnable allow operations happen after
publication and can only narrow the published base. They never recover a model,
tool, or cap removed by Setup/State configuration.

The resource pipeline is:

```text
catalogs + providers + adapters ─ allow.models ─> setup.models
installed toolsets             ─ allow.tools  ─> setup.tools
materialized module caps       ─ cap allow    ─> state.caps_for(module)

setup/state effective base
  -> session/request allow
  -> runnable = / += / -= directives
  -> concrete AgentResources
```

Config allow values are consumed during publication and are not fields of
`AgentSetup`, `StateResources`, or `AgentResources`. Startup cap overrides are
also publication inputs rather than durable authored State; the run's concrete
cap identities remain recorded in `AgentResources` alongside the durable State
revision.

## Runtime Values

### AgentSetup

The public immutable shape is:

```text
AgentSetup
├── layout
├── providers
├── adapters
├── models                  # ModelCollection, after allow.models
├── tools                   # ToolCollection, after allow.tools
├── defaults
│   ├── model               # concrete ref or None
│   └── runnable            # configured ref or None
├── limits
├── envs
└── environment
```

`providers` contains resolved providers needed by the effective model entries;
their nested model maps do not retain the complete unfiltered catalog.
`adapters`, `envs`, and `environment` remain process-local immutable values.

The following current fields are removed:

- `catalog`: raw catalog inspection uses a dedicated loader;
- `provider_configs`: route configuration is compiled into providers and model
  entries;
- `ceiling`: setup-owned allow has already been consumed; and
- `bindings`: renamed and narrowed to `defaults`.

Setup exposes no aliases, default-query list, raw candidate list, public query
dataset, or public indexes.

### RunDefaults And RunBindings

`RunDefaults(model, runnable)` represents configured starting values.
`RunBindings(model, runnable)` continues to represent concrete values bound to
one accepted run. `resolve_run_bindings()` becomes
`resolve_run_defaults()`; Setup watcher constructor and CLI vocabulary use
`default_overrides` rather than `binding_overrides` where the value is not yet
bound to a run.

`[default].model` accepts only a concrete model ref. Setup publication resolves
it through `setup.models`; zero matches, ambiguity, or exclusion by
`allow.models` rejects the candidate. Initial refresh then fails, while a
later invalid candidate retains the last-known-good Setup.

If `[default].model` is absent, `defaults.model` is `None`. Toolang does not
choose `gpt-5`, the first collection entry, or any other implicit model. A Flow
that does not require a model may run with no model; an agic request without an
explicit or configured model is rejected before acceptance.

`[default].runnable` is syntax-validated by Setup without importing State. A
root caller resolving defaults validates it against the captured current State.
Surface-specific behavior such as Chat's `agic:chat` fallback remains at that
surface and does not become an `AgentSetup` default.

`[models].default` and `[models.aliases.*]` are rejected. Custom identities and
aliases will later enter through `CustomModelCatalog`, where they can become
ordinary concrete collection entries.

### ModelCollection

The effective collection owns matching, set operations, and exact lookup:

```text
ModelCollection
├── entries
├── match(queries)
├── apply(operations)
├── resolve(ref)
├── contains(ref)
└── refs()

private:
├── _by_key
├── _by_ref
└── _matcher
```

Each `ModelEntry` contains:

```text
key       deterministic internal set/resource identity
ref       unique public concrete provider/model ref
target    resolved execution target
info      query, capability, limit, and accounting metadata
```

Equivalent Setup builds produce the same secret-free key. A snapshot rejects
duplicate public refs. `resolve()` is exact and O(1); it never interprets a
collection query. `match()` and `apply()` use the existing `MatchUnion` and set
operator semantics against one immutable base, preserve base order, and return
immutable subsets without rediscovering candidates. Query views and datasets
remain private implementation details of the collection.

`ToolCollection` follows the same contract. `ToolEntry` additionally retains
the model-facing name and concrete `AgentTool`. Its public ref remains
`toolset/name`.

`AgentResources.models` stores ordered model entry keys rather than query
strings. Execution resolves those keys only through the root tree's captured
`setup.models`. Model-step preparation and accounting use the selected entry's
`target` and `info`; they no longer require `setup.catalog` or reconstruct a
candidate dataset.

This definition supersedes the `[models].default` fallback and runtime alias
decisions in `collection-query-language.md` and `agic-runtime-calls.md`.
Collection query grammar and set semantics remain unchanged.

### State Publication And Effective Caps

The durable `AgentState` continues identifying exact Programs, raw materialized
caps, and the State-owned config projection. A process publication pairs it
with one immutable derived value:

```text
StatePublication
├── state                    # durable AgentState
└── resources
    └── caps_by_module       # after config/startup cap allow
```

`StateResources` is derived once per `(AgentState revision, frozen startup cap
override)` and precomputes the effective ordered cap collection for every
Program module. Config allow is the default input; a startup field replaces
that field before derivation and may therefore expand back into the durable raw
cap base. Execution looks up `caps_for(module)` on `StateResources` rather than
rebuilding a `QueryDataset` at each run boundary.

The companion value avoids making process-local startup overrides part of the
durable State revision. A run records `StatePublication.state.revision` plus
the concrete cap identities in `AgentResources`, so historical resolution
remains exact. `StateWatcher.current()` supplies the complete publication;
`load()` and State reload derive resources with the same watcher-owned frozen
override.

The four cap kinds remain independent query bases. There is no combined `caps`
allow field, no cross-kind query, and no coupling to Setup model/tool matching.

## Setup Watcher

`SetupWatcher` owns one serialized candidate and publication path. Its public
`refresh()` takes no static/dynamic or force-mode switch:

```text
refresh()
  -> capture cheap input fingerprints
  -> parse only changed semantic inputs
  -> reuse or recreate affected plugin instances
  -> obtain watcher-driven catalog snapshots
  -> derive available model/tool entries
  -> apply config/startup allow once
  -> resolve defaults and limits
  -> publish one immutable AgentSetup
```

There is no `refresh_static()`, plugin-owned timer, or separate static/dynamic
watcher. `run()` invokes the same `refresh()` every five seconds. File-backed
inputs may be reused from path, device, inode, `mtime_ns`, and size
fingerprints; sources without a stable file fingerprint are probed during each
cycle. All plugin `snapshot()` calls are initiated by SetupWatcher, and
independent catalog probes may run concurrently.

Plugin entry-point discovery and factory calls are reused until the relevant
plugin configuration or environment projection changes. An unchanged cycle
does not reread the file-backed models catalog, reconstruct adapters/tools, or
rebuild model query indexes. If the completed candidate equals the published
value, `current()` keeps the exact same object.

Initial refresh raises when no valid Setup can be built. After publication, a
parse, plugin, catalog, policy, or default-resolution exception records a
diagnostic, returns the last valid Setup, and does not publish. A returned
offline catalog snapshot is valid input and is merged/published normally; only
an exception is a probe failure. Config allow may legitimately produce an
empty collection. `diagnostics()` exposes the most recent rejected candidate
and clears after the next valid refresh; `updates()` yields only a newly
published object.

Consumers never call `refresh()` at a run or request boundary. They use
`current()`. Explicit catalog refresh/inspection commands use their dedicated
one-shot loader and do not add another Setup watcher mode.

### Derived Model Cache

The model catalog package persists a rebuildable, schema-versioned,
secret-free derived projection below `.setup/models/`; SetupWatcher and the
one-shot catalog inspection loader share that component. Its key includes the
selected catalog file identity, catalog snapshot revisions, Setup config
projection, relevant environment readiness facts, and Toolang cache schema.

The cached value contains only validated provider/model metadata needed to
rebuild effective entries. It never contains adapter/tool instances, API keys,
credential values, headers containing secrets, the full process environment,
or a pickled `AgentSetup`. Runtime targets are reconstructed with the current
in-memory environment. Cache files are atomically written and fully validated;
missing, stale, corrupt, or incompatible data is a cache miss.

The cache does not suppress watcher-driven probes for catalogs whose current
snapshot cannot be established from a cheap fingerprint. It improves a warm
first refresh in a new process and avoids reparsing a large unchanged
file-backed catalog. A true cold miss still validates the source catalog and
then seeds the cache.

## Snapshot Capture And Reload

At root acceptance, the executor obtains `setup.current()` and the current
`StatePublication` once, validates cross-references such as the runnable
default, and builds the tree-level effective resource base. Setup and State
have no shared revision requirement and are not published in one transaction.

The accepted root and every child retain the captured Setup object. A Setup
refresh affects only later root executions. Existing State reload behavior
continues independently: a later run/step boundary may capture the root tree's
new State publication, but it continues resolving model/tool keys through the
root's Setup.

Request/session allow values narrow the prepared base. Runnable directives
operate on the resulting collection objects. Validation, child-run preparation,
and retry/rerun paths reuse those collections and do not parse root/home config
or rediscover model candidates.

## Process Modes

| Mode | Setup/State lifecycle |
| --- | --- |
| Resident AgentServer (`start`, managed sandbox server) | One initial Setup and State refresh, then their background watchers; endpoints and Executor use `current()` |
| Chat with an embedded host Executor | One initial refresh, then watchers for the Chat session lifetime |
| Chat connected to an existing or temporary AgentServer | No client Setup/State construction; server owns snapshots |
| Local one-shot host Script | Exactly one Setup and State refresh; no watcher task |
| Script connected to an AgentServer | No client Setup/State construction |
| Commands that execute no run | Use definition-only, catalog-only, tool-only, State-only, or orchestration-only loaders as required |

Raw model catalog list/export/provider commands use a catalog inspection loader
and never depend on `AgentSetup.catalog`. Effective runtime model and tool lists
may use one current Setup when already inside a resident process. `too query`
continues loading definitions only.

Agent APIs stop calling State `refresh()` in ordinary list, defaults, and run
acceptance paths; the process-owned watcher supplies freshness. An explicit
State refresh/reload operation remains explicit.

Remote Chat obtains concrete run defaults without downloading `/models`.
Concrete model refs are sent directly and validated through the server's
`ModelCollection.resolve()`. `/models` is requested only for an explicit model
list/picker operation. The same rule removes the model-list request from an
ordinary remote Script whose model is already concrete. Canonical runnable refs
similarly bypass runnable-list materialization.

## Implementation Sequence

Keep implementation reviewable in three PRs after this definition:

1. `feat(setup): publish effective runtime resources`
   - introduce `RunDefaults`, `ModelCollection`, and `ToolCollection`;
   - partition config ownership and State semantic projections;
   - publish effective model/tool/module-cap collections;
   - remove Setup catalog/ceiling/provider-config/binding fields, aliases, and
     implicit model defaults; and
   - migrate execution, accounting, catalog inspection, tests, and docs to the
     new snapshot contract.
2. `perf(setup): reuse setup refresh inputs`
   - implement the single five-second refresh path, fingerprints, plugin and
     snapshot reuse, last-known-good diagnostics, and safe derived model cache;
   - preserve explicit offline publication; and
   - add cold, warm, and unchanged-refresh benchmarks.
3. `perf(execution): remove repeated runtime preparation`
   - apply the process-mode lifecycle table;
   - remove request-boundary refreshes and repeated resource/default
     materialization;
   - bypass `/models` and runnable lists for concrete remote selections; and
   - add repeated Chat/Script/run call-count and integration coverage.

Draft PR #422 is not merged independently. Its empty/exact-query fast paths may
be folded into the collection or consumer PR only if they remain necessary;
the generic candidate-dataset workaround is superseded by this design.

## Design Touchpoints

- `src/toolang/setup/{types,config,watcher}.py`: Setup shape, projections,
  publication, diagnostics, fingerprints, and cache orchestration.
- `src/toolang/base/types/policy.py`: `RunDefaults` versus `RunBindings` and
  remaining request-level allow types.
- `src/toolang/plugin/models/{collections,resolution,config,catalog,loading}.py`:
  effective model entries, exact lookup, matching, raw inspection, and
  secret-free cache codecs.
- `src/toolang/plugin/toolsets/{collections,loading}.py`: effective tool
  entries and collection operations.
- `src/toolang/state/{config,source,cache,prepare,state,watcher}.py`: State-owned
  projection, layer schema, `StatePublication`/`StateResources`, prepared
  module cap collections, and independent invalidation.
- `src/toolang/execution/{calls,policy}.py` and
  `execution/executor/{resources,prepare,executor,limits}.py`: root capture,
  stable resource keys, exact model resolution, accounting, and removal of
  repeated config/query work.
- `src/toolang/up/{core,server,sandbox,config}.py`: watcher ownership and narrow
  orchestration config loaders.
- `src/toolang/api/routers/{agent,runs}.py`: current-snapshot endpoints and
  exact request validation.
- CLI Chat, Script, model catalog, plugin, and shared run-client modules:
  process-mode integration and remote fast paths.
- Setup, State, execution, API, CLI, plugin, and performance tests plus the
  affected model, execution, config, and layout documentation.

## Acceptance Tests

1. Setup config projection applies root/home precedence and startup replacement
   to model/tool allow, defaults, and limits exactly once, then exposes no allow
   value.
2. State config projection and frozen startup replacement produce one
   `StateResources` per State revision/override pair, apply each cap-kind allow
   exactly once, and make `caps_for(module)` perform no query-dataset
   construction.
3. Editing only Setup-owned config keeps State layer and Agent State revisions;
   editing only State-owned config returns the identical `AgentSetup` when all
   Setup inputs are otherwise unchanged.
4. `AgentSetup` has the defined fields and no catalog, provider configs,
   ceiling, bindings, aliases, or default-query list.
5. Model/tool collection matching, `=`, `+=`, `-=`, ordering, key stability,
   exact resolution, duplicate-ref rejection, and empty results match the
   collection-query contract.
6. `AgentResources` round-trips model keys and resolves them through the
   captured Setup; accounting uses entry metadata without a catalog snapshot.
7. Missing `[default].model` stays `None`; no model is auto-selected. Concrete,
   excluded, missing, ambiguous, and dynamically unavailable defaults follow
   the defined publication errors. Agic acceptance requires a model while a
   model-free Flow remains valid.
8. `[models].default` and `[models.aliases.*]` fail with actionable errors;
   provider and catalog plugin configuration remains valid.
9. Concurrent Setup refresh requests serialize. Unchanged inputs preserve
   object identity, entry points/plugins are not recreated, unchanged file
   catalogs are not reread, and probe exceptions retain last-known-good.
10. The five-second watcher drives catalog probes; explicit offline snapshots
    publish changed availability, while plugins create no timer or publication
    task of their own.
11. Derived-cache warm load, stale/corrupt/schema-mismatch fallback, atomic
    writes, and secret exclusion are deterministic and offline.
12. Every process mode constructs and refreshes Setup/State the specified
    number of times. No accepted root calls either watcher `refresh()`.
13. An ordinary remote Chat turn and concrete remote Script perform no
    `/models` or runnable-list request. Explicit model list/picker operations
    still receive the established payload.
14. Active execution trees retain their Setup across watcher publication and
    State reload; later roots observe the latest Setup/State pair without a
    revision-match requirement.
15. Raw catalog list/export output and Unicode/numeric table behavior remain
    unchanged; no dedicated large-table renderer is introduced.
16. Ruff, formatting, type checking, and the default pytest suite pass for each
    implementation PR.

## Performance Verification

Default tests assert work counts rather than wall-clock limits. The relevant
PRs also publish repeated-median measurements on the same machine and fixture
for:

- cold Setup refresh from a 7,250-model catalog;
- warm first refresh in a new process using the derived cache;
- repeated unchanged refresh on one watcher;
- effective model exact lookup and allow/directive application; and
- remote and embedded Chat turn preparation before provider execution.

The report compares against `ba184c30` and separately identifies catalog
parsing, plugin discovery, collection construction, API transfer, and table
rendering. Cold construction must improve rather than regress; warm startup and
unchanged refresh must avoid full catalog parsing; and ordinary turn
preparation must be independent of raw catalog row count. Table rendering is
measured but is not optimized by this feature.

## Risks

- Removing aliases and `[models].default` is intentionally breaking and needs
  direct migration errors and documentation.
- State config projection changes the rebuildable layer schema and must not
  weaken durable configured-cap provenance.
- Persistent cache invalidation must include every semantic input while never
  storing credentials; a conservative miss is preferable to stale reuse.
- Dynamic catalog availability can invalidate a configured default. Retaining
  last-known-good avoids publishing an internally inconsistent Setup but may
  keep the previous default until configuration or availability recovers.
- Stable model resource keys become durable execution vocabulary and require
  golden round-trip tests before use in records.
- The first implementation PR is broad, but splitting the snapshot contract
  across compatibility layers would make both review and runtime behavior less
  clear.

## Open Questions

None.
