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
| [program.md](./program.md) | Program declarations, executable signatures, agics, flows, directives, and surface rules |
| [flow-syntax.md](./flow-syntax.md) | Flow declarations, statements, result binding, and clauses |
| [input-syntax.md](./input-syntax.md) | ContentBody syntax, input perceiving, input coercion, output coercion, and content escaping |
| [layout.md](./layout.md) | Layout and storage, including Toolang root, agent home, and runtime room paths |
| [prepared-state.md](./prepared-state.md) | Immutable prepared generations, versioning, publication, and watching |
| [caps.md](./caps.md) | Capability model, including form, scope, origin, refs, precedence, and effective-cap rules |
| [selectors.md](./selectors.md) | Shared selector-list syntax for filters, runtime allow lists, and runnable directives |
| [tasks.md](./tasks.md) | Job model, including tasks, chores, scheduling, and thread mapping |
| [webui-jobs.md](./webui-jobs.md) | Web UI job board integration guide |
| [execution.md](./execution.md) | Execution boundaries, controls, threads, traces, and mandatory persistence |
| [executor.md](./executor.md) | RunExecutor acceptance, execution, control polling, and tracing |
| [run-step-records.md](./run-step-records.md) | Durable run, step, thread, and control records plus source events |
| [chat.md](./chat.md) | Chat and transcript model, including thread, run, message, and stream behavior |
| [execution-presentation.md](./execution-presentation.md) | Shared inspect, script, and chat TUI presentation language for runs, steps, streaming, and scrollback |
| [execution-transcript.md](./execution-transcript.md) | Compact linear execution transcript grammar, repeat context, verbosity, and bounded batch work |
| [models.md](./models.md) | Model integrations, including selectors, providers, routes, and built-in model providers |
| [tools.md](./tools.md) | Tool runtime, including built-in tools and service-cap integration |
| [plugins.md](./plugins.md) | Plugin model, including shared contracts, plugin families, and loading |
| [api.md](./api.md) | Control surfaces, including the CLI and local agent HTTP API |


## Implementation Targets

| Document | Scope |
| --- | --- |
| [refactor-target.md](./refactor-target.md) | Target package structure, core classes, dependency direction, CLI split, and migration order |


## Generated Reference

Use `reference/` for generated package and module reference derived directly
from code.
