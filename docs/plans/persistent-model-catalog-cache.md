# Persistent Flat Model Catalog Cache

> Superseded by [Lazy AgentSetup Accessors](lazy-agent-setup-accessors.md). The
> current design removes persistent model-catalog caches and materializes setup
> resources lazily in memory. This document is retained as historical context.

## Goal and approved scope

Speed up `models`, `providers`, `chat`, and `run/start/serve` through one runtime
setup path. `SetupWatcher.refresh()` owns source capture, dependency validation,
cache reads/rebuilds, resource loading, policy, and publication. Callers consume
`AgentSetup`; they never load model catalogs or tools independently. `adapters`
remains an installed-plugin metadata command, as explicitly agreed.

A warm setup skips static parsing, catalog merging, full-catalog route/query-view
construction, and policy ordering. It hydrates only ready, allowed model records,
resolves their routes against the current environment, and builds their ID index.
Query views and field indexes are created only when a query needs them. Preserve query/export fields, ordering, source revisions, and last-good
publication on refresh failures.

## Representation and layout

Each existing root/agent `.setup/models` directory contains:

```text
merged.json             # Complete flat catalog shared by all setup consumers
sources/<dynamic>.json  # Probe results/stamps for runtime source revisions
```

`merged.json` is one checksummed JSON document with dependency metadata and two
arrays: `providers` and `models`. Each record appears once. Models reference a
provider by string ID and have unique `(provider, id)` identities. The reader
checks the envelope checksum and dependency metadata before directly decoding
typed provider/model arrays, without a catalog-sized intermediate dictionary tree. Per-model
connection declarations use `connection` internally; public JSON export restores
`provider`. Structured facts such as costs, limits, and modalities keep their shape.

Each model carries adapter/API/environment availability facts and nullable
`allowed_order`. These determine readiness, status reasons, and default ordering.
Query views, resolved runtime routes, and lookup indexes are not persisted.
Setup owns the flat records and builds query datasets or runtime indexes only as
needed. `AgentSetup.model_listing()` exposes the published inspection projection.
`model_catalog(all=True)` lazily hydrates the complete catalog from the same records,
environment, and adapter defaults captured by that setup version, without rereading files.
Nested record values are immutable so inspection cannot mutate a later lazy view.
Hydration copies fields directly from typed records instead of converting records
back into mutable JSON-shaped dictionaries and decoding them again.
Export reconstructs nested public JSON only for selected records.

Root and agent caches remain isolated. `--catalog` replaces only the static source;
captured content identifies it, independently of its selected path. Switching
between different contents A → B → A rebuilds and replaces the same cache each
time. Identical content at another path can reuse it. Missing explicit files error.
Dynamic probe files retain their existing `detected:` revisions; they do not supply
models for setup construction. The old `sources/models_dev.json` is ignored and is
no longer written. Static parsing on a merged-cache miss uses the captured source
bytes. No additional static cache or separate CLI loading path remains.

Sandbox mounts already share root `.setup` and the agent home. The JSON format and
dependency identity are portable across host/guest paths and supported Python
versions: exclude the injected `models_dev.path`, file timestamps, and unrelated
host/guest environment variables. Default, explicit, and environment-selected
catalogs follow the same rule. Changed credentials, configurations, plugin versions,
or local discovery facts still invalidate; a shared cache remains one replaceable slot.
Mount an existing root `catalog.json` read-only, resolving symbolic links, so default
source selection survives the move. Explicit selection of that same file adds no
duplicate mount. Verify against the actual sandbox mount plan, not a whole-root copy.

Privileged cache writers inherit the mounted parent directory's UID/GID for new
directories, lock files, and atomically replaced documents. Keep existing file modes
and the process umask; do not broaden access or change the container's execution UID.
This covers both source and merged caches and both host-first and guest-first writes.
Unprivileged writers still require filesystem access to the shared directory.

## Dependency detection

- Read the static catalog once, check its size/stable observation, and hash its
  captured bytes. Parse those same bytes on a miss. A content-preserving touch
  does not invalidate the listing; runtime watcher touch semantics are unchanged.
