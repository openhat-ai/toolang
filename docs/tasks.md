# Toolang Task Model

This document defines how Toolang should treat `task` as a collaboration
primitive.

Execution primitives live in [execution.md](./execution.md).
Collaboration rules live in [collaboration.md](./collaboration.md).
Control-surface endpoints live in [api.md](./api.md).


## 1. Core Principle

Toolang should not implement a different execution model for each task system.

Examples:

- local markdown task files
- Linear
- GitHub issues
- Taskwarrior

The runtime should execute only one kind of task object:

- a local task file under `${AGENT_ROOM}/tasks/`

Remote task systems should be mirrored into local task files first. Runtime
turns still use the same task primitive:

- `origin = task`
- `thread_id = task:local:<task_id>`


## 2. Keep Runtime Semantics Small

The runtime should provide only a small amount of task-specific structure:

- provider
- task ref
- task name
- body
- status
- requester
- thread id
- available task services

The runtime should not hardcode task-specific workflows for each provider.


## 3. Built-In Task Prompt

Task execution behavior should be defined by one built-in prompt rather than a
large amount of provider-specific orchestration code.

This built-in prompt should tell the agent:

- understand the current task before acting
- perform the requested work
- update the task at important milestones
- write the result back to the task before finishing
- explicitly ask for missing configuration if required task services are not
  available

Rules:

- if task read access is missing, the task should not proceed
- if task write access is missing, the agent may still work but must clearly
  report that it could not update the task
- the agent must not pretend a task update happened if the required service is
  unavailable


## 4. Task Context Injected Into One Turn

When `origin = task`, runtime should inject structured task context into the
turn.

Suggested shape:

```json
{
  "task": {
    "provider": "local|taskwarrior|linear|github",
    "ref": "task:...",
    "name": "...",
    "body": "...",
    "status": "...",
    "requester": "...",
    "thread_id": "task:..."
  },
  "task_services": {
    "read": true,
    "write": true,
    "comment": true
  }
}
```

This context is sufficient for the built-in task prompt.


## 5. Task Services

Toolang should treat task-system integration as a service capability, not as a
new runtime abstraction family.

The agent should rely on configured services for task operations such as:

- read task
- update task fields
- append task notes or comments

Provider-specific details should stay behind those configured services.

The built-in prompt should reason in terms of:

- read
- write
- comment

not in terms of provider-specific APIs.


## 6. Local Task Files

Local task files are the simplest task provider and should remain first-class.
They are still just `task` with `provider = local`.

### 6.1 Shape

Local task files live under:

- `${AGENT_ROOM}/tasks/*.md`

Local task files should stay human-writable and minimal.

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
  - examples:
    - `owner`
    - `self`
    - `peer:<agent>`
    - `service:<provider>`
- `status`
  - defaults to `todo`
- `paused`
  - optional execution control flag

The runtime should derive:

- `thread_id = task:local:<id>`
- title-like display from the filename rather than a separate front-matter
  field

### 6.2 Update Style

For local task files, the agent should update the markdown document directly.

Recommended body structure:

- `## Task`
- `## Notes`
- `## Progress`
- `## Outcome`

This is a convention, not a second file format.

The minimal expected behavior is:

- update `status` in front matter
- append progress notes while work is ongoing
- write final result or summary before marking the task finished

The runtime should not use `.doing` or `.done` directories to represent normal
status changes. Status belongs in front matter so file paths remain stable.


## 7. Remote Task Mirrors

Remote task systems should not enter runtime directly through `poll` as
task-origin deliveries.

Instead, one `chore` should periodically synchronize remote tasks that belong
to the current agent into local task files.

Examples:

- Linear issue assigned to the current account
- GitHub issue assigned to the configured user
- Taskwarrior task tagged for the current agent

Suggested flow:

1. one `chore` queries the remote provider for tasks that belong to the current
   agent
2. each discovered remote task is matched against local mirror state
3. Toolang creates or updates one local task file under `${AGENT_ROOM}/tasks/`
4. `pulse` sees that local task file and schedules a normal `origin = task`
   turn
5. a later chore or service call pushes local progress back to the remote
   provider

This keeps runtime execution simple:

- runtime executes local task files only
- remote provider differences stay in chore logic and services

### 7.1 Identifying Tasks That Belong To The Current Agent

Determining "tasks assigned to me" should be provider logic, not runtime core
logic.

Examples:

- Linear provider resolves the current user from its token and lists issues
  assigned to that user
- GitHub provider resolves the current user and lists assigned issues or pull
  requests
- Taskwarrior provider uses its own filters

Toolang core should not maintain a universal "who am I" mapping for task
providers.

### 7.2 Mirror State

Mirror bookkeeping should not bloat local task front matter. Human-written
task files should stay small.

Instead, each agent room should persist:

- `${AGENT_ROOM}/task_mirrors.json`

Each mirror entry should record:

- `provider`
- `remote_ref`
- `local_task_id`
- `path`
- `remote_updated_at`
- `last_synced_at`

Rules:

- local task ids stay short and stable
- remote mirrors get a local task id the first time they are imported
- future syncs use `provider + remote_ref -> local_task_id`
- local task identity does not depend on path or remote title

### 7.3 Local Task Files For Remote Mirrors

Remote mirror tasks still use the same local markdown shape:

- front matter:
  - `id`
  - `requester`
  - `status`
  - `paused`
- filename:
  - human-readable task name
- body:
  - current working copy of the task input

The expected default requester for imported remote tasks is:

- `requester = service:<provider>`


## 8. Agent Behavior When Configuration Is Missing

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


## 9. What Should Be Implemented In Code

Toolang code should stay small.

It should implement:

- task context loading
- service availability checks
- local task file read and write helpers
- local task id generation and persistence
- task mirror state for remote-to-local synchronization
- runtime API needed to surface task turns

It should not implement:

- provider-specific task strategies
- large workflow state machines
- separate execution logic per task provider
- direct remote-provider task execution paths inside runtime


## 10. Immediate Implementation Direction

The next useful implementation steps are:

1. keep local task files minimal and stable
2. persist `task_mirrors.json` in the agent room
3. add chore-driven remote task synchronization
4. use the built-in task prompt for both human-authored and mirrored local
   tasks
5. later connect provider-specific sync and update services without changing
   runtime task semantics
