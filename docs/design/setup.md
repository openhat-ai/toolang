# Setup

`AgentSetup` is one immutable generation of installed runtime resources and
execution policy. [State](state.md) prepares authored language, caps, and
workspace declarations. Setup connects execution to installed plugins, model
catalogs, credentials, and process-environment facts. Neither publication
starts or resumes a Run.

## Preparation boundary

```text
placement-specific config + environment + entry points + catalog snapshots
  -> SetupWatcher candidate
  -> immutable AgentSetup generation
  -> independently materialized resource views
  -> captured RunSpec
```

Call sites supply concrete layout, sandbox, source paths, and policy overrides.
The watcher reads origin-aware configuration and captures catalog snapshots;
plugins receive their own resolved factory configuration. Core configuration
does not dereference a plugin's secret-reference fields. [Plugins](plugins.md)
owns those contracts and [Models](models.md) owns catalogs and connection routes.

Resident and visiting layouts use root/home configuration. Roaming layouts use
the captured project layers described in [Scripts](scripts.md#directories-and-configuration),
with relative paths bound to the declaring file. Project execution uses process
environment rather than resident configuration or project dotenv. For ordinary
layouts, root dotenv defaults are overlaid by home dotenv and then the process
environment. Each generation keeps its own copied environment map.
[Hosting](hosting.md) owns transport into a guest process.

## Policy

Policy resolves field by field from built-in defaults through root/home or
project configuration, process-derived overrides, and explicit CLI overrides.
`setup.config` parses source tables; `setup.models` validates configured model
choices when the lazy catalog loads. The watcher coordinates publication;
execution supplies handler/thread baselines and applies the shared typed model
operations. Request/session policy applies when preparing a Run. Model/tool
allow policy belongs to Setup; cap allow policy belongs to State. External
resource ceilings can only narrow the published bases. An empty allow set denies that resource;
a zero limit is active, while `none` disables a limit.

The configuration groups are `allow`, `model`, `limit`, `talk`, and `compact`.
Allow queries select common resources; model and limit settings supply ordinary
root defaults. Talk overlays model/limit settings for automatic message handling,
and compact settings apply to all thread kinds. Limits use the scopes defined by
[Execution](execution.md#run-execution). There is no runtime
default-runnable setting; [Drivers](drivers.md) resolve their handlers. Chat's
runnable default is session state.

[Queries](queries.md) owns matching. [Models](models.md#model-selection) owns model
expressions and compaction configuration. Startup CLI/environment overrides are
fixed watcher inputs; later configuration refreshes do not rewrite accepted Runs.

## Lazy views

Models/providers, adapters, catalogs, toolset plugins, and leaf tools are
separate generation-local views. Accessors materialize their dependencies and
memoize successful values with thread-safe single-flight loading. Listing
toolset plugins need not construct leaf tools, and inspecting tools need not
discover models.

A failed load publishes no partial value and permits another attempt. Lazy
memoization does not change captured inputs. All/effective views share the same
underlying resources; allow filtering does not mutate the complete snapshot.
[Queries](queries.md) owns their inspection projections.

## Publication and lifetime

Refreshes serialize candidate checks. A changed valid candidate replaces the
current Setup; an unchanged candidate reuses it. Rejection records diagnostics
and preserves the last valid Setup. Initial preparation fails when no prior
valid generation exists. Lazy resource failures remain visible to the accessor
that requested them.

The watcher retains only the current generation. Accepted Runs hold their own
references, keeping previous generations alive as needed; descendants inherit
the accepted Setup. Later State publication can update model-call cap/runnable
resolution, while installed models, tools, environment, and policy remain with
the captured Setup. [Execution](execution.md#state-capture) owns these boundaries.

Setup revisions are provenance, not a durable resource replay store. Views stay
in memory; no `.setup/` model cache or historical Setup table is maintained.
Model Steps retain their actual normalized calls for inspection.