- Capture each applicable config's complete bytes once for both parsing and hash.
  Missing files have a stable marker; even comment-only changes invalidate the
  persistent cache on its next read. An existing watcher may retain an unchanged
  effective setup for changes that only affect comments or State configuration.
- Probe dynamic catalogs on every invocation and hash their current snapshot
  facts, including endpoint and model metadata. Preserve plugin error behavior:
  propagated errors abort without replacing the listing; built-in unavailable
  local services retain their existing empty-snapshot behavior.
- Include concrete adapter/catalog configuration, effective allow rules, plugin
  entry-point/distribution/version provenance, and a manually versioned schema.
  Include the actually loaded adapter names and default-API hash: an installed
  entry point can become unloadable when a sandbox lacks an optional dependency.
- Derive environment dependency names using the same effective credential rules
  and API-template selection as route resolution, including adapter defaults and
  currently missing variables. Store sorted names and SHA-256 hashes of set values.
  Missing and empty values differ; rotation invalidates; unrelated values do not.
  Local discovery variables need no extra hash when their only effect is represented
  by the probe, but remain dependencies when explicitly used in a route template.
- Recompute dependency names on every miss, including config/plugin changes.
  Queries and output formats do not enter the cache identity.

Catalog declarations are trusted data: neither source nor merged catalog reads or
writes run heuristic secret/header/URL scans or discard authored fields. Raw
process/dotenv values and resolved runtime routes never enter the listing. Enforce
that boundary when projecting environment inputs. Keep size, checksum, schema,
type (including nested connection declarations), identity, and reference checks.
Retain locking and atomic replacement;
corruption is a miss and a failed write does not reject a valid in-memory result.

## Implementation and acceptance

Touchpoints: setup records/listing/cache/environment modules; route dependency
selection; config and static-source capture; watcher, setup types, and models CLI;
compaction tool selection; focused tests. `run` already owns a watcher; `start/serve`
use `AgentCore.setup`; local `chat` owns a watcher. Those callers need no alternative
cache integration. Adapter registration does not import the OpenAI call SDK; transport
error handling imports it only when a model call begins. Compaction selects history tools from `setup.tool_collection(all=True)`
so it reuses configured instances even when user-facing tool policy excludes them.

1. Real round trips preserve all query fields, nested declaration fields, table/JSON
   output, policy order, unavailable/disallowed models, and duplicate model IDs
   belonging to different providers. Default and full queries share the records.
2. Warm runtime and inspection consumers share the merged cache. Bypass static
   parsing, merging, full-catalog routing/query views, ordering, and scanning.
   Hydrate only selected runtime records; full views retain the published environment
   and adapter defaults after source/cache removal, plugin mutation, or a subsequent refresh. Model/tool defaults still
   undergo the caller's existing validation policy.
3. Test changed bytes with restored size/mtime, unchanged touches, complete config
   changes, environment missing/set/empty/rotation, effective overrides/defaults,
   escaped template syntax, dynamic endpoint/metadata changes, and plugin/schema drift.
4. Cover root/agent isolation, explicit catalog switching, captured-byte consistency,
   source-name collisions, corrupt records/checksums, probe errors, and write failure.
   Verify host-to-sandbox and sandbox-to-sandbox remounts hit without static parsing
   for default, explicit, and environment-selected catalogs in root and agent contexts.
   On Linux, verify root guest writes remain readable and writable by the host UID,
   including first writes, replacements, directory traversal, and lock reopening.
5. Run default offline verification. Benchmark fresh processes on the same complete
   catalog/configuration against main, reporting cache size, confirmed warm hits,
   cache-to-setup-ready time, interactive chat readiness, and models/providers latency. Keep timing
   thresholds out of unit tests and do not equate skipped work with measured speedup.

## Risks and open questions

Probes, lightweight adapter/tool factories, and process startup remain on the warm path.
All setup consumers share one cache path; SDK initialization and model query views
are deferred until needed. Full provider inspection intentionally materializes every model.
Dynamic probe snapshots may duplicate their small catalogs across agents. Config comments and credential rotation intentionally
invalidate. Editable plugin changes require a version/schema bump or cache removal.
No open design questions remain for this scope.
