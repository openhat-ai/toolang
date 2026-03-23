# Toolang Execution Model

This document defines Toolang runtime execution semantics.

Identity and filesystem paths live in [layout.md](./layout.md).
Control surfaces live in [api.md](./api.md).


## 1. Two Orthogonal Axes

Toolang uses two independent concepts:

- `runtime loop`
  - a long-lived trigger source
  - examples: `server`, `poll`, `hook`, `pulse`
- `execution strategy`
  - the strategy used to complete one turn
  - examples: `direct`, `react`, `plan_execute`

Rules:

- runtime loops decide when a turn starts
- execution strategies decide how that turn completes
- these concepts must not be merged


## 2. Message Model

Toolang uses one `Message` shape as the semantic input for one turn.

Minimal fields:

- `origin`
- `channel`
- `sender`
- `thread_id`
- `text`
- `meta`

`origin` values:

- `invoke`
- `chat`
- `task`
- `chore`
- `will`

`channel` is only for chat transport.

Common values:

- `tui`
- `webui`
- `api`
- `telegram`

`sender` values:

- `owner`
- `peer`
- `guest`
- `self`
- `service`

Rules:

- `origin == chat` requires a non-null `channel`
- `origin in {invoke, task, chore, will}` requires `channel = null`
- `task`, `chore`, and `will` use `sender = self`
- hook deliveries commonly use `sender = service`
- `run` and `start` are process surfaces, not message origins


## 3. Runtime Loops

Toolang defines four runtime loops:

- `server`
  - accepts local API requests
  - usually emits `chat` or `invoke`
- `poll`
  - polls external channels
  - usually emits `chat` or `task`
  - keeps one plugin-owned poll cursor per binding under `${AGENT_ROOM}/poll/`
- `hook`
  - accepts hook deliveries
  - usually emits `invoke`
- `pulse`
  - emits internal `task`, `chore`, and `will`

Rules:

- runtime loops create turn requests
- runtime loops do not execute turns directly
- `toolang invoke` starts no runtime loops
- `toolang run` starts only `server`
- `toolang start` starts a selected runtime-loop set
- the current runtime host serializes turns by `thread_id` even when multiple
  poll bindings are active


## 4. Execution Strategies

Execution strategy is a turn-local concern.

Examples:

- `direct`
  - one prompt build and one model completion
- `react`
  - repeated think/act/observe steps within one turn
- `plan_execute`
  - explicit planning followed by one or more execution steps

Rules:

- one turn uses one execution strategy
- one strategy may produce many internal steps
- strategy selection is independent from runtime-loop selection


## 5. Core Execution Objects

Toolang execution is organized around:

- `run`
- `thread_group`
- `thread`
- `turn`
- `step`

The local execution truth layer should live in `${AGENT_ROOM}/execution.db`.
The existing `${AGENT_ROOM}/agent.run` file remains a current-running summary,
not the historical execution truth.


## 6. Runs

A run is one continuous active interval of agent execution.

Examples:

- one foreground `toolang run`
- one background `toolang start`
- one one-shot `toolang invoke`

Suggested fields:

- `run_id`
- `run_kind`
  - `runtime`
  - `invoke`
- `agent_uri`
- `started_at`
- `finished_at`
- `status`
- `runtime_loops`
- `sandbox`
- `cap_scopes`
- `sync_fingerprint`
- `plugin_snapshot`

Rules:

- each run has a clear start and end
- the same agent may have many runs over time
- old runs remain queryable after later restarts
- one thread may span multiple runs
- every turn belongs to exactly one run


## 7. Thread Groups

A thread group is a scheduling category.

Suggested built-in groups:

- `chat`
- `task`
- `chore`
- `will`

Recommended policy fields:

- `priority`
- `max_running_turns`
- `max_queued_turns`
- `overflow_policy`
  - `reject`
  - `drop_oldest`
  - `replace_pending`
- `coalesce`

Default intent:

- `chat`
  - highest priority
- `task`
  - moderate concurrency
- `chore`
  - one running turn and one pending turn
- `will`
  - lowest priority and aggressive coalescing


