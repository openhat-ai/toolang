# Toolang Design Index

Toolang is a local-first agent runtime built around `.too` source files,
durable synced state, and explicit long-lived runtime processes.

This directory holds the design documents for the runtime. Each document owns
one topic so the same rules do not need to be repeated in multiple places.

Generated implementation reference does not live here. It belongs under
`reference/`.


## Design Map

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
  - activation, thread, turn, and step
  - scheduling, truth layers, and projection guidance
- [api.md](./api.md)
  - CLI surface
  - registry state
  - agent API and bus API
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
  - the strategy used to complete one turn, such as `direct` or `react`
- `activation`
  - one continuous active interval of one agent process
- `thread`
  - a durable execution context
- `turn`
  - one complete handling attempt inside a thread
- `step`
  - an internal part of one turn


## Top-Level Surfaces

Main CLI groups:

- agent lifecycle
  - `new`, `clone`, `remove`, `list`
- state materialization
  - `sync`
- execution
  - `invoke`, `serve`, `start`
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

- Layout and identity rules live in [layout.md](./layout.md).
- Capability semantics and sync rules live in
  [capabilities.md](./capabilities.md).
- Runtime execution truth is local-first. Shared bus state is a projection.
- `invoke` is a one-shot process surface. Long-lived behavior lives under
  runtime loops.
- Plugins stay at the edge. Core runtime owns lifecycle, scheduling, state, and
  diagnostics.
