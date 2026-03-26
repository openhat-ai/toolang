# Toolang Work Definitions

This document defines how Toolang treats tasks, chores, and will.

Execution primitives live in [execution.md](./execution.md).
Collaboration rules live in [collaboration.md](./collaboration.md).
Control-surface endpoints live in [api.md](./api.md).


## 1. Core Principle

Toolang should not implement a different execution model for each external work
system.

Examples:

- local markdown task files
- Linear
- GitHub issues
- Taskwarrior

The runtime should execute one stable set of local definitions:

- local task files under `${AGENT_ROOM}/tasks/`
- local chore files under `${AGENT_ROOM}/chores/`
- one local will file at `${AGENT_ROOM}/will.md`

Remote work systems should be mirrored into these local definitions first.

Rules:

- definitions are not runs
- definition endpoints return authored state only
- execution history for any definition is queried through runs


## 2. Tasks

Tasks are the collaboration-oriented work definition.

Tasks should remain the one built-in primitive for:

- durable delegation
- tracked ownership
- long-running work
- task-system mirroring

### 2.1 Runtime Mapping

When `origin = task`, the runtime creates a run in the task's stable thread.

Suggested task-thread mapping:

- `thread_id = task:local:<task_id>`

Task runs still use the same general runtime objects:

- activation
- thread
- run
- step

### 2.2 Local Task Files

Local task files live under:

- `${AGENT_ROOM}/tasks/*.md`

Suggested front matter:

- `id`
- `requester`
- `status`
- `paused`

Rules:

- `filename`
  - the human-facing task name
- `body`
  - the full task input
- `id`
  - a short stable task identifier
  - auto-generated if missing
- `requester`
  - who created or requested the task
- `status`
  - task definition status
  - examples:
    - `todo`
    - `doing`
    - `done`
    - `cancelled`
- `paused`
  - optional execution-control flag

The runtime derives:

- `thread_id = task:local:<id>`
- display title from the filename rather than a second authored title field

### 2.3 Task API Boundary

`/api/v1/tasks` returns definition data such as:

- `id`
- `name`
- `body`
- `status`
- `requester`
- `thread_id`
- `paused`

Rules:

- `TaskItem.status` is task definition status, not run status
- task execution history should be queried through `/api/v1/runs?origin=task`
- current or latest activity should not be inferred from the task definition
  status alone

### 2.4 Task Services

Toolang should treat task-system integration as a service capability, not as a
new runtime abstraction family.

The agent should rely on configured services for task operations such as:

- read task
- update task fields
- append task notes or comments

Provider-specific details should stay behind those configured services.


## 3. Chores

Chores are local recurring definitions.

Chores are useful for:

- periodic sync
- maintenance work
- recurring checks
- background coordination tasks

### 3.1 Authored Shape

Chore files live under:

- `${AGENT_ROOM}/chores/*.md`

Authored fields:

- `title`
- `body`
- `rrule`
- `paused`

Current API summary shape:

- `id`
- `title`
- `rrule`
- `paused`

### 3.2 Runtime Mapping

Chores map to stable derived thread identities:

- `thread_id = chore:<chore_id>`

Rules:

- chore definitions do not expose runtime status
- chore execution history should be queried through `/api/v1/runs?origin=chore`
- chore scheduling is RRULE-driven
- new or updated chores are enqueued once immediately, then follow `rrule`


## 4. Will

Will is the agent-local recurring long-horizon definition.

It is useful for:

- periodic self-review
- long-horizon goals
- ongoing project alignment

### 4.1 Authored Shape

Will lives at:

- `${AGENT_ROOM}/will.md`

Authored fields:

- `title`
- `body`
- `rrule`
- `paused`

Current API summary shape:

- `id`
- `title`
- `rrule`
- `paused`

`id` is the stable public identifier for the one local will definition.

### 4.2 Runtime Mapping

Will maps to one stable derived thread:

- `thread_id = will:<agent_id>`

Rules:

- will scheduling is RRULE-driven
- will execution history should be queried through `/api/v1/runs?origin=will`
- create or update may trigger one immediate run before later scheduled runs


## 5. RRULE Scheduling

Scheduled definitions use RRULE, not `interval_sec`.

Rules:

- chore and will definitions persist `rrule`
- API write surfaces accept `rrule`
- API read surfaces expose `rrule`
- legacy interval-based documents may be migrated to equivalent RRULE values
  during load

This keeps scheduling expressive while preserving one stable authored shape.


## 6. Remote Task Mirrors

Remote task systems should not enter runtime directly as custom work objects.

Instead, one chore should periodically synchronize remote tasks that belong to
the current agent into local task files.

Suggested flow:

1. one chore queries the remote provider for tasks that belong to the current
   agent
2. each discovered remote task is matched against local mirror state
3. Toolang creates or updates one local task file under `${AGENT_ROOM}/tasks/`
4. pulse sees that local task file and schedules a normal `origin = task` run
5. a later chore or service call pushes local progress back to the remote
   provider

This keeps runtime execution simple:

- runtime executes local definitions only
- remote provider differences stay in chores and services


## 7. Agent Behavior When Configuration Is Missing

If required task services are missing, the agent should not silently degrade.

Expected behavior:

- clearly state what is missing
- request the needed service configuration
- continue only when the remaining work is still meaningful without that
  integration

Examples:

- no read service
  - do not attempt task execution
- read available, write missing
  - task execution may proceed
  - final response must state that the task could not be updated


## 8. Design Boundary Summary

- tasks, chores, and will are authored definitions
- tasks are the collaboration primitive
- chores and will are RRULE-driven local triggers
- runs are the execution history for all of them
- threads are the durable bridge between definitions and runtime history
