# Toolang Collaboration Model

This document defines how one long-lived agent runtime should accept work and
coordinate with humans or other agents.

Execution primitives live in [execution.md](./execution.md).
Control surfaces live in [api.md](./api.md).


## 1. Core Principle

Toolang should not invent a separate execution model for:

- human chat
- agent-to-agent communication
- task-driven work
- chore-driven work

Instead, all of these should enter the same runtime through one normalized
queue of turn requests.


## 2. Keep `run` Narrow

`run` already means one continuous active interval of one agent process.

Examples:

- one foreground `toolang run`
- one background `toolang start`
- one one-shot `toolang invoke`

The runtime queue should therefore not contain `run requests`.

The queue should contain:

- `turn requests`
- or `work items`

This keeps the vocabulary stable:

- `run`
  - process lifetime
- `turn request`
  - one schedulable work item inside that lifetime


## 3. Runtime Shape

One long-lived agent runtime should contain:

- one `run`
- one `inbox`
- one `scheduler`
- one execution store
- zero or more runtime loops

Runtime loops do not execute work directly.

They only create turn requests and place them into the inbox.

The scheduler then decides when each request may start.


## 4. Sources Of Turn Requests

The initial built-in sources are:

- `chat`
  - human or peer messages
- `task`
  - assigned or updated work items
- `chore`
  - local recurring or policy-driven work
- `invoke`
  - one-shot direct requests
- `will`
  - low-priority self-driven work

Rules:

- all of these become turn requests
- all of them are persisted under the same execution truth model
- all of them are scheduled by the same scheduler


## 5. Threads Remain The Durable Unit

Turn requests should always target a durable `thread_id`.

Suggested mapping:

- direct human or peer chat
  - one chat thread id
- task work
  - `task:<task_ref>`
- chore work
  - `chore:<chore_key>`
- will work
  - `will:<agent_id>`

Rules:

- one thread may span many turns
- one thread may span many runs
- at most one running turn may exist per thread at a time


## 6. Scheduler Policy

The first useful scheduler policy is:

- serialize by `thread_id`
- allow different threads to run concurrently
- apply group-level budgets by `thread_group`

Suggested built-in groups:

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

This keeps the runtime responsive to humans while still allowing background
work.


## 7. Minimal Communication Primitives

Agent collaboration should keep only two outward-facing primitives:

- `chat`
- `task`

### 7.1 Chat

Use chat for:

- short coordination
- clarification
- quick requests
- conversational status exchange

### 7.2 Task

Use tasks for:

- durable delegation
- tracked ownership
- multi-step work
- project-management-driven coordination

Rules:

- if work needs explicit ownership and lifecycle, use a task
- if work needs quick back-and-forth, use chat


## 8. Chores Are Not A Communication Primitive

`chore` is a local runtime trigger.

It may:

- create or update tasks
- send chat messages
- enqueue more local work

But it should not become a third communication primitive.

If a chore needs to involve another actor, it should do so by emitting:

- a chat
- or a task mutation


## 9. Agent-To-Agent Communication

Agent-to-agent communication should not use a special protocol.

It should reuse the same two primitives:

- direct chat
- task handoff

### 9.1 Direct Chat

One agent sends another agent a message.

This becomes a normal inbound chat turn for the receiving agent:

- `origin = chat`
- `sender = peer`
- one stable `thread_id`

### 9.2 Task Handoff

One agent creates, updates, or assigns a task that another agent should own.

For the receiving agent, this becomes a normal inbound task turn:

- `origin = task`
- `thread_id = task:<task_ref>`

This is the preferred path for durable multi-agent work.


## 10. Tasks As Indirect Communication

A project-management task can act as a shared durable thread.

That makes it useful for:

- human-to-agent coordination
- agent-to-agent coordination
- chore-driven updates
- long-running multi-step delegation

A useful mental model is:

- task fields
  - ownership and work state
- task comments
  - conversational coordination

This lets agents communicate indirectly through shared task state without
needing a separate collaboration subsystem.


## 11. Recommended Outbound Effects

To keep the runtime simple, one completed turn should emit at most these
high-level external effects:

- `OutboundChat`
- `TaskMutation`

Examples:

- send a peer one short message
- create a new task
- reassign a task
- add a task comment
- mark a task done

This keeps effect handling understandable and avoids a large taxonomy of
special message types.


## 12. Human Mental Model

A human operator should only need to understand:

- the agent is always running inside one run
- runtime loops place work into one inbox
- the scheduler chooses when each thread may run
- work arrives as chat, task, chore, or invoke
- collaboration happens through chat or task

Everything else remains an implementation detail.
