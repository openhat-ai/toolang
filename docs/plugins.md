# Plugin Model

Toolang uses a small shared plugin boundary for runtime integrations.

Shared contracts and canonical value types live in:

- `toolang.base`

Plugins should raise `toolang.common.errors.ToolangError` for invalid Toolang
configuration, input, or runtime behavior that should be presented to users.


## Plugin Families

Current plugin families are:

- `tool`
- `channel`
- `sandbox`
- `model_provider`
- `model_adapter`


## Responsibility Boundary

Toolang core owns:

- runtime lifecycle
- queueing and scheduling
- durable records
- trace events
- HTTP API
- response projection

Plugins own only domain-specific behavior inside their family.

Plugins do not mutate durable runtime truth directly.


## Family Roles

### Tool

Tool plugins expose one `AgentToolSet`, which may return one or more
model-facing `AgentTool` values.

### Channel

Channel plugins ingest or deliver external messages.

### Sandbox

Sandbox plugins provide the runtime execution environment.

### Model Provider

Model provider plugins expose model discovery, default endpoint metadata, and
provider-specific target preparation.

### Model Adapter

Model adapter plugins execute one model turn for a resolved target and return
`ModelCallResult`.


## Loading

Toolang loads plugins from Python entry points:

- `toolang.tool`
- `toolang.channel`
- `toolang.sandbox`
- `toolang.model_provider`
- `toolang.model_adapter`

Toolang uses the same generic entry point loader for built-in and externally
installed plugins. Built-in implementations are registered through project
entry points and are not imported directly by family-specific loaders.

The built-in plugin packages are:

- `toolang.plugin.tools.*`
- `toolang.plugin.channels.*`
- `toolang.plugin.sandboxes.*`
- `toolang.plugin.models.providers.*`
- `toolang.plugin.models.adapters.*`

Each entry point names one factory such as `create_tool_set`, `create_channel`,
`create_sandbox`, `create_model_provider`, or
`create_model_adapter`. A single Python distribution or package may define
multiple Toolang plugin entry points, including multiple entry points in the
same family.

`toolang.plugin.loading` owns generic entry point discovery. Family loading
modules apply runtime-specific binding such as tool name encoding, model
provider configuration, channel bindings, or sandbox selection.


## Configuration Rule

The call site resolves environment variables and configuration values before
constructing plugin instances.

Core modules receive explicit plugin instances or explicit config objects. They
do not read environment variables on their own.
