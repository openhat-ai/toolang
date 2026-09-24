# Define a Persistent Derived Model Catalog Cache

## Status

Proposed. Implementation requires explicit human approval of this plan.

## Goal

Avoid rebuilding the full model-list projection on every `too models`
invocation. Persist the complete derived catalog and reuse it while all inputs
that affect it remain unchanged. Apply `--query` after loading the full data.

## Success Criteria

- A warm unchanged invocation skips source parsing, catalog merge, route
  resolution, model build, and ordering; query filtering still works.
- One cache contains all catalog models, including unavailable or disallowed
  entries, so default and `--all` views use the same source data.
- Any change to catalog bytes, full config-file bytes, relevant environment
  values, dynamic catalog snapshots, plugin provenance, or projection schema
  invalidates the cache.
- Local catalogs are probed every invocation; their host environment variables
  do not directly enter the environment fingerprint.
- Cached and uncached query/table/JSON results are identical. Corrupt, unsafe,
  missing, or incompatible entries safely rebuild without persisting secrets.
- Tests prove warm hits bypass build/order; a benchmark compares warm and cold
  runs without fragile timing assertions in the unit suite.

## Decisions

### Placement and data

Use the existing root or agent model-cache directory, whose location already
provides isolation:

```text
.setup/models/
  models_dev.json
  ollama.json
  llama_cpp.json
  merged.json
```

Keep existing per-catalog source snapshots. Add one distinct `merged.json`
containing the complete ordered model-list projection plus its dependency
metadata. Do not call it `effective.json`, and do not store a separate
manifest. Keep JSON export fields, query facts, `--all` readiness/allow state,
and stable ordering needed by the model-list command. This is not a persisted
`AgentSetup`: exclude adapters, plugin instances, credentials, raw environment
values, resolved headers, and resolved API endpoints. Reuse cache safety checks;
unsafe data is non-cacheable, never a reason to relax validation.

### Cache validation and refresh

Build the cache identity from:

- exact static catalog byte revisions;
- complete byte fingerprints for root and selected agent `config.toml` files,
  including a stable missing-file value;
- sorted relevant environment entries as `(name, SHA-256(value))` pairs;
- current dynamic catalog snapshot revisions;
- model catalog and adapter plugin provenance; and
- a manually versioned projection/cache schema.

Store these revisions and the environment-variable **names** in `merged.json`;
never store variable values. Directory placement defines scope, so scope is not
part of the identity.

Static catalog revisions use a digest of the captured file bytes. A hit may
reuse the cached variable-name list only when its non-environment dependency
revisions match; hash the current values for those names and compare the full
identity. On a miss, derive the names from the exact source snapshots used for
the rebuild. Names come from provider environment requirements and valid API
URL template substitutions in both provider and model declarations (`$NAME`,
`${NAME}`; `$$` is an escaped literal). A dedicated
`src/toolang/setup/cache_environment.py` module owns name extraction and
fingerprinting. It returns only sorted `(name, SHA-256(value))` pairs for
currently set variables, and must never persist or log raw values. A value
rotation intentionally invalidates the projection, including credentials.

Do not fingerprint `OLLAMA_HOST`, `LLAMA_CPP_HOST`, or
`TOOLANG_HOST_GATEWAY`. Probe configured local catalogs on every invocation;
include their returned snapshot revisions in the identity. A changed endpoint or
model inventory therefore triggers a rebuild. If a required probe fails, do
not treat old data as current or publish an incomplete replacement; retain
existing error behavior.

On a hit, read and validate `merged.json`, then filter it for the request. Do
not decode source cache records or rerun merge, route resolution, build, or
ordering. On a miss, use the same captured static bytes and dynamic snapshots
whose revisions formed the identity, rebuild once, and atomically replace the
merged cache. This prevents associating a key with a different source read.
Query and output format are not identity inputs. A failed cache write must not
reject a valid in-memory result.

## Scope and Touchpoints

The first implementation covers `too models` only; `providers` and runtime
`AgentSetup` caching remain unchanged. Likely files:

- `src/toolang/setup/cache_environment.py`: auditable variable extraction and
  hashed fingerprinting;
- `src/toolang/setup/cache.py` or a focused sibling: merged projection codec
  and safe atomic persistence;
- `src/toolang/setup/watcher.py` and
  `src/toolang/cli/toolang/commands/model_catalog.py`: validate revisions and
  use the merged projection before list construction;
- setup unit tests and `tests/integration/cli/test_model_catalog_commands.py`:
  invalidation, parity, secrecy, and warm-path coverage.

## Acceptance Tests

1. An unchanged hit skips source parsing, merge, route resolution, build, and
   ordering, but repeated queries return the same stable results.
2. Changed catalog bytes invalidate even if size and mtime are restored;
   changes to any root/agent config bytes invalidate.
3. Environment extraction covers provider `env` and provider/model API
   templates, supports `$NAME` and `${NAME}`, ignores escaped `$$`, sorts names,
   omits unset values, and persists hashes but never raw values. An unrelated
   environment variable does not invalidate.
4. Local catalogs are probed each invocation; unchanged revisions hit and
   changed revisions rebuild. Host/gateway env vars are not direct key inputs.
5. Plugin provenance and projection schema changes invalidate.
6. Default and `--all` table/JSON output, repeated query semantics, and full
   catalog export match an uncached build.
7. Corrupt, unsafe, stale-schema, or missing cache data safely rebuilds; failed
   writes do not leave partial files or reject valid in-memory output.
8. Default offline verification passes.

## Risks

- Remote dynamic changes require a probe each invocation; the cache cannot know
  a service changed without observing its current snapshot.
- Hashing complete configs intentionally invalidates on comments or other
  byte-only changes.
- Credential rotation also invalidates the model projection by design.
- Full-catalog serialization and validation have a cost; benchmark the warm
  path against the current approximately 1.9-second `too models` invocation.
- Third-party catalog data may contain sensitive provider/model overrides; the
  safety validator must reject such a cache without weakening secret checks.

## Open Questions

None. Cache location/name, environment hashing, local-probe behavior, and
root/agent scope are decided above.
