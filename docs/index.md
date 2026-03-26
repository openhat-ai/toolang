# Toolang Design Index

Toolang is a local-first agent runtime built around authored `.too` source
files, synced capabilities, durable local state, and explicit long-lived
runtime processes.

This directory holds the design documents for the runtime. Each document owns
one topic so the same rule does not need to be redefined in many places.

Generated implementation reference does not live here. It belongs under
`reference/`.


## Design Map

- [model.md](./model.md)
  - canonical top-level terminology
  - agent, incarnation, activation, definition, capability, thread, run, and
    step
  - API boundary rules between definitions, capabilities, lifecycle, and
    runtime execution
- [layout.md](./layout.md)
  - canonical agent identity
  - Toolang root, agent home, and agent room layout
  - path rules for execution state, prompt traces, sync state, and sandbox
    staging
- [capabilities.md](./capabilities.md)
  - capability kinds, forms, scopes, and refs
  - sync materialization rules
  - runtime visibility and `/api/v1/caps` projection
- [execution.md](./execution.md)
  - runtime loops and execution strategies
  - activation, thread, run, and step mechanics
  - scheduler, pulse, truth layers, and projections
- [chat.md](./chat.md)
  - chat as a projection over threads, runs, and ordered messages
  - AI SDK-compatible stream mapping
  - message-part ordering and persistence rules
- [collaboration.md](./collaboration.md)
  - normalized run-submission model
  - chat and task as collaboration primitives
  - thread-oriented scheduling and handoff rules
- [tasks.md](./tasks.md)
  - task, chore, and will definitions
  - RRULE-driven scheduled work
  - local task files and remote mirror guidance
- [tools.md](./tools.md)
  - built-in runtime tool families
  - tool loading and service capability integration
  - tool-call recording as run steps
- [api.md](./api.md)
  - CLI surface
  - registry state
  - per-agent HTTP API and shared bus API
- [plugins.md](./plugins.md)
  - plugin families
  - plugin loading model
  - runtime/plugin responsibility boundary
- [memory.md](./memory.md)
  - memory plugin contract
  - recall/remember timing
  - diagnostics and failure behavior
- [impl.md](./impl.md)
  - selected stack
  - package boundaries
  - storage choices and implementation constraints
- [python.md](./python.md)
  - Python naming and module-boundary guidance
  - how code should reflect the design vocabulary directly


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
- `incarnation`
  - the interval from agent creation to agent removal
- `activation`
  - the interval from agent start to agent stop
- `runtime loop`
  - a long-lived trigger source such as `server`, `poll`, `hook`, or `pulse`
- `execution strategy`
  - the strategy used to complete one run, such as `direct` or `react`
- `thread`
  - a durable execution context
- `run`
  - one concrete handling attempt inside one thread
- `step`
  - one internal part of one run
- `message`
  - one ordered chat message attached to a run in a thread
- `message part`
  - one ordered content, tool, source, or file unit inside a message
- `skill`, `service`, `prompt`, `psyche`
  - the four current capability kinds


## Design Rules

- Cross-document terminology lives in [model.md](./model.md).
- Filesystem and identity rules live in [layout.md](./layout.md).
- Capability visibility and sync rules live in
  [capabilities.md](./capabilities.md).
- Runtime truth is local-first. Shared bus state is a projection.
- Definition endpoints expose authored state only. Runtime history belongs to
  runs and threads.
- `invoke` is a one-shot process surface. Long-lived behavior lives under
  runtime loops and activations.
- Plugins stay at the edge. Core runtime owns lifecycle, scheduling, state,
  and diagnostics.
