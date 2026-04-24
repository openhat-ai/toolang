# Web UI Job Board Integration

This document summarizes the local agent API surface that the web UI should use
to render and update the job board. The UI may present the data as a Kanban
board, but the shared API vocabulary is job, task, chore, runtime, and phase.


## Core Concepts

A job is either a task or a chore.

Tasks are one-shot jobs. They have authored `state` and task-specific `stage`.

Chores are recurring jobs. They have authored `state` and `schedule`.

Runtime data is derived from execution records. It is returned under
`runtime` and should not be edited by the UI.

Phase is a UI projection derived from authored job fields and runtime data. It
is not stored by Toolang and is not returned as a persisted field.


## Recommended Read Flow

Use the unified jobs endpoint for board data:

```http
GET /api/v1/jobs
```

Use filters when useful:

```http
GET /api/v1/jobs?kind=task
GET /api/v1/jobs?kind=chore
GET /api/v1/jobs/archived
GET /api/v1/jobs/archived?kind=task
GET /api/v1/jobs/archived?kind=chore
```

List responses intentionally omit `body`. Fetch detail when opening an edit
drawer or detail view:

```http
GET /api/v1/tasks/{task_id}
GET /api/v1/chores/{chore_id}
```

For archived items, use the archived detail routes:

```http
GET /api/v1/jobs/archived/{job_id}
GET /api/v1/tasks/archived/{task_id}
GET /api/v1/chores/archived/{chore_id}
```


## List Item Shape

Task list item:

```json
{
  "id": "3nprht",
  "kind": "task",
  "state": "active",
  "stage": "todo",
  "title": "Review API changes",
  "path": "tasks/3nprht.md",
  "updated_at": "2026-04-23T10:10:00Z",
  "runtime": {
    "thread_id": "task_3nprht",
    "active_run": null,
    "last_run": null,
    "next_run": null
  }
}
```

Chore list item:

```json
{
  "id": "xy1234",
  "kind": "chore",
  "state": "active",
  "schedule": "FREQ=HOURLY;INTERVAL=6",
  "title": "Check stale PRs",
  "path": "chores/xy1234.md",
  "updated_at": "2026-04-23T10:10:00Z",
  "runtime": {
    "thread_id": "chore_xy1234",
    "active_run": null,
    "last_run": {
      "id": "run_ab12cd34",
      "status": "succeeded",
      "started_at": "2026-04-23T06:00:00Z",
      "finished_at": "2026-04-23T06:02:00Z"
    },
    "next_run": {
      "at": "2026-04-23T12:00:00Z"
    }
  }
}
```

Detail responses return the same item plus `body`.


## Field Values

`state` values:

| Value | Meaning |
| --- | --- |
| `active` | Participates in scheduling or claiming |
| `inactive` | Authored but skipped by scheduling |
| `archived` | Retired and hidden from default lists |

Task `stage` values:

| Value | Meaning |
| --- | --- |
| `todo` | Ready to be claimed |
| `running` | Claimed or currently being processed |
| `done` | Completed successfully |
| `failed` | Failed and will not retry automatically |

Runtime run `status` values:

| Value | Meaning |
| --- | --- |
| `running` | Run is currently active |
| `succeeded` | Run finished successfully |
| `failed` | Run failed |
| `canceled` | Run was canceled |


## Phase Projection

The UI should derive a board phase locally. Do not write phase back to the API.

Recommended derivation:

```ts
function jobPhase(job: Job): JobPhase {
  if (job.state === "archived") return "archived";
  if (job.runtime.active_run) return "in_progress";

  if (job.kind === "task" && job.stage === "running") return "in_progress";
  if (job.state === "inactive") return "inactive";
  if (job.kind === "task" && job.stage === "failed") return "failed";
  if (job.runtime.last_run?.status === "failed") return "failed";

  if (job.kind === "task" && job.stage === "todo") return "todo";
  if (job.kind === "task" && job.stage === "done") return "finished";

  if (job.kind === "chore" && job.runtime.next_run) return "scheduled";
  if (job.runtime.last_run?.status === "succeeded") return "finished";

  return "ready";
}
```

Suggested columns:

