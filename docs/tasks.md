# Authored Jobs

This guide owns Markdown task and chore files, their identity and stage
transitions. [Program job declarations](program.md#job-declarations) are another
source of scheduled work. The shared runtime scheduling and recovery model is
defined in [work.md](./work.md).

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
| `id` | before publication | Stable identity unique within the agent home |
| `title` | no | Human-readable display label |

Chores add:

| Field | Required | Meaning |
| --- | --- | --- |
| `schedule` | no | RFC 5545 RRULE; defaults to `FREQ=HOURLY;INTERVAL=1` |

There is no separate job `name`. The id is the machine selector, title is the
optional label, and path is the current source location. New catalog-created
jobs use `<id>.md`; renaming that file does not change identity.

Within one agent home, job IDs are unique across task/chore kinds and every stage. Both id and
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

The body is run-only input defined by [Call Input](call-input.md): a
`RunOverride` prefix and one `CallInput[str]`. It has no ambient template
variables. Includes resolve relative to the Markdown file, and prompt templates
receive only explicit arguments and input.

The scheduler retains the body as source and parses it only when dispatching.
The surface default is `task` or `chore`, falling back to the single unnamed entry when that
runnable is absent. Resolution evaluates the input source into
`RunSpec.input["_"]` and binds arguments under their names in the same map.

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

Publication and body revisions activate tasks. Titles and paths do not change
body identity. [Task scheduling](work.md#task-semantics) owns coalescing, terminal
status, cancellation and reopen behavior.

## Chore Document

```md
---
id: xy1234ab
title: Check stale PRs
schedule: "FREQ=HOURLY;INTERVAL=6"
---

Check stale PRs and report actionable items.
```

[Chore scheduling](work.md#chore-semantics) owns RRULE anchoring, missed-occurrence
coalescing, manual requests and failure recovery. A body edit changes later
occurrences without triggering an immediate Run.

## Threads And Runs

Thread ids are derived from immutable job identity:

```text
task_<id>
chore_<id>
```

All task revisions, reopens, manual chore runs, and scheduled chore runs reuse
the same thread. Moving or archiving a job never deletes that thread or its run
history.

The thread's `origin` is `task` or `chore`; its ID identifies the job. Its
`create` Control has an empty payload. Revision, schedule cursors, and trigger
details remain exclusively in `jobs.db`. Runs associate with the job through
their thread.


## Caller Projection

CLI/API job lists and details project the Markdown catalog, joining authored
fields with the current scheduler checkpoint and a latest-run summary from the
stable job thread. They do not enumerate `agent.too` declarations. Program jobs
still have scheduler checkpoints and durable threads/runs; inspect their
execution through the thread and run interfaces. Catalog edits and stage moves
operate on Markdown files.

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
    "next_run_at": "2026-04-23T12:00:00Z",
    "error": null
  }
}
```

Ready jobs normally have scheduler records. Draft and archived jobs have no
scheduler status. A ready job removed while running may retain one transient
checkpoint until its run becomes terminal.

`runtime.error` reports scheduler-side validation, dispatch, or recovery
failures. When present, `last_run` includes its own `error` field for execution
failure details. Neither value is written back to the authored Markdown.


## Implementation and verification

[Authored jobs](../src/toolang/catalog/job.py) owns file formats and stage moves.
[Catalog tests](../tests/unit/catalog/test_authored_jobs.py) and
[job integration tests](../tests/integration/catalog/test_jobs.py) verify identity,
validation and transitions. [HTTP jobs](api.md#jobs-and-consumer-projections) owns the
consumer contract; [scheduling](work.md) owns checkpoint status and dispatch.
