# Web UI Job Board Integration

This document summarizes the local agent API surface that the web UI should use
to render and update the job board. The UI may present the data as a Kanban
board, but the shared API vocabulary is job, task, chore, stage, runtime,
and phase.


## Core Concepts

A job is either a task or a chore.

Task and chore Markdown files store stable authored definitions. Stage is
folder placement:

- `tasks/` and `chores/` are ready folders
- `drafts/tasks/` and `drafts/chores/` are draft folders
- `archive/tasks/` and `archive/chores/` are archived folders

Runtime status is stored separately in `.runtime/jobs.db`. Thread and run
history is stored in `.runtime/runs.db`. Runtime fields should not be edited by
the UI as Markdown frontmatter.

Phase is a UI projection derived from stage, job status, and runtime data.
It is not stored by Toolang and is not returned as a persisted field.


## Recommended Read Flow

Use the unified jobs endpoint for ready board data:

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
  "id": "3nprht9x",
  "kind": "task",
  "stage": "ready",
  "status": "todo",
  "title": "Review API changes",
  "path": "tasks/3nprht9x.md",
  "updated_at": "2026-04-23T10:10:00Z",
  "runtime": {
    "thread_id": "tsk_3nprht9x",
    "last_run": null,
    "next_run": null
  }
}
```

Chore list item:

```json
{
  "id": "xy1234ab",
  "kind": "chore",
  "stage": "ready",
  "status": "todo",
  "schedule": "FREQ=HOURLY;INTERVAL=6",
  "title": "Check stale PRs",
  "path": "chores/xy1234ab.md",
  "updated_at": "2026-04-23T10:10:00Z",
  "runtime": {
    "thread_id": "chr_xy1234ab",
    "last_run": {
      "id": "run_ab12cd34",
      "status": "finished",
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

`stage` values:

| Value | Meaning |
| --- | --- |
| `ready` | Visible to the runtime |
| `draft` | Authored but not ready |
| `archived` | Retired and hidden from default lists |

Task `status` values:

| Value | Meaning |
| --- | --- |
| `todo` | Ready to be claimed |
| `running` | Claimed or currently being processed |
| `done` | Completed successfully |
| `failed` | Failed and not retried automatically |
| `canceled` | Intentionally canceled |

Chore `status` values:

| Value | Meaning |
| --- | --- |
| `todo` | Waiting for the next due or manual run |
| `running` | Claimed or currently being processed |
| `done` | No future scheduled occurrences remain |

Runtime run `status` values:

| Value | Meaning |
| --- | --- |
| `running` | Run is currently active |
| `finished` | Run finished successfully |
| `failed` | Run failed |
| `canceled` | Run was canceled |


## Phase Projection

The UI should derive a board phase locally. Do not write phase back to the API.

Recommended derivation:

```ts
function jobPhase(job: Job): JobPhase {
  if (job.stage === "archived") return "archived";
  if (job.stage === "draft") return "draft";
  if (job.runtime.last_run?.status === "running") return "in_progress";

  if (job.kind === "task" && job.status === "failed") return "failed";
  if (job.kind === "task" && job.status === "canceled") return "canceled";
  if (job.kind === "task" && job.status === "todo") return "todo";
  if (job.kind === "task" && job.status === "done") return "finished";

  if (job.kind === "chore" && job.status === "todo" && job.runtime.next_run) return "scheduled";
  if (job.kind === "chore" && job.status === "done") return "finished";

  return "ready";
}
```

Suggested columns:

| Phase | Typical contents |
| --- | --- |
| `todo` | Ready tasks |
| `scheduled` | Ready chores with a future `next_run` |
| `ready` | Ready jobs without a more specific phase |
| `in_progress` | Jobs whose latest run is running |
| `failed` | Failed tasks |
| `canceled` | Canceled tasks |
| `finished` | Done tasks or chores with no future schedule |
| `draft` | Draft jobs when shown |
| `archived` | Archived jobs when shown |


## Create And Update

Create a task:

```http
POST /api/v1/tasks
Content-Type: application/json
```

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks."
}
```

Patch a task definition:

```http
PATCH /api/v1/tasks/{task_id}
Content-Type: application/json
```

```json
{
  "title": "Review updated API changes",
  "body": "Focus on web UI integration risks."
}
```

Move a task through stage folders:

```http
POST /api/v1/tasks/{task_id}/draft
POST /api/v1/tasks/{task_id}/ready
POST /api/v1/tasks/{task_id}/archive
```

Reopen a finished, failed, or canceled task:

```http
POST /api/v1/tasks/{task_id}/reopen
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
  "schedule": "FREQ=HOURLY;INTERVAL=6"
}
```

Patch a chore definition:

```http
PATCH /api/v1/chores/{chore_id}
Content-Type: application/json
```

```json
{
  "schedule": "FREQ=DAILY",
  "body": "Check stale pull requests once per day."
}
```

Move a chore through stage folders:

```http
POST /api/v1/chores/{chore_id}/draft
POST /api/v1/chores/{chore_id}/ready
POST /api/v1/chores/{chore_id}/archive
```

Create one manual chore occurrence without changing the schedule:

```http
POST /api/v1/chores/{chore_id}/run
```


## Archive And Delete

Archive instead of deleting for normal UI retirement:

```http
POST /api/v1/tasks/{task_id}/archive
POST /api/v1/chores/{chore_id}/archive
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

Use `runtime.last_run.id` to open a trace:

```http
GET /api/v1/runs/{run_id}
```

If `runtime.last_run.status` is `running`, it is the current active run. The UI
may expose steer and cancel controls for that run:

```http
POST /api/v1/runs/{run_id}/steer
POST /api/v1/runs/{run_id}/cancel
```

Do not expose rewind or fork for task and chore threads. Their thread ids are
derived from job ids, so those thread operations are limited to branchable chat
threads.


## Refresh Strategy

After create, patch, stage action, execution action, or delete, the
simplest correct behavior is to refetch `GET /api/v1/jobs`.

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
| `409` | Runtime action is not valid for the current status |
| `422` | Request body shape failed API validation |

Job ids are server-generated. The web UI should not ask users to type ids.
