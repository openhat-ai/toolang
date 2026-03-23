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
- Taskwarrior
- Linear
- GitHub issues

All of these should enter the runtime as the same primitive:

- `origin = task`
- `thread_id = task:<provider-specific-ref>`

The runtime only needs to know that one turn is task-driven.


## 2. Keep Runtime Semantics Small

The runtime should provide only a small amount of task-specific structure:

- provider
- task ref
- title
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
    "title": "...",
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


## 7. External Task Providers

External task systems should still map to the same task primitive.

Examples:

- Linear issue
- GitHub issue
- Taskwarrior task

The runtime should not split them into different execution paths.

Instead:

- external events become `origin = task`
- the built-in task prompt decides what to do
- configured services perform the provider-specific read or write operations


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
- runtime API and channel ingress needed to surface task turns

It should not implement:

- provider-specific task strategies
- large workflow state machines
- separate execution logic per task provider


## 10. Immediate Implementation Direction

The next useful implementation steps are:

1. inject explicit task context into `origin = task` turns
2. add a built-in task prompt used automatically for task-driven turns
3. expose the minimum service-availability signals needed by that prompt
4. keep local task-file updates as direct markdown mutations with stable short
   task ids
5. later connect external providers through configured services, not new task
   runtime abstractions