| Phase | Typical contents |
| --- | --- |
| `todo` | Active task with `stage: todo` |
| `scheduled` | Active chore with a future `next_run` |
| `ready` | Active job without a more specific phase |
| `in_progress` | Running run or task `stage: running` |
| `failed` | Failed task or last run |
| `finished` | Completed task or successful last chore run |
| `inactive` | `state: inactive` jobs |
| `archived` | `state: archived` jobs when shown |


## Create And Update

Create a task:

```http
POST /api/v1/tasks
Content-Type: application/json
```

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks.",
  "state": "active",
  "stage": "todo"
}
```

Patch a task:

```http
PATCH /api/v1/tasks/{task_id}
Content-Type: application/json
```

```json
{
  "title": "Review updated API changes",
  "body": "Focus on web UI integration risks.",
  "state": "inactive",
  "stage": "todo"
}
```

Task patch accepts any subset of `title`, `body`, `state`, and `stage`.

Archive a task by patching `state: "archived"` on the normal route:

```http
PATCH /api/v1/tasks/{task_id}
Content-Type: application/json
```

```json
{
  "state": "archived"
}
```

Unarchive a task by patching `state: "active"` or `state: "inactive"` on the
archived route:

```http
PATCH /api/v1/tasks/archived/{task_id}
Content-Type: application/json
```

```json
{
  "state": "active",
  "stage": "todo"
}
```

Create a chore:

```http
POST /api/v1/chores
Content-Type: application/json
```

```json
{
  "title": "Check stale PRs",
  "body": "Check stale pull requests and summarize blockers.",
  "state": "active",
  "schedule": "FREQ=HOURLY;INTERVAL=6"
}
```

Patch a chore:

```http
PATCH /api/v1/chores/{chore_id}
Content-Type: application/json
```

```json
{
  "state": "inactive",
  "schedule": "FREQ=DAILY"
}
```

Chore patch accepts any subset of `title`, `body`, `state`, and `schedule`.

Archive a chore by patching `state: "archived"` on the normal route:

```http
PATCH /api/v1/chores/{chore_id}
Content-Type: application/json
```

```json
{
  "state": "archived"
}
```

Unarchive a chore by patching `state: "active"` or `state: "inactive"` on the
archived route:

```http
PATCH /api/v1/chores/archived/{chore_id}
Content-Type: application/json
```

```json
{
  "state": "active"
}
```

Pause and resume active-directory jobs by patching `state` on the normal route:

```http
PATCH /api/v1/tasks/{task_id}
PATCH /api/v1/chores/{chore_id}
PATCH /api/v1/jobs/{job_id}
```

Use `state: "inactive"` to pause and `state: "active"` to resume.


## Archive And Delete

Archive instead of deleting for normal UI retirement. Archive uses `PATCH
state`, not a separate action endpoint:

```http
PATCH /api/v1/tasks/{task_id}
PATCH /api/v1/chores/{chore_id}
PATCH /api/v1/jobs/{job_id}
```

Delete is destructive and is available only for archived items:

```http
DELETE /api/v1/tasks/archived/{task_id}
DELETE /api/v1/chores/archived/{chore_id}
DELETE /api/v1/jobs/archived/{job_id}
```


## Linking To Thread And Trace Views

Use `runtime.thread_id` to open the job thread:

```http
GET /api/v1/threads/{thread_id}
```

Use `runtime.active_run.id` or `runtime.last_run.id` to open a trace:

```http
GET /api/v1/runs/{run_id}
```

The web UI can route these ids to existing thread and trace pages.


## Refresh Strategy

After create, patch, archive, or delete, the simplest correct behavior is to
refetch `GET /api/v1/jobs`.

For background changes, poll `GET /api/v1/jobs` on an interval. `GET
/api/v1/events` exposes recent update records such as `task_changed` and
`chore_changed`, but it currently has no cursor parameter. `GET
/api/v1/events/stream` is currently a heartbeat endpoint, not a full job-update
stream.

The UI may optimistically update local state after successful write responses,
because write responses return the updated item.


## Error Handling

Use normal HTTP status handling:

| Status | Meaning |
| --- | --- |
| `400` | Invalid job data, such as an invalid RRULE schedule |
| `404` | Task or chore id not found |
| `422` | Request body shape failed API validation |

Job ids are server-generated. The web UI should not ask users to type ids.
