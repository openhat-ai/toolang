# Plugin Model

Toolang uses small entry-point contracts for runtime integrations. Shared
protocols and canonical value types live in `toolang.base`. Plugins should
raise `toolang.base.errors.ToolangError` for user-facing configuration, input,
or runtime failures.

## Plugin Families

The public plugin families are:

- `toolset`;
- `channel`;
- `sandbox`;
- `model_catalog`;
- `model_adapter`.

Toolang core owns runtime lifecycle, scheduling, durable records, trace events,
HTTP APIs, and response projection. Plugins own only their family-specific
integration behavior and do not mutate durable runtime truth directly.

## Family Roles

### Toolset

Toolset plugins expose one `Toolset`, which may return one or more
model-facing `AgentTool` values. `AgentTool.invoke()` is asynchronous.

### Channel

Channel plugins ingest or deliver external messages.

### Sandbox

Sandbox plugins provide runtime execution environments.

`SandboxRef.runtime_id` is the immutable identifier used for lifecycle control.
Plugins also return a structured `runtime_kind` and optional `runtime_name` for
human inspection; they do not preformat CLI labels. The built-in host sandbox
uses `process`, Docker uses `container`, and the default is `workload`.

Core persists the reference before calling
`attach(plan, ref, progress=...)`. `attach` may start process-local observers
and emit `ProgressEvent` updates within the closed `runtime.create` and
`runtime.start` stages, but progress is presentation-only and must not decide
readiness or lifecycle state. Callbacks are never stored in a plan, request,
reference, or persisted state.

`attach` does not forward raw foreground output to the controller terminal.
For an inherited-output plan, `wait(ref)` begins forwarding only after core has
reported readiness and handed the terminal to foreground logs. Cancellation
stops forwarding before runtime cleanup progress begins.

Progress labels are complete, short, verb-first sentences. Running labels end
in `...`; checkpoints and terminal outcomes use simple past without terminal
punctuation; failures use `Failed to VERB`. Labels do not contain agent names,
commands, environment values, secrets, or raw logs. Useful bounded context is
part of the sentence when safe, while `detail` is reserved for diagnostics.

### Model Catalog

Model catalog plugins return immutable provider/model snapshots. Static and
local discovery use the same models.dev-compatible `Provider` and `Model`
types. Catalog plugins do not execute model calls or install packages named by
catalog metadata.

### Model Adapter

Model adapter plugins execute one model turn for a concrete `ModelTarget` and
return `ModelCallResult`. Both non-streaming and streaming calls are
asynchronous, and streaming adapters await their model-part handler.

Adapters own one protocol shape and its optional default endpoint. They do not
discover models, match providers, calculate availability, or own pricing.

## Loading

Toolang loads plugins from Python entry points:

- `toolang.toolset`;
- `toolang.channel`;
- `toolang.sandbox`;
- `toolang.model_catalog`;
- `toolang.model_adapter`.

Built-in implementations are registered through the same entry-point mechanism
as external packages. The implementation packages are:

- `toolang.plugin.toolsets.*`;
- `toolang.plugin.channels.*`;
- `toolang.plugin.sandboxes.*`;
- `toolang.plugin.models` catalog implementations;
- `toolang.plugin.models.adapters.*`.

Each entry point names one factory such as `create_toolset`, `create_channel`,
`create_sandbox`, `create_models_dev_model_catalog`,
`create_ollama_model_catalog`, or `create_model_adapter`. A distribution may
register multiple entries in one or more families.

`toolang.plugin.loading` owns generic entry-point discovery. Family-specific
loaders pass explicit configuration into factories and validate the returned
protocol.

Naming reflects cardinality: `create_<singular>` returns one selected plugin,
`load_<plural>` returns a collection, and `list_<plural>` discovers installed
entry points. File parsing uses `read_*`, such as
`read_model_catalog_snapshot`, rather than a plugin loader name.

Toolset entry-point names, effective `Toolset.name` identities, public
toolset names, and leaf names start with an ASCII letter and contain only ASCII
letters and underscores. They cannot contain `__`, which is reserved as the
provider-facing separator. A toolset plugin may return either a bare leaf key or
an explicit `<toolset>/<leaf>` key. Selectors use `<toolset>/<leaf>` while model
APIs receive `<toolset>__<leaf>`.

A toolset with exactly one leading underscore is internal to Toolang. Only
entry points whose installed distribution metadata identifies the `toolang`
distribution may register one; a `toolang.*` Python module target alone grants
no authority. External plugins may register one or more public toolsets but
cannot claim internal toolsets.

## Configuration Rule

CLI and setup call sites resolve paths, endpoints, and configuration layers
before constructing plugins. Core modules receive concrete plugin instances or
configuration values; plugins do not read CLI state implicitly.

Every plugin uses the same canonical TOML shape:

```toml
[plugin.toolset.fs]
max_chars = 20000

[plugin.sandbox.docker]
root = "/root/.toolang"
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'

[plugin.model_catalog.ollama]
timeout = 3

[plugin.model_adapter.responses]
```

The grammar is `[plugin.<family>.<entry-point-name>]`. Root configuration is
merged with agent configuration, with the agent winning at each key and nested
tables preserved. Plugin configuration is passed through unchanged: the
configuration layer never dereferences environment-variable names or resolves
secrets. A concrete implementation owns those semantics at its runtime
boundary.

Each factory receives a fresh mapping whose authored values come only from its
own merged table. Core may add concrete runtime inputs owned by that plugin,
such as the selected static catalog path or process environment, but it does
not expose peer plugins, other families, the full `[plugin]` table, or other
core configuration. Installed collection plugins receive an empty mapping when
they have no table. Removed shapes such as `[tools]`, `[channels]`,
`[sandbox.config]`, and `[models.catalogs]` are invalid; there is no legacy
fallback or merge path.

Core selection remains separate from plugin-owned configuration. For example,
`[sandbox]` selects `driver` and optional `target`, while
`[plugin.sandbox.<driver>]` configures the selected implementation. Sandbox
lifecycle recovery re-reads the current root and agent plugin tables instead
of persisting plugin configuration or secrets in runtime state.

Authored plugin configuration is intended for non-sensitive values and secret
references such as environment-variable names, never secret values. Concrete
implementations must validate their own sensitive fields because a generic
mapping loader cannot infer whether an arbitrary string is sensitive.
Sandbox-specific dotenv materialization is runtime transport and does not
change or resolve a plugin's configuration mapping.

The Docker sandbox exposes all names explicitly authored in the root or agent
`.env`. Host-process-only names must match `environment_allow_pattern`, which is
a configurable full-match regular expression with a built-in allowlist for
Toolang, bootstrap, proxy, certificate, and common model-provider variables.
The concrete plugin still resolves any exposed secret-reference name from its
guest process environment.

Only configured external model catalogs are instantiated; the three built-in
catalogs are always loaded. After snapshots are merged, the resolver maps raw
npm metadata to installed adapters, resolves provider and model routes,
interprets environment availability, and stores only non-secret runtime facts.
See [models.md](models.md) for the complete boundary.
