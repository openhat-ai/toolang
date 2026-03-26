# Toolang Execution Model

This document defines Toolang runtime execution semantics.

Identity and filesystem paths live in [layout.md](./layout.md).
Top-level lifecycle and runtime-resource vocabulary lives in
[model.md](./model.md).
Control surfaces live in [api.md](./api.md).
Chat and message semantics live in [chat.md](./chat.md).


## 1. Two Orthogonal Axes

Toolang uses two independent concepts:

- `runtime loop`
  - a long-lived trigger source
  - examples: `server`, `poll`, `hook`, `pulse`
- `execution strategy`
  - the strategy used to complete one run
  - examples: `direct`, `react`

Rules:

- runtime loops decide when a run submission enters the scheduler
- execution strategies decide how an admitted run completes
- these concepts must not be merged


## 2. Runtime Input Model

Toolang uses one normalized runtime input message.

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

`channel` is only for transport-facing chat ingress.

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

- `origin == chat` usually carries a non-null `channel`
- `origin in {invoke, task, chore, will}` usually carries `channel = null`
- `task`, `chore`, and `will` commonly use `sender = self`
- hook or provider deliveries commonly use `sender = service`
- runtime input messages are not themselves runs; they become run submissions


## 3. Run Submissions

Runtime loops do not execute work directly.

They normalize input into `run submissions` and place those submissions into the
runtime scheduler.

A run submission carries:

- `kind`
  - one of the runtime origins
- `thread_id`
  - the target durable context
- `message`
  - the normalized runtime input, when the source has one

Rules:

- chat, task, chore, invoke, and will all become run submissions
- the scheduler admits run submissions and creates durable run records
- the queue should be modeled around run submissions, not around activations


## 4. Activation, Thread, Run, And Step

`activation`

- one online interval from `started` to `stopped`
- created by `toolang run`, `toolang start`, or one-shot `toolang invoke`

`thread`

- one durable execution context
- examples:
  - `telegram:12345678`
  - `api:thread-abc`
  - `task:local:<task_id>`
  - `chore:<chore_id>`
  - `will:<agent_id>`

`run`

- one concrete handling attempt inside one thread
- belongs to exactly one activation and exactly one thread
- carries:
  - `origin`
  - `sender`
  - `channel`
  - `execution_strategy`
  - `status`
  - timestamps
  - input, output, and error state

`step`

- one internal part of one run
- examples:
  - prompt build
  - model call
  - tool call
  - delivery

Rules:

- one activation may contain many runs
- one thread may contain many runs across many activations
- one run may have many steps
- steps do not replace runs as the durable scheduling unit
- current persisted step records capture completed step outcomes and timestamps
  rather than a separate long-lived `running` step state


## 5. Runtime Loops

Toolang defines four runtime loops:

- `server`
  - accepts local API requests
  - usually emits `chat` or direct `run` submissions
- `poll`
  - polls external channels
  - usually emits `chat` or `task`
  - keeps one plugin-owned poll cursor per binding under `${AGENT_ROOM}/poll/`
- `hook`
  - accepts hook deliveries
  - usually emits `invoke`
- `pulse`
  - scans local tasks, chores, and will definitions
  - emits `task`, `chore`, and `will`

Rules:

- runtime loops create run submissions
- runtime loops do not execute runs directly
- `toolang invoke` starts no long-lived runtime loop
- `toolang run` starts the `server` loop and may add others with `--loop`
- `toolang start` starts the selected runtime-loop set in the background


## 6. Execution Strategies

Execution strategy is a run-local concern.

Examples:

- `direct`
  - one prompt build and one model completion
- `react`
  - repeated think/act/observe steps within one run

Rules:

- one run uses one execution strategy
- one strategy may produce many internal steps
- strategy selection is independent from runtime-loop selection


## 7. Scheduler Policy

The current useful scheduler policy is:

- serialize by `thread_id`
- allow different threads to run concurrently
- apply group-level budgets by origin-derived thread group

Built-in groups:

- `invoke`
- `chat`
- `task`
- `chore`
- `will`

Default intent:

- `chat`
  - highest priority
- `task`
  - medium priority
- `chore`
  - low priority
- `will`
  - lowest priority

Rules:

- at most one running run may exist at a time for the same `thread_id`
- different threads may still run concurrently when scheduler budget allows it
- thread grouping influences concurrency policy, but the durable unit remains
  the run


## 8. Threads As The Durable Bridge

Thread identity is the durable bridge between definitions and runtime history.

Built-in mapping:

- `chat`
  - caller-selected or transport-selected thread
- `task`
  - stable derived thread from local task identity
- `chore`
  - stable derived thread `chore:<chore_id>`
- `will`
  - stable derived thread `will:<agent_id>`
- `invoke`
  - often an ephemeral thread

Rules:

- thread history may outlive the activation that produced a run
- definition endpoints may stay definition-only even when runtime history is
  later queried through the same thread
- chat thread summaries keep a stable `title` and a rolling `preview`


## 9. Chat Projection

Chat is a projection over threads, runs, and ordered messages.

Rules:

- one inbound chat send creates one run
- the runtime persists ordered chat messages separately from run records
- `/api/v1/threads/{thread_id}` returns thread metadata, related runs, and
  ordered messages together
- `/api/v1/chats*` is an alias over the same thread model

Chat message ordering and SSE semantics are defined in [chat.md](./chat.md).


## 10. Pulse And Scheduled Definitions

`pulse` is the local scheduler loop for definitions owned by the agent room.

Task behavior:

- local task documents are scanned under `${AGENT_ROOM}/tasks/`
- non-paused tasks with non-terminal task status may enqueue task runs when the
  definition changes

Scheduled definition behavior:

- chores live under `${AGENT_ROOM}/chores/*.md`
- will lives at `${AGENT_ROOM}/will.md`
- both are scheduled by `rrule`

Rules:

- chore and will definitions use RRULE-driven scheduling
- new or updated scheduled definitions are enqueued once immediately
- future due times are computed from the stored `rrule`
- pulse state is a projection used for local scheduling, not the canonical run
  history


## 11. Truth Layers And Projections

Toolang keeps multiple durable layers with different meanings.

Primary local truth:

- `${AGENT_ROOM}/execution.db`
  - activation, thread, run, and step records
- `${AGENT_ROOM}/chats/chats.db`
  - ordered chat messages attached to runs in threads

Derived or diagnostic state:

- `${AGENT_ROOM}/runs/{RUN_ID}/prompt.json`
  - prompt-build diagnostics for one run
- `${AGENT_ROOM}/pulse.json`
  - local scan state and scheduling projection
- `${TOOLANG_ROOT}/bus/events.db`
  - shared cross-agent event projection

Rules:

- `execution.db` is the runtime execution truth
- chat history is durable presentation data linked to runs and threads
- bus events and pulse state are projections
- prompt traces are diagnostics, not the canonical source of run status


## 12. Current Status Guidance

The intended stable public runtime model is:

- lifecycle
  - `incarnation`
  - `activation`
- execution
  - `thread`
  - `run`
  - `step`
- chat view
  - ordered messages attached to runs in threads

Definition objects such as tasks, chores, and will remain separate from runtime
history. Their execution status should be queried through runs rather than
embedded back into definition `status` fields.
