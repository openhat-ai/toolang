# Plugin Model

Toolang uses a small shared plugin boundary for runtime integrations.

Shared contracts and canonical value types live in:

- `toolang.base`


## Plugin Families

Current plugin families are:

- `tool`
- `loop`
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

### Loop

Loop plugins decide how to sequence:

- model calls
- tool calls
- run completion

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
- `toolang.loop`
- `toolang.channel`
- `toolang.sandbox`
- `toolang.model_provider`
- `toolang.model_adapter`

Toolang uses the same generic entry point loader for built-in and externally
installed plugins. Built-in implementations are registered through project
entry points and are not imported directly by family-specific loaders.

The built-in plugin packages are:

- `toolang.tools.*`
- `toolang.loops.*`
- `toolang.channels.*`
- `toolang.sandboxes.*`
- `toolang.models.providers.*`
- `toolang.models.adapters.*`

Each entry point names one factory such as `create_tool_set`, `create_loop`,
`create_channel`, `create_sandbox`, `create_model_provider`, or
`create_model_adapter`. A single Python distribution or package may define
multiple Toolang plugin entry points, including multiple entry points in the
same family.

Plugin packages do not own loader modules. Core runtime code asks the generic
loader for a family by entry point group, then applies runtime-specific binding
logic such as tool name encoding or model provider configuration.


## Configuration Rule

The call site resolves environment variables and configuration values before
constructing plugin instances.

Core modules receive explicit plugin instances or explicit config objects. They
do not read environment variables on their own.
