# Plugin Model

Toolang uses a small shared plugin boundary for runtime integrations.

Shared contracts and canonical value types live in:

- `toolang.base`


## Plugin Families

Current plugin families are:

- `model`
- `run_strategy`
- `tool`
- `channel`
- `sandbox`


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

### Model

Model plugins perform one model turn.

They resolve selectors and return `ModelCallResult`.

### Run Strategy

Run strategies decide how to sequence:

- model calls
- tool calls
- run completion

### Tool

Tool plugins expose callable tool definitions and local invocation.

### Channel

Channel plugins ingest or deliver external messages.

### Sandbox

Sandbox plugins provide the runtime execution environment.


## Loading

Toolang loads plugins from Python entry points:

- `toolang.model`
- `toolang.run_strategy`
- `toolang.tool`
- `toolang.channel`
- `toolang.sandbox`

Built-in implementations ship in the corresponding `toolang.*` package.


## Configuration Rule

The call site resolves environment variables and configuration values before
constructing plugin instances.

Core modules receive explicit plugin instances or explicit config objects. They
do not read environment variables on their own.
