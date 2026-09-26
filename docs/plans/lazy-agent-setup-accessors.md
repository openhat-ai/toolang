# Lazy AgentSetup and Flat Model Catalog

## Goal and scope

Load model data once per setup revision, without persistent projection caches or
setup-time model query indexes. A watcher publishes lightweight, versioned
setups; each setup resolves its model/provider views on first access and keeps
those views for its lifetime. Preserve public CLI commands, model routes, and
tool/cap behavior. Catalog input and model-allow ordering change as specified
below. The upstream catalog publication pipeline is owned separately; this PR
only consumes a manually selected or bundled flat catalog.

## Design

### Catalog input

- Select one file in this order: explicit `--catalog`,
  `TOOLANG_MODEL_CATALOG`, agent-home `catalog.json`, root `catalog.json`,
  bundled `catalog.json`. Root-only setup skips agent home. No automatic fetch.
- Accept only `{ "providers": [...], "models": [...] }`. Each model has a
  `provider` owner ID and `id`, with optional connection settings in `override`;
  compose its public `ref` as `provider/id`. Validate required types, unique
  provider/model identities, and existing provider ownership. Reject the old
  flat `ref`/`provider_override` fields and nested/raw models.dev input. Unknown
  additive fields may be dropped. Preserve provider and model file order.
- Bundle the exact 2026-09-26 `openhat-ai/models` release catalog (223 providers,
  2,134 models); external catalog updates are independent of Toolang startup.
  Additional local catalog plugins remain supported as separate sources.

### Lazy generations and views

- Watcher refresh captures validated static data and semantic local-plugin
  probes; no catalog merge, route resolution, model query index, or tool
  collection is constructed on publication. Changes in config, environment,
  catalog content, plugin inputs, or local probes publish a new setup. Identical
  inputs retain the same object/revision regardless of file timestamps or probe
  timing; rejected refreshes retain the last good setup. Old setups keep their
  captured snapshots and environment.
- Synchronous, single-flight accessors memoize successful loads independently
  per setup: `models()`, `providers()`, `models_effective()`,
  `providers_effective()`, `tools()`, `toolsets()`, `adapters()`, and `catalogs()`.
  Failed loads may retry without publishing partial values. Tool/plugin access
  does not materialize model views. Source snapshots can be released after
  successful model materialization. No persistent model-projection cache I/O.
- Build one routed model record for every catalog model. Store route readiness
  (`ROUTABLE`) and allow membership (`ALLOWED`) as independent flags in one
  status field. Memoize immutable ordered reference sequences: `models()` has
  all records, `models_effective()` has exactly the routable-and-allowed subset;
  both reference the same model objects. Providers retain file order; the
  effective provider view keeps only those used by effective models. Repeated
  access returns the same sequences.

### Allow policy, defaults, and queries

- An unset `allow.models` permits every model and preserves catalog order; no
  implicit provider-priority sort. A non-empty policy ranks matches by their
  first matching `tq-json` branch, stable by catalog position, followed by all
  unmatched models in original order. Unmatched records remain in the all view
  but are not allowed. An empty policy allows none without deleting records.
- Explicit model/compact settings retain validation and precedence. Otherwise,
  choose the first effective model as default and the first effective
  tool-call model for compaction.
- Model inspection and runnable model directives use transient `tq-json`
  evaluation against current records, not setup-level `ModelCollection` or
  `QueryDataset` indexes. A bare glob matches model IDs, a qualified identity
  matches a full composed ref; predicate fields remain the public model query
  fields. Sequence `=` and `!=` preserve legacy membership/nonempty-inequality
  semantics; explicit TQ `has no` also matches empty sequences. Inspection
  branches return first-match branch order; runnable `=`, `+=`, `-=` apply set
  operations within the inherited resource base and retain base order.

## Implementation and acceptance

Touchpoints: `src/toolang/setup/` (watcher, lazy setup, revisions), flat catalog
parsing and bundled data in `src/toolang/plugin/catalogs/models_dev/`, transient
model queries in `src/toolang/plugin/models/`, CLI/API inspection and execution
resource selection, and focused docs/tests. Keep unrelated tool/cap collections
unchanged.

- Test flat input validation and precedence, pinned bundle identity, order,
  override routing, and rejection of old formats.
- Test all/effective membership, immutable reference identity, flag independence,
  allow branch order, defaults/compact selection, TQ predicates and directive
  ordering, with no model query index built merely by loading setup.
- Test single-flight/retry behavior, unchanged/changed revisions, old-generation
  pinning, local probes, last-good setup, and no disk projection cache.
- Verify provider/model CLI output, including `--all`, and run the default
  offline ruff, ty, and pytest checks. Measure CLI process times separately
  from parsing/route costs; do not impose a wall-clock test threshold.

## Risks

- Previously supported nested/raw and `ref`-bearing flat catalogs must be
  converted outside Toolang. Catalog order now determines implicit default
  selection; old implicit provider priority is intentionally removed.
- `too providers` still resolves routes to count ready models on first access;
  `--all` increases formatting cost. Neither command is guaranteed to be
  limited by catalog parsing alone.
