# Toolang Collaboration Model

This document defines how one long-lived agent runtime accepts work and
coordinates with humans or other agents.

Execution primitives live in [execution.md](./execution.md).
Task and scheduled-definition rules live in [tasks.md](./tasks.md).
Control surfaces live in [api.md](./api.md).


## 1. Core Principle

Toolang should not invent a separate runtime model for:

- human chat
- agent-to-agent communication
- task-driven work
- chore-driven work
- will-driven work

Instead, all of these should enter the same runtime through one normalized
queue of run submissions.


## 2. Keep Lifecycle And Runtime Terms Separate

`activation` means one online interval of the agent.

Examples:

- one foreground `toolang run`
- one background `toolang start`
- one one-shot `toolang invoke`

`run` means one concrete handling attempt inside one thread during an
activation.

The runtime queue should therefore not contain activation requests.
It should contain run submissions.


## 3. Runtime Shape

One long-lived runtime should contain:

- one current activation
- one inbox of run submissions
- one scheduler
- one execution store
- zero or more runtime loops

Runtime loops do not execute work directly.

They only create run submissions and place them into the inbox.
The scheduler then decides when each admitted run may start.


## 4. Sources Of Run Submissions

The built-in sources are:

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

- all of these become run submissions
- all of them are persisted under the same execution truth model
- all of them are scheduled by the same scheduler


## 5. Threads Remain The Durable Unit

Run submissions should always target a durable `thread_id`.

Suggested mapping:

- direct human or peer chat
  - one transport or caller-selected chat thread
- task work
  - `task:local:<task_id>`
- chore work
  - `chore:<chore_id>`
- will work
  - `will:<agent_id>`

Rules:

- one thread may span many runs
- one thread may span many activations
- at most one running run may exist per thread at a time


## 6. Scheduler Policy

The first useful scheduler policy is:

- serialize by `thread_id`
- allow different threads to run concurrently
- apply group-level budgets by run origin

Suggested built-in groups:

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

This keeps the runtime responsive to humans while still allowing background
work.


## 7. Minimal Collaboration Primitives

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


## 8. Chores Are Not A Collaboration Primitive

`chore` is a local runtime trigger.

It may:

- create or update tasks
- send chat messages
- enqueue more local work

But it should not become a third collaboration primitive.

If a chore needs to involve another actor, it should do so by emitting:

- a chat
- or a task mutation


## 9. Agent-To-Agent Communication

Agent-to-agent communication should not use a special execution hierarchy.

It should reuse the same two primitives:

- direct chat
- task handoff

### 9.1 Direct Chat

One agent sends another agent a message.

For the receiving agent, this becomes a normal inbound chat run:

- `origin = chat`
- `sender = peer`
- one stable `thread_id`

### 9.2 Task Handoff

One agent creates, updates, or assigns a task that another agent should own.

For the receiving agent, this becomes a normal inbound task run:

- `origin = task`
- `thread_id = task:local:<task_id>`

This is the preferred path for durable multi-agent work.


## 10. Tasks As Indirect Communication

A project-management task can act as a shared durable thread for:

- human-to-agent coordination
- agent-to-agent coordination
- chore-driven updates
- long-running multi-step delegation

A useful mental model is:

- task fields
  - ownership and work state
- task notes or comments
  - conversational coordination

This lets agents communicate indirectly through shared task state without
needing a separate collaboration subsystem.


## 11. Recommended Outbound Effects

To keep the runtime simple, one completed run should emit at most these
high-level external effects:

- `OutboundChat`
- `TaskMutation`

Examples:

- send a peer one short message
- create a new task
- reassign a task
- add a task note or comment
- mark a task done

This keeps effect handling understandable and avoids a large taxonomy of
special message types.
