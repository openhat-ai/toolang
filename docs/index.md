# Toolang Design Index

Toolang is a local-first agent runtime built around authored `.too` source
files, synced caps, durable local state, and explicit long-lived runtime
processes.

This directory holds the design documents for the runtime. Each document owns
one topic so the same rule does not need to be redefined in many places.

Generated implementation reference does not live here. It belongs under
`reference/`.


## Design Map

- [model.md](./model.md)
  - canonical top-level terminology
  - agent, incarnation, activation, definition, caps, thread, run, and
    step
  - API boundary rules between definitions, caps, lifecycle, and
    runtime execution
- [layout.md](./layout.md)
  - canonical agent identity
  - Toolang root, agent home, and agent room layout
  - path rules for execution state, prompt traces, sync state, and sandbox
    staging
- [caps.md](./caps.md)
  - cap definitions, cap kinds, cap scopes, cap sources, and cap locators
  - sync materialization rules and effective cap-set derivation
  - authoring guidance for CLI/WebUI and `/api/v1/caps` runtime projection
- [execution.md](./execution.md)
  - durable, prepared, and live runtime state
  - runtime process, jobs, runs, and prompt assembly
  - prepare, inspect, control, and live refresh
- [chat.md](./chat.md)
  - chat as a projection over threads, runs, and ordered messages
  - AI SDK-compatible stream mapping
  - message-part ordering and persistence rules
- [models.md](./models.md)
  - canonical model refs and selector grammar
  - model plugin family and zero-config resolution
  - authored source, profiles, and `run/start --model` override rules
- [collaboration.md](./collaboration.md)
  - normalized run-submission model
  - chat and task as collaboration primitives
  - thread-oriented scheduling and handoff rules
- [tasks.md](./tasks.md)
  - task, chore, and will definitions
  - RRULE-driven scheduled work
  - local task files and remote mirror guidance
- [tools.md](./tools.md)
  - tool plugin family
  - tool loading and service cap integration
  - tool-call recording as run steps
- [service-auth.md](./service-auth.md)
  - runtime-managed OAuth callback relay for `service_use`
  - mcat 2.0 connection-file and callback-handoff integration
- [api.md](./api.md)
  - CLI surface
  - registry state
  - per-agent HTTP API and shared bus API
- [plugins.md](./plugins.md)
  - `memory`, `tools`, `channels`, and `sandbox` plugin families
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
- `agent uri`
  - the canonical identity string
- `agent id`
  - a short stable hash derived from `agent uri`
- `incarnation`
  - the interval from agent creation to agent removal
- `activation`
  - the interval from agent start to agent stop
- `runtime loop`
  - a long-lived trigger source such as `server`, `poll`, `hook`, or `pulse`
- `execution strategy`
  - the strategy used to complete one run, such as `basic` or `react`
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
- `cap definition`
  - one authored cap input
- `cap scope`
  - the visibility and precedence boundary for a cap definition
- `cap source`
  - where the authoritative cap definition comes from
- `cap locator`
  - the canonical pointer to the authoritative cap definition
- `effective cap set`
  - the cap set visible to one activation after scope precedence is
    applied
- `skill`, `service`, `prompt`, `psyche`
  - the four current cap kinds


## Design Rules

- Cross-document terminology lives in [model.md](./model.md).
- Filesystem and identity rules live in [layout.md](./layout.md).
- Caps visibility and sync rules live in
  [caps.md](./caps.md).
- Runtime truth is local-first. Shared bus state is a projection.
- Definition endpoints expose authored state only. Runtime history belongs to
  runs and threads.
- `invoke` is a one-shot process surface. Long-lived behavior lives under
  runtime loops and activations.
- Plugins stay at the edge. Core runtime owns lifecycle, scheduling, state,
  and diagnostics.
