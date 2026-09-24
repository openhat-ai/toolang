# Persistent Flat Model Catalog Cache

## Goal and approved scope

Speed up repeated `too models` queries by persisting the complete catalog once.
This design incorporates the approved review: flat records, no content scanning,
accurate dependency detection, and one replaceable cache per root or agent.
`providers` and runtime `AgentSetup` construction keep their current behavior.

Success means a warm invocation skips static parsing, merge, route resolution,
model building, and policy ordering while preserving every query, table status,
JSON field, and default/full ordering.

## Representation and layout

Each existing root/agent `.setup/models` directory contains:

```text
merged.json             # Complete derived inspection catalog
sources/<catalog>.json  # Independent source snapshots maintained by runtime setup
```

`merged.json` is one checksummed JSON document with dependency metadata and two
arrays: `providers` and `models`. Each record appears once. Models reference a
provider by string ID and have unique `(provider, id)` identities. Per-model
connection declarations use `connection` internally; public JSON export restores
`provider`. Structured facts such as costs, limits, and modalities keep their shape.

Each model carries adapter/API/environment availability facts and nullable
`allowed_order`. These determine readiness, status reasons, and default ordering.
Query views, resolved runtime routes, and lookup indexes are not persisted.
Consumers build and reuse query datasets or indexes only when needed. Export
reconstructs nested public JSON only for selected records.

Root and agent caches remain isolated. `--catalog` replaces only the static source;
captured content identifies it, independently of its selected path. Switching
between different contents A → B → A rebuilds and replaces the same cache each
time. Identical content at another path can reuse it. Missing explicit files error.
Source filenames cannot collide with the derived file. Source files are independent
runtime caches, not a transactionally synchronized copy of the listing's inputs.
Old flat source-cache locations and old derived schemas are ignored and rebuilt.

Sandbox mounts already share root `.setup` and the agent home. The JSON format and
dependency identity are portable across host/guest paths and supported Python
versions: exclude the injected `models_dev.path`, file timestamps, and unrelated
host/guest environment variables. Default, explicit, and environment-selected
catalogs follow the same rule. Changed credentials, configurations, plugin versions,
or local discovery facts still invalidate; a shared cache remains one replaceable slot.

## Dependency detection

- Read the static catalog once, check its size/stable observation, and hash its
  captured bytes. Parse those same bytes on a miss. A content-preserving touch
  does not invalidate the listing; runtime watcher touch semantics are unchanged.
- Capture each applicable config's complete bytes once for both parsing and hash.
  Missing files have a stable marker; even comment-only changes invalidate.
- Probe dynamic catalogs on every invocation and hash their current snapshot
  facts, including endpoint and model metadata. Preserve plugin error behavior:
  propagated errors abort without replacing the listing; built-in unavailable
  local services retain their existing empty-snapshot behavior.
- Include concrete adapter/catalog configuration, effective allow rules, plugin
  entry-point/distribution/version provenance, and a manually versioned schema.
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
type, identity, and reference checks. Retain locking and atomic replacement;
corruption is a miss and a failed write does not reject a valid in-memory result.

## Implementation and acceptance

Touchpoints: setup records/listing/cache/environment modules; route dependency
selection; config and static-source capture; watcher and models CLI; focused tests.

1. Real round trips preserve all query fields, nested declaration fields, table/JSON
   output, policy order, unavailable/disallowed models, and duplicate model IDs
   belonging to different providers. Default and full queries share the records.
2. Warm hits bypass parsing, routing, merging, building, ordering, and scanning.
   Dataset/index access is reused and status rendering does not rebuild a full
   lookup per output row.
3. Test changed bytes with restored size/mtime, unchanged touches, complete config
   changes, environment missing/set/empty/rotation, effective overrides/defaults,
   escaped template syntax, dynamic endpoint/metadata changes, and plugin/schema drift.
4. Cover root/agent isolation, explicit catalog switching, captured-byte consistency,
   source-name collisions, corrupt records/checksums, probe errors, and write failure.
   Verify host-to-sandbox and sandbox-to-sandbox remounts hit without static parsing
   for default, explicit, and environment-selected catalogs in root and agent contexts.
5. Run default offline verification. Benchmark fresh processes on the same complete
   catalog/configuration against main, reporting cache size, confirmed warm hits,
   cache-to-query-ready time, and default/all/filtered command latency. Keep timing
   thresholds out of unit tests and do not equate skipped work with measured speedup.

## Risks and open questions

Probes and process startup remain on the warm path. Runtime source snapshots may
duplicate data across agents. Config comments and credential rotation intentionally
invalidate. Editable plugin changes require a version/schema bump or cache removal.
No open design questions remain for this scope.
