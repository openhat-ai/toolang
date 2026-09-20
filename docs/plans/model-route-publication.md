# Publish effective model routes through setup

Status: approved for implementation in PR #554.

## Goal

Setup resolves connection facts once per published version. CLI and executor
consume those facts, and adapters receive a Model carrying its effective route.
Catalog source caches remain independent of process credentials, readiness, and
allow policy. Durable records and their serialized formats stay unchanged.

## Data contract

Keep the existing Model and Provider catalog fields as source facts. Extend the
existing Toolang metadata rather than introducing replacement model types:

```python
class ModelToolang:
    provider: str
    ready: bool
    route: ModelRoute

class ModelRoute:
    adapter: str | None
    api: str | None
    env: ResolvedEnv | None
    headers: Mapping[str, str]
    options: Mapping[str, object]
```

ModelRoute no longer repeats the owning provider ID. All nested route values are
immutable. Source models have an empty, unresolved route and ready=False; those
placeholder values are omitted from source-cache documents.

For published routes:

- adapter is the selected installed adapter name, or None when resolution or
  installation is missing, or the selected catalog mode is invalid.
- api is the expanded effective endpoint, or None when absent or unresolved.
- env is the satisfied normalized environment rule, containing names only;
  None means unsatisfied, while () means no environment is required.
- headers and options contain the effective merged values; empty mappings are
  valid and do not affect readiness.
- Each field is resolved independently. A model is ready exactly when adapter,
  api, and env are all non-None. Allow membership is a separate policy result.

A declared `provider.mode` must select an object in `experimental.modes`.
A missing or non-object selection makes only that model unavailable:
`route.adapter=None`, empty effective headers/options, and independently resolved
API/env. Preserve its source declarations in the full view and source cache.
Other models continue to publish, even when allow policy excludes the invalid
model. A later valid catalog revision restores readiness. Existing validation
of explicitly configured default/compact models remains in force.

No issues field or diagnostic type is added. Inspection reports coarse failure
categories from None fields; it does not promise exact missing variable names
or distinguish an unknown adapter from an uninstalled one.

ProviderToolang also gains a default route. Its existing adapter and env fields
retain their role as trusted catalog declarations, needed by local and custom
catalogs. Setup puts effective values in route instead of overwriting source
facts. Model-level provider overrides remain source facts; setup stops injecting
its resolved adapter into Model.provider._toolang.

## Persistence and publication

Split the currently shared source-cache and published-snapshot codec entry
points. They may share field helpers, but their persisted content differs:

1. Source-cache encoding stores complete catalog declarations, model ownership,
   and trusted plugin declarations. It omits ready and effective route at every
   level. Preserve the distinction between trusted typed plugin declarations
   and arbitrary raw _toolang mappings. Raw models.dev metadata must not gain
   plugin configuration authority through a cache round trip.
2. Setup loads and merges those source snapshots, resolves provider defaults
   and model overrides using its adapter registry and captured environment,
   then applies readiness and allow filtering for its default view.
3. The private full-view encoding includes resolved routes and readiness. It
   remains an in-memory serialized payload pinned to the setup revision; it is
   not another file and is never read back from a mutable source cache.
4. Environment, adapter, and configuration changes invalidate setup publication
   through its existing revision inputs. Environment-only changes do not alter
   source-cache content or source revisions. Independent catalog changes and
   local probe results can still legitimately change source files.
5. Bump the cache schema to reject documents with the previous mixed contract.

Default typed collections contain ready, allowed models and exactly their
providers. setup.model_catalog(all=True) decodes the pinned complete resolved
view, including unavailable, excluded, and empty-provider entries. Environment
values stay in AgentSetup.envs; expanded endpoints are memory-only. Catalog JSON
exports retain source fields and omit Toolang metadata.

## Consumers

Move effective route resolution from plugin/models/provider_resolver.py into
the setup package. Neither CLI nor executor performs route resolution.

Adapters use invoke(model, request, *, environ) and
stream(model, request, *, environ, on_event). They read connection facts from
model._toolang.route and ownership from model._toolang.provider. Setup supplies
the immutable route; executor supplies only its declared environment values
from the run-pinned setup. Adapters retain ownership of wire translation and
credential selection. No adapter reads the process environment.

CLI models uses published route fields for query/display and derives generic
unavailability labels from None fields. Providers aggregates adapters and ready
counts from the selected nested models; an empty provider can display its
published default route. The provider API column explicitly represents the
default endpoint and marks differing model endpoints as overrides. Environment
requirements shown for unavailable entries are catalog declarations, not a
fresh CLI availability calculation. Provider availability means at least one
ready model, independently of default-route readiness.

Both commands keep their default/--all selection rules. Any allow-exclusion
label is separate from readiness. Formatting and counting remain CLI concerns;
source loading, route selection, template expansion, and availability decisions
belong to setup.

## Implementation touchpoints

- base/types/model.py and base/protocols/model.py: route metadata and signatures.
- setup/cache.py, setup/watcher.py, and a setup-owned resolver: separate codecs,
  route construction, publication, and version pinning.
- plugin/models/provider_resolver.py and collections.py: migrate resolution and
  make query views consume published routes.
- plugin/adapters/: consume Model metadata; preserve provider-wire behavior.
- execution/executor/frame.py and other adapter call sites: consume the pinned
  Model route without rebuilding it.
- cli/toolang/commands/model_catalog.py: remove API/adapter/environment inference.
- docs/models.md and docs/plans/model-plugins.md: align the implemented contract.

Records files and codecs are out of scope. Residual runtime/query package
relocation is not required for this change.

## Acceptance

- Cold and warm source-cache loads preserve all catalog and trusted plugin
  declarations; neither cache contains ready or an effective route.
- Changing only credentials or installed adapters changes setup routes/readiness
  without changing an otherwise unchanged source-cache file or revision.
- Cover each missing route field, multiple simultaneous failures, empty env
  requirements, and mixed AND/OR credential alternatives without storing values.
- Provider defaults and model overrides resolve consistently, including mixed
  adapters and endpoints; source declarations are unchanged after resolution.
- CLI and executor consume identical pinned routes. Refresh, source replacement,
  or cache deletion cannot alter an older setup's default or full view.
- Invalid catalog modes do not reject startup or refresh; cold/warm loads,
  allow exclusion, recovery, and old snapshot stability retain this behavior.
- Default and --all membership, nested provider membership, empty providers,
  query adapter fields, and catalog-only JSON exports retain their contracts.
- Built-in adapter request/stream behavior remains equivalent, and existing
  durable serialization regressions continue to pass.
- Run ruff check, ruff format --check, ty check, and the default offline pytest
  suite before implementation handoff.

## Tradeoffs

Resolved routes intentionally duplicate derived connection facts in memory.
This buys one consistent published result without repeating resolution at every
consumer. None-based diagnostics intentionally provide only broad categories.
The adapter signature is a plugin API change, so external adapters need the same
migration as built-ins; no legacy-signature compatibility layer is proposed.

No open decisions remain within this approved scope.
