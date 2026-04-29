# Developer Documentation

This directory contains the developer-facing documentation for the current
Toolang runtime.

User-facing quickstart and guide content belongs in the separate
`toolang-docs` site. Generated implementation reference belongs under
`reference/`.


## Scope

Use this directory for:

- runtime concepts and terminology
- filesystem layout and durable state
- capability, job, and execution models
- CLI and local agent API surfaces
- plugin and integration boundaries


## Document Map

| Document | Scope |
| --- | --- |
| [concepts.md](./concepts.md) | Developer overview and core runtime vocabulary |
| [ids.md](./ids.md) | Toolang-owned id families, reversible encoding, and durable allocator model |
| [program.md](./program.md) | Program syntax, including `struct`, `slash`, thunk signatures, directives, and surface rules |
| [layout.md](./layout.md) | Layout and storage, including Toolang root, agent home, and runtime room paths |
| [caps.md](./caps.md) | Capability model, including visibility, origins, inclusions, refs, precedence, and effective-cap rules |
| [tasks.md](./tasks.md) | Job model, including task, chore, will, and thread mapping |
| [webui-jobs.md](./webui-jobs.md) | Web UI job board integration guide |
| [execution.md](./execution.md) | Execution model, including durable records, trace events, and response events |
| [chat.md](./chat.md) | Chat and transcript model, including thread, run, message, and stream behavior |
| [models.md](./models.md) | Model integrations, including selectors, providers, routes, and built-in model providers |
| [tools.md](./tools.md) | Tool runtime, including built-in tools and service-cap integration |
| [plugins.md](./plugins.md) | Plugin model, including shared contracts, plugin families, and loading |
| [api.md](./api.md) | Control surfaces, including the CLI and local agent HTTP API |


## Generated Reference

Use `reference/` for generated package and module reference derived directly
from code.
