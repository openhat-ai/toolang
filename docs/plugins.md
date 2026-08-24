# Plugin Model

Toolang uses small entry-point contracts for runtime integrations. Shared
protocols and canonical value types live in `toolang.base`. Plugins should
raise `toolang.base.errors.ToolangError` for user-facing configuration, input,
or runtime failures.

## Plugin Families

The public plugin families are:

- `tool`;
- `channel`;
- `sandbox`;
- `model_catalog`;
- `model_adapter`.

Toolang core owns runtime lifecycle, scheduling, durable records, trace events,
HTTP APIs, and response projection. Plugins own only their family-specific
integration behavior and do not mutate durable runtime truth directly.

## Family Roles

### Tool

Tool plugins expose one `AgentToolSet`, which may return one or more
model-facing `AgentTool` values. `AgentTool.invoke()` is asynchronous.

### Channel

Channel plugins ingest or deliver external messages.

### Sandbox

Sandbox plugins provide runtime execution environments.

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

- `toolang.tool`;
- `toolang.channel`;
- `toolang.sandbox`;
- `toolang.model_catalog`;
- `toolang.model_adapter`.

Built-in implementations are registered through the same entry-point mechanism
as external packages. The implementation packages are:

- `toolang.plugin.tools.*`;
- `toolang.plugin.channels.*`;
- `toolang.plugin.sandboxes.*`;
- `toolang.plugin.models` catalog implementations;
- `toolang.plugin.models.adapters.*`.

Each entry point names one factory such as `create_tool_set`, `create_channel`,
`create_sandbox`, `create_models_dev_catalog`, `create_ollama_catalog`, or
`create_model_adapter`. A distribution may register multiple entries in one or
more families.

`toolang.plugin.loading` owns generic entry-point discovery. Family-specific
loaders pass explicit configuration into factories and validate the returned
protocol.

## Configuration Rule

CLI and setup call sites resolve paths, environment values, endpoints, and
configuration layers before constructing plugins. Core modules receive
concrete plugin instances or configuration values; plugins do not read CLI
state implicitly.

Model catalog providers are resolved once after all catalog snapshots are
merged. The resolver maps raw npm metadata to an installed adapter, resolves a
concrete endpoint, interprets environment availability, and stores only
non-secret runtime facts in `Provider.resolved`. See [models.md](models.md) for
the complete boundary.
