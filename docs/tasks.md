# Authored Jobs

Toolang uses Markdown task and chore documents for durable authored jobs. The
runtime scheduling and recovery model is defined in [work.md](./work.md).

Current job kinds are:

- `task`: one-shot work activated by publication or a body revision;
- `chore`: recurring work activated by an RRULE or an explicit manual request.


## Layout And Stage

Jobs live below one agent home:

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

Stage is directory placement rather than frontmatter:

| Directory | Stage | Meaning |
| --- | --- | --- |
| `drafts/tasks/` | `draft` | Inactive task definition |
| `drafts/chores/` | `draft` | Inactive chore definition |
| `tasks/` | `ready` | Task visible to scheduling |
| `chores/` | `ready` | Chore visible to scheduling |
| `archive/tasks/` | `archived` | Retired task definition |
| `archive/chores/` | `archived` | Retired chore definition |

Only ready directories are watched at runtime. Draft and archived directories
are cold catalog storage and are read only by explicit catalog operations.


## Identity And Fields

Shared frontmatter fields are:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | before publication | Globally unique stable job identity |
| `title` | no | Human-readable display label |

Chores add:

| Field | Required | Meaning |
| --- | --- | --- |
| `schedule` | yes | RFC 5545 RRULE |

There is no separate job `name`. The id is the machine selector, title is the
optional label, and path is the current source location. New catalog-created
jobs use `<id>.md`; renaming that file does not change identity.

Job ids are unique across task and chore kinds and every stage. Both id and
kind are immutable. Moving between stages, renaming a source file, editing the
body, and changing a chore schedule preserve the id. Copying a job or changing
its kind requires a new id.

The CLI, API, and agent tools allocate an id before catalog creation. A
manually added ready file may omit it; `toolang.work` allocates and writes the
id under the authored-job lock before publishing the next ready snapshot.
Duplicate ids make the authored state invalid.

Runtime fields such as status, run ids, errors, and schedule cursors are never
written into authored Markdown.


## Body

The body is one `Submission` defined by [input-syntax.md](./input-syntax.md).
It has no ambient template variables. Includes resolve relative to the Markdown
file, and prompt templates receive only explicit arguments and input.

The scheduler retains the body as source and parses it as a `RunnableCall` only
when dispatching. The surface default is `task` or `chore`, falling back to
`default` when that runnable is absent. The evaluated content becomes the root
`RunSpec.input`.

A scheduler-side parse or validation failure is retained on the job record and
does not create a run.


## Task Document

```md
---
id: 3nprht9x
title: Review API changes
---

Review the API changes and summarize risks.
```

A new ready task is pending immediately. Editing its executable body creates a
new revision; title or path edits do not. Pending edits coalesce to the latest
body. An edit during a run does not mutate that run and requests the latest
revision afterward. An unchanged terminal task stays terminal until explicitly
reopened.

Task statuses are:

| Status | Meaning |
| --- | --- |
| `pending` | A revision is ready to dispatch |
| `running` | One captured revision has an active run |
| `done` | The current revision finished successfully |
| `failed` | The current revision failed |
| `canceled` | The current revision was canceled |


## Chore Document

```md
---
id: xy1234ab
title: Check stale PRs
schedule: "FREQ=HOURLY;INTERVAL=6"
---

Check stale PRs and report actionable items.
```

The scheduler persists a stable anchor and the earliest RRULE occurrence not
yet claimed. Body edits affect later runs without triggering an immediate run.
Schedule edits establish a new cursor. Missed scheduled occurrences coalesce
to the latest due occurrence, so downtime and long runs do not create an
unbounded backlog or shift the recurrence.

`chore run <id>` requests one manual occurrence without changing the schedule.
Repeated pending manual requests coalesce. Scheduled and manual occurrences
remain distinct and execute serially in the same thread.

Chore statuses are:

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for a schedule or manual activation |
| `running` | One occurrence has an active run |
| `done` | A finite schedule is exhausted with no manual request |

A failed or canceled run remains execution history and does not disable later
chore occurrences.


## Threads And Runs

Thread ids are derived from immutable job identity:

```text
task_<id>
chore_<id>
```

All task revisions, reopens, manual chore runs, and scheduled chore runs reuse
the same thread. Moving or archiving a job never deletes that thread or its run
history.

The stable job thread's create control stores minimal attribution without
changing run context:

```json
{
  "job": {
    "id": "3nprht9x",
    "kind": "task"
  }
}
```

Revision, schedule cursors, and trigger details remain exclusively in
`jobs.db`. A future root-run context change requires separate execution review.


## Caller Projection

The jobs API joins authored fields with the current scheduler checkpoint.
Execution history is projected independently from the job thread.

```json
{
  "id": "xy1234ab",
  "kind": "chore",
  "stage": "ready",
  "status": "pending",
  "title": "Check stale PRs",
  "schedule": "FREQ=HOURLY;INTERVAL=6",
  "path": "chores/xy1234ab.md",
  "runtime": {
    "thread_id": "chore_xy1234ab",
    "last_run": null,
    "next_run_at": "2026-04-23T12:00:00Z"
  }
}
```

Ready jobs normally have scheduler records. Draft and archived jobs have no
scheduler status. A ready job removed while running may retain one transient
checkpoint until its run becomes terminal.


## API Shape

Unified `/jobs` routes are read-only. Writes use `/tasks` and `/chores` so kind
semantics remain explicit.

Read operations cover ready and archived job lists and details. Write
operations cover creation, title/body/schedule edits, stage moves, task reopen
and cancel, chore manual run, and archived deletion. Stage operations mutate
catalog placement. Execution controls act on scheduler or run state and never
rewrite job output into the authored document.
