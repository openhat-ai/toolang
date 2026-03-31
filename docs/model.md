# Toolang Core Model

This document defines the top-level lifecycle, caps, definition, and
runtime objects used across Toolang.

Exact filesystem paths live in [layout.md](./layout.md).
Exact cap resolution and sync behavior live in
[caps.md](./caps.md).
Exact runtime scheduling and message-flow mechanics live in
[execution.md](./execution.md).
Exact API surfaces live in [api.md](./api.md).

This document is the source of truth for cross-document terminology.


## 1. System Layers

Toolang uses five top-level layers:

- identity and lifecycle
- authored definitions
- caps
- runtime execution
- projections and diagnostics

These layers are related, but they must not be collapsed into one object
model.


## 2. Agent, Incarnation, And Activation

`agent`

- one canonical identity
- identified by canonical agent URI

`incarnation`

- the interval from `created` to `removed`
- answers: "does this agent currently exist as a managed entity?"
- an incarnation is an identity and registry concept

`activation`

- the interval from `started` to `stopped`
- answers: "is this agent currently online?"
- an activation is a runtime-process concept

Rules:

- one agent may have many incarnations over time
- one incarnation may have many activations over time
- one activation belongs to exactly one incarnation
- stopping an activation does not remove the incarnation
- removing an agent ends the current incarnation and prevents future
  activations inside that incarnation


## 3. Authored Definitions

Toolang has durable authored definitions that describe work or configuration
without being executions themselves.

Important built-in definitions include:

- agent program
  - the authored `.too` source
- task
  - one durable work definition owned by the agent
- chore
  - one durable recurring definition owned by the agent
  - trigger policy is defined by `rrule`
- will
  - one durable agent-local long-horizon definition
  - trigger policy is defined by `rrule`

Rules:

- definitions are not runs
- definition state must not be overloaded with latest runtime state
- task status is definition status, not run status
- chore and will define trigger policy, not run history

`chat` is not an authored definition. It is a runtime input source that creates
runs inside threads.


## 4. Caps

Caps are composable agent primitives that shape what an activation and its runs
can see and do.

Current cap kinds:

- `skill`
- `service`
- `prompt`
- `psyche`

One authored cap input is a `cap definition`.

A cap definition carries:

- `kind`
- `name`
- `scope`
- `source`
- `locator`

Current canonical scopes:

- `agent`
- `home`
- `global`

Current canonical sources:

- `inline`
- `local`
- `remote`

Rules:

- `scope` controls both visibility and same-name precedence
- `source` identifies where the authoritative definition comes from
- `locator` identifies the authoritative definition regardless of source
- cap identity and sync state are separate from runtime execution state
- sync resolves authored cap inputs into local runtime-ready artifacts
- an activation starts from one prepared agent plus one effective visible cap
  set
- runs inherit the effective caps of their activation
- steps may use tools or prompts made available by those caps
- `/api/v1/caps` describes the effective caps visible to the current
  activation, not the authored history of every cap definition
- the public caps API uses `service` and `services`, not `server` and
  `servers`

Caps influence prompt building, tool availability, and behavior, but
they are not themselves runs, threads, or steps.


## 5. Thread, Run, And Step

`thread`

- one durable context or topic
- groups related runs
- may outlive a single activation

`run`

- one complete handling attempt inside one thread
- answers: "what did the agent do this time?"
- carries runtime fields such as origin, status, timestamps, input, output, and
  error state

`step`

- one internal unit inside one run
- examples:
  - prompt build
  - model call
  - tool call
  - outbound delivery

Rules:

- one thread may contain many runs
- one run belongs to exactly one activation
- one run belongs to exactly one thread
- one run may have many steps
- steps do not replace runs as the durable runtime unit


## 6. Mapping Definitions To Threads And Runs

Built-in sources map to runtime objects like this:

- `chat`
  - creates runs in a caller-selected or transport-selected thread
- `task`
  - one durable definition that usually owns one stable thread identity
- `chore`
  - one durable recurring definition that uses one stable derived thread
    identity
- `will`
  - one durable agent-local definition that uses one stable derived thread
    identity
- `invoke`
  - one direct run, often with an ephemeral thread

Rules:

- tasks, chores, and will expose definition data through definition endpoints
- latest or historical runtime activity for those definitions is queried
  through runs
- thread identity is the durable bridge between definition state and runtime
  history


## 7. Chat And Messages

Chat is a specialized view over runs and messages, not a separate execution
hierarchy.

Rules:

- chat history lives in threads
- one chat send or reply creates one run
- ordered chat messages are canonical transcript data attached to runs in chat
  threads
- the scheduler and truth model stay centered on run, not on chat as a
  separate runtime primitive


## 8. Projections And Derived State

Toolang also keeps derived or projected state that should not redefine the core
model.

Examples:

- shared bus events
- prompt traces
- pulse state for local task, chore, and will scanning
- derived status summaries in WebUI responses

Rules:

- projections may summarize lifecycle or latest activity
- projections must not replace the primary meaning of definitions, caps,
  threads, runs, or steps
- latest task activity is runtime state derived from runs or pulse state, not a
  substitute for task definition status


## 9. API Design Implications

The API should reflect the same model boundaries.

Definition endpoints:

- return authored definition state only
- examples:
  - tasks
  - chores
  - will

Caps endpoints:

- return effective cap visibility and materialization views
- do not mix cap metadata with run history

Runtime endpoints:

- center on `run`, `thread`, and `step`
- `run` is the public runtime unit for one concrete execution
- `thread` is the public context unit for related runs
- `step` is the public detail unit inside one run

Lifecycle endpoints:

- expose current incarnation and activation state when needed
- do not overload `run` to mean "agent is online"


## 10. Naming Guidance

Preferred public terminology:

- `run`
- `thread`
- `step`

Preferred lifecycle terminology:

- `incarnation`
- `activation`

Guidance:

- use spaced forms for concept names in prose, such as `agent uri` and
  `agent id`
- use underscore forms for concrete field names, such as `agent_uri` and
  `agent_id`
- use `run_id` only for one concrete run
- do not use `run` for an activation interval
- do not expose `turn` as the main public runtime noun
- do not use `session` for activation, because it is easy to confuse with
  client or auth sessions


## 11. Canonical Relationship Summary

The intended high-level relationship is:

- one `agent` has many `incarnations`
- one `incarnation` has many `activations`
- one `activation` has many `runs`
- one `thread` has many `runs`
- one `run` has many `steps`
- one `activation` uses one effective visible cap set
- tasks, chores, and will definitions emit runs but are not runs
