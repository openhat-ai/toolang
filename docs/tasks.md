# Job Model

Toolang uses Markdown job documents for durable authored work.

Current built-in job kinds are:

- `task`
- `chore`

## Directory Structure

Jobs live under one agent home:

```text
tasks/
  <id>.md
chores/
  <id>.md
drafts/
  tasks/
    <id>.md
  chores/
    <id>.md
archive/
  tasks/
    <id>.md
  chores/
    <id>.md
.runtime/
  ids.json
  jobs.db
  runs.db
```

Rules:

- `tasks/` contains ready one-shot work.
- `chores/` contains ready recurring work.
- `drafts/tasks/` and `drafts/chores/` contain authored definitions that are not ready.
- `archive/tasks/` and `archive/chores/` contain retired definitions.
- `.runtime/ids.json` owns local id allocation state.
- `.runtime/jobs.db` owns scheduler projection and atomic job claims.
- `.runtime/runs.db` owns thread, run, step, input, and transcript truth.

Only `tasks/` and `chores/` are scanned for execution. Draft and archived
folders are cold folders; the CLI updates them directly, and listing may scan
them lazily.


## Definition And Stage

Markdown files store stable job definitions only.

Shared frontmatter fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | required before publication | Globally unique stable local job id |
| `name` | required for writes | Authored logical name, projected from the file name when omitted |
| `title` | optional | Human-readable display title |

The CLI, API, and agent tools allocate `id` before passing a new job to
`AuthoredJobs`. The catalog itself does not allocate ids. Job ids are unique
across tasks, chores, and every stage. Moving a job between stage directories
does not change `id`, `name`, or the Markdown file name. Editing `name` does not
rename the file.

New jobs created by the CLI use the generated id as the file name:

```text
tasks/3nprht9x.md
chores/xy1234ab.md
```

When a manually added Markdown file omits `name`, `JobFile` projects the file
name as `meta["name"]`. When it omits `id`, `toolang.work` allocates an id and
rewrites the frontmatter before publishing the next immutable home-job state.
If a direct edit introduces a duplicate id, state publication fails and reports
the last-modified conflicting file. Subsequent operations use `id` as the
selector; `title` is only display text and may be changed freely.

Stage is expressed by folder placement, not by frontmatter:

| Folder | Stage | Meaning |
| --- | --- | --- |
| `drafts/tasks/` | `draft` | Task definition exists but is not ready |
| `drafts/chores/` | `draft` | Chore definition exists but is not ready |
| `tasks/` | `ready` | Task is visible to the runtime |
| `chores/` | `ready` | Chore is visible to the runtime |
| `archive/tasks/` | `archived` | Task is retired |
| `archive/chores/` | `archived` | Chore is retired |

Runtime fields such as status, last run, next run, counters, and errors are not
written to Markdown.

The Markdown body is a `ContentBody` using
[input-syntax.md](./input-syntax.md). It has no ambient template variables;
includes resolve relative to the job document, and invoked prompt templates
receive only their own explicit arguments and input. Input perceiving produces
the `Percept` passed to `RunSpec.input`; the selected runnable still sees its
language-level primary type such as the default `Part[]`.


## Tasks

Tasks are one-shot work items.

A minimal task document:

```md
---
id: 3nprht9x
name: 3nprht9x
title: Review API changes
---

Review the API changes and summarize risks.
```

Task runtime status is stored in `.runtime/jobs.db`:

| Status | Meaning |
| --- | --- |
| `todo` | Ready for a run |
| `running` | Currently claimed by one run |
| `done` | Latest run completed and the task is finished |
| `failed` | Latest run failed |
| `canceled` | Latest run was intentionally canceled |

Task execution rules:

- only ready tasks with scheduler status `todo` can be claimed
- claim atomically sets job status to `running` and records `last_run_id`
- run `finished` sets job status to `done`
- run `failed` sets job status to `failed`
- run `canceled` sets job status to `canceled`
- `task reopen <id>` sets `done`, `failed`, or `canceled` tasks back to `todo`

The task body is authored input. Runtime output is stored in execution records
and projected through the job thread.


## Chores

Chores are recurring local jobs.

A minimal chore document:

```md
---
id: xy1234ab
name: xy1234ab
title: Check stale PRs
schedule: "FREQ=HOURLY;INTERVAL=6"
---

Check stale PRs and report actionable items.
```

Chore-specific frontmatter fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `schedule` | required | RRULE schedule string |

Chore runtime status is stored in `.runtime/jobs.db`:

| Status | Meaning |
| --- | --- |
| `todo` | Waiting for the next scheduled or manual run |
| `running` | Currently claimed by one run |
| `done` | No future scheduled occurrences remain |

Chore execution rules:

