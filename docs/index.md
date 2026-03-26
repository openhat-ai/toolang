# Toolang Design Index

Toolang is a local-first agent runtime built around `.too` source files,
durable synced state, and explicit long-lived runtime processes.

This directory holds the design documents for the runtime. Each document owns
one topic so the same rules do not need to be repeated in multiple places.

Generated implementation reference does not live here. It belongs under
`reference/`.


## Design Map

- [model.md](./model.md)
  - top-level lifecycle and resource model
  - incarnation, activation, run, thread, and step
  - authored definitions, capability visibility, and runtime boundaries
- [layout.md](./layout.md)
  - canonical agent identity
  - Toolang root, agent home, and agent room layout
  - path rules for sync state, local caps, and sandbox staging
- [capabilities.md](./capabilities.md)
  - cap kinds, forms, and scopes
  - ref resolution
  - sync materialization and runtime overlay rules
  - cap-management command surface
- [execution.md](./execution.md)
  - message model
  - runtime loops and execution strategies
  - activation, run, thread, and step mechanics
  - scheduling, truth layers, and projection guidance
- [collaboration.md](./collaboration.md)
  - inbox and scheduler model for long-lived runtimes
  - chat, task, chore, invoke, and will as turn-request sources
  - minimal agent collaboration through chat and task
- [tasks.md](./tasks.md)
  - task as one cross-provider collaboration primitive
  - local markdown task-file rules
  - built-in task prompt and task-service expectations
- [tools.md](./tools.md)
  - built-in runtime tool families
  - local tool-provider loading
  - tool-call recording and diagnostics
- [api.md](./api.md)
  - CLI surface
  - registry state
  - agent API and bus API
- [chat.md](./chat.md)
  - canonical turn-message model
  - AI SDK stream mapping
  - ordered message-part persistence
- [plugins.md](./plugins.md)
  - plugin families
  - loading model
  - plugin/runtime boundary
- [memory.md](./memory.md)
  - memory plugin contract
  - recall/remember flow
  - memory-specific diagnostics and failure handling
- [impl.md](./impl.md)
  - chosen stack
  - package boundaries
  - implementation constraints
- [python.md](./python.md)
  - Python module and package style
  - docstrings, comments, and `__all__`
  - how code should express design directly


## Core Vocabulary

- `toolang root`
  - the local Toolang system directory
- `agent home`
  - the local directory that hosts one or more `.too` files
- `agent room`
  - the private machine-managed area for one agent
- `agent_uri`
  - the canonical identity string
- `agent_id`
  - a short stable hash derived from `agent_uri`
- `ref`, `inline`, `local`
  - the three cap forms
- `agent`, `shared`, `global`
  - the three cap scopes
- `runtime loop`
  - a long-lived trigger source such as `server` or `poll`
- `execution strategy`
  - the strategy used to complete one run, such as `direct` or `react`
- `incarnation`
  - the interval from agent creation to agent removal
- `activation`
  - the interval from agent start to agent stop
- `run`
  - one complete handling attempt inside a thread
- `thread`
  - a durable execution context
- `step`
  - an internal part of one run
- `message`
  - one ordered message emitted or persisted for a run, especially in chat views
- `message part`
  - one ordered content/tool/source/file unit inside a message


## Top-Level Surfaces

Main CLI groups:

- agent lifecycle
  - `new`, `clone`, `remove`, `list`
- state materialization
  - `sync`
- execution
  - `invoke`, `run`, `start`
- capability management
  - `skill`, `service`, `prompt`, `psyche`
- shared multi-agent API
  - `bus serve`

Hidden helper commands exist for local path workflows:

- `home`
- `source`
- `room`
- `init`


## Design Rules

- Top-level lifecycle and resource vocabulary lives in [model.md](./model.md).
- Layout and identity rules live in [layout.md](./layout.md).
- Capability semantics and sync rules live in
  [capabilities.md](./capabilities.md).
- Runtime execution truth is local-first. Shared bus state is a projection.
- `invoke` is a one-shot process surface. Long-lived behavior lives under
  runtime loops.
- Plugins stay at the edge. Core runtime owns lifecycle, scheduling, state, and
  diagnostics.