## 8. Threads, Turns, And Steps

`thread`

- a durable execution context
- examples:
  - `telegram:12345678`
  - `api:thread-abc`
  - `task:<task_ref>`
  - `chore:<chore_key>`
  - `will:<agent_id>`

`turn`

- one complete handling attempt inside a thread
- belongs to one `thread` and one `run`
- carries:
  - `origin`
  - `sender`
  - `channel`
  - `execution_strategy`
  - `status`
  - timestamps
  - input/output/error state

`step`

- an internal part of one turn
- examples:
  - prompt build
  - model call
  - tool call
  - memory recall
  - outbound delivery

Rules:

- `task_ref` is treated as `thread_id`
- `chore` and `will` also map directly to `thread_id`
- at most one turn may run at a time for the same `thread_id`
- steps do not replace turns as the durable scheduling unit


## 9. Process Model

Toolang uses two execution modes:

- one-shot invoke process
- long-lived runtime process

### 9.1 One-Shot Invoke

`toolang invoke` is always an independent process.

Rules:

- it prepares one agent
- executes one turn
- writes local execution state
- emits shared bus projection events
- exits

Reason:

- invoke is commonly used by scripts, editors, CI, and Makefiles
- process isolation keeps one-shot execution simple

### 9.2 Long-Lived Runtime Process

`toolang run` and `toolang start` run one long-lived runtime process per
agent.

That process hosts:

- the selected runtime loops
- one shared turn scheduler
- one shared worker pool

Rules:

- runtime loops enqueue turn requests
- the scheduler enforces thread and thread-group constraints
- workers execute admitted turns


## 10. Scheduling

The scheduler should enforce:

1. `thread` serialization
2. `thread_group` budget limits

Rules:

- only one running turn per `thread_id`
- different threads may run concurrently when budgets allow
- higher-priority groups should win admission when capacity is limited

Recommended origin handling:

- `chat`
  - thread-scoped serialization
  - interactive priority
- `task`
  - task-ref-scoped serialization
  - moderate concurrency across different task refs
- `chore`
  - key-scoped serialization
  - repeated triggers should coalesce
- `will`
  - one low-priority agent-local thread
  - repeated triggers should coalesce
- `invoke`
  - one-shot process
  - not part of the long-lived in-process scheduler


## 11. Truth Layer And Projections

Execution should use three layers:

- agent-local execution truth
- shared bus projection
- outbound side effects

Rules:

- local execution state is written first
- shared bus projection is written second
- external delivery happens last

Recommended local truth-layer stores:

- `runs`
- `run_events`
- `threads`
- `turns`
- `turn_events`
- `steps`

Requirements:

- append-first
- durable across agent restarts
- enough raw facts to compute later projections
- not dependent on `bus/events.db`

Statistics and summaries should be projection outputs, not truth inputs.

The truth layer should preserve enough raw data to derive:

- wall time
- active time
- model time
- tool time
- memory time
- success rates
- token usage
- cache savings


## 12. Turn Lifecycle

Recommended write order:

1. ingress
  - resolve or create `thread`
  - resolve the active `run`
  - create queued `turn`
2. admission
  - scheduler marks the turn runnable
3. start
  - mark the turn `running`
  - append `run_started`
4. progress
  - write step records and prompt traces
5. finish or fail
  - update local turn state first
  - append `run_finished` or `run_failed`
  - perform outbound replies or callbacks last


## 13. Loop-Owned Sources

Recommended source files:

- `channels.toml`
  - external chat-channel configuration
- `hooks.toml`
  - hook declarations
- `tasks/`
  - local markdown task documents
- `chores/`
  - local markdown chore documents
- `will.md`
  - durable agent-local intent document

Recommended runtime state in the agent room:

- `poll/`
- `hooks/`
- `pulse.json`
  - persisted scheduling state plus latest run feedback for task, chore, and
    will scans

Current pulse-loop behavior:

- `task` turns are enqueued when a local task document changes and the task is
  still active for the current agent
- `chore` turns are enqueued when a local chore document becomes due
- `will` turns are enqueued from `will.md` on its configured interval