- only ready chores with scheduler status `todo` can be claimed
- scheduled claims require the RRULE to be due
- `chore run <id>` creates one manual occurrence without changing `schedule`
- any final run status updates `next_run_at`
- if another occurrence exists, the chore returns to `todo`
- if no future occurrence exists, the chore becomes `done`
- failed or canceled chore runs do not make the chore itself failed or canceled

The chore body is authored recurring work input. It is not rewritten by run
output.


## Threads And Runs

Job thread ids are runtime projections derived from job kind and id:

```text
task_<id>
chore_<id>
```

Examples:

```text
task_3nprht9x
chore_xy1234ab
```

Run ids use the run id family with a `run_` prefix:

```text
run_<id>
```

Example:

```text
run_ppkp9e94
```

Job documents store only `id`. They do not store `thread_id`.

Each execution creates one run under the job thread. `runs.db` keeps the
history; future task retries, task reopens, and repeated chore occurrences all
reuse the same job thread.


## Runtime Projection

The jobs API returns authored fields at the top level and runtime-derived state
under `runtime`.

Task projection:

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
    "thread_id": "task_3nprht9x",
    "last_run": null,
    "next_run_at": null
  }
}
```

Chore projection:

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
    "thread_id": "chore_xy1234ab",
    "last_run": {
      "id": "run_ab12cd34",
      "status": "finished",
      "started_at": "2026-04-23T06:00:00Z",
      "finished_at": "2026-04-23T06:02:00Z"
    },
    "next_run_at": "2026-04-23T12:00:00Z"
  }
}
```

`runtime.last_run` is the latest known run for the job thread. If its status is
`running`, it is also the active run. `runtime.next_run_at` is either `null` or
the next scheduled chore timestamp.

Runtime run statuses are:

| Status | Meaning |
| --- | --- |
| `running` | The run is in progress |
| `finished` | The run finished successfully |
| `failed` | The run finished with an error |
| `canceled` | The run was canceled |


## Scheduler Projection

`jobs.db` is an execution aid, not a second source of authored job definition.
It stores one row per ready Markdown job:

- `job_id`
- `kind`
- `path`
- `definition_hash`
- `thread_id`
- `status`
- `last_run_id`
- `next_run_at`
- `run_count`
- `failed_count`
- `canceled_count`
- `created_at`
- `updated_at`

The scheduler reconciles `jobs.db` against ready folders before claiming jobs:

- ready files missing in `jobs.db` are inserted
- ready files whose path or definition hash changed are updated
- rows whose ready files disappeared are removed
- cold folders are not inserted into `jobs.db`

Run creation uses an atomic claim:

```text
claim ready job where status = todo
set job.status = running
create run with status = running
set job.last_run_id = run_id
```

If the claim fails, another scheduler or manual operation already claimed the
job.

Run completion is also atomic:

```text
set run.status = final_status
set run.finished_at = now
update job.status from kind and final_status
update counters
update next_run_at for chores
```


## HTTP API

Current read endpoints:

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/archived`
- `GET /api/v1/jobs/archived/{job_id}`
- `GET /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/archived`
- `GET /api/v1/tasks/archived/{task_id}`
- `GET /api/v1/chores`
- `GET /api/v1/chores/{chore_id}`
- `GET /api/v1/chores/archived`
- `GET /api/v1/chores/archived/{chore_id}`

Default list endpoints return ready jobs. Archived jobs are returned only by
explicit `/archived` routes.

Current write endpoints:

- `POST /api/v1/tasks`
- `PATCH /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/draft`
- `POST /api/v1/tasks/{task_id}/ready`
- `POST /api/v1/tasks/{task_id}/archive`
- `POST /api/v1/tasks/{task_id}/reopen`
- `POST /api/v1/tasks/{task_id}/cancel`
- `PATCH /api/v1/tasks/archived/{task_id}`
- `DELETE /api/v1/tasks/archived/{task_id}`
- `POST /api/v1/chores`
- `PATCH /api/v1/chores/{chore_id}`
- `POST /api/v1/chores/{chore_id}/draft`
- `POST /api/v1/chores/{chore_id}/ready`
- `POST /api/v1/chores/{chore_id}/archive`
- `POST /api/v1/chores/{chore_id}/run`
- `POST /api/v1/chores/{chore_id}/cancel`
- `PATCH /api/v1/chores/archived/{chore_id}`
- `DELETE /api/v1/chores/archived/{chore_id}`

Create and patch requests use structured JSON fields instead of raw
frontmatter. Task writes accept `title` and `body`. Chore writes accept
`title`, `body`, and `schedule`.

The unified `/jobs` endpoints are read-only. Writes use the concrete `/tasks`
or `/chores` collection so job-kind semantics remain explicit.

Stage endpoints move definition files between folders. Execution endpoints
operate on scheduler status and runs; they do not rewrite Markdown definitions.
