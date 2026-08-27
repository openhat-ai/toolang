# Job Scheduling

Toolang schedules durable background jobs without introducing a separate
`Work` entity. The model has three concepts:

- `Job` is the latest effective task or chore definition.
- `JobRecord` is the scheduler checkpoint for a currently ready job.
- `Run` is one durable execution attempt and the only execution history.

Chat and direct script calls create runs directly. They do not create jobs or
scheduler records. File watching and long-lived duties are outside the current
job model.


## Vocabulary

```python
JobKind = Literal["task", "chore"]

JobStatus = Literal[
    "pending",
    "running",
    "done",
    "failed",
    "canceled",
]

JobTrigger = Literal[
    "source",
    "schedule",
    "manual",
]
```

`JobTrigger` is private scheduler state. It is not execution metadata.

Tasks use every `JobStatus`. Chores normally use `pending`, `running`, and
`done`; a failed or canceled chore run does not disable later occurrences.


## Identity And Definition

A job id is globally unique across task and chore kinds and every authored
stage. It is the sole scheduler key. Both `id` and `kind` are immutable:

```text
id       durable identity
kind     immutable behavior
stage    authored lifecycle placement
revision current executable body version
```

Moving a job between draft, ready, and archived stages, renaming its source
file, editing its body, or changing its schedule preserves its id. Copying a
job or changing between task and chore creates a new id. A duplicate id is an
invalid authored state.

The runtime normalizes ready authored sources and program declarations into:

```python
@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: JobKind
    title: str | None
    body: str
    schedule: str | None
    revision: str
    source: str
    path: Path | None
```

`revision` hashes the normalized executable body. Title, file name, path, and
schedule do not affect it. The schedule is compared separately so an RRULE
change can reset its cursor without pretending that the body changed.

`id` is the machine-readable selector and `title` is the only optional
human-readable label. There is no additional job `name`. A display title falls
back to the first meaningful body line and then the id.

The thread id is a projection rather than persisted scheduler state:

```python
thread_id = f"{job.kind}_{job.id}"
```

All runs for one job share that thread.


## Ready Snapshot

Only ready Markdown directories are watched for execution:

```text
tasks/*.md
chores/*.md
```

Draft and archived directories are cold catalog storage. Catalog operations may
inspect them, but `JobWatcher` does not scan them during runtime.

`JobWatcher` publishes immutable ready snapshots. Filesystem notifications are
wakeup hints: it debounces a change, reads a stable complete snapshot, and
publishes only a different value. Startup always performs a complete ready
refresh, and an infrequent safety refresh repairs missed notifications.

Program task and chore declarations arrive through `StateWatcher` and are
inherently ready. `JobScheduler` merges both sources by id. A duplicate id is
an error; sources do not silently shadow one another.

A manually added ready file without an id receives one under the authored-job
write lock before the snapshot is published.


## Scheduler Checkpoint

`.runtime/jobs.db` is a mutable checkpoint, not authored truth or execution
history. Its logical row is:

```python
@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    kind: JobKind
    revision: str
    status: JobStatus
    ready_at: str | None

    active_run_id: str | None
    active_revision: str | None
    active_trigger: JobTrigger | None
    active_at: str | None

    schedule_revision: str | None
    schedule_anchor: str | None
    next_run_at: str | None

    error: str | None
    created_at: str
    updated_at: str
```

`ready_at` represents a pending task revision or one coalesced manual chore
request. The active fields preserve a claimed activation across the non-atomic
boundary between `jobs.db` and `runs.db`. Chore schedule fields own the stable
RRULE cursor. Scheduler validation errors are retained in `error` because they
may occur before a run exists.

The checkpoint does not copy the job body, title, source path, thread id, run
counters, occurrence history, or run results.

`JobStore` upgrades older checkpoint schemas only in writable scheduler mode.
It rejects a newer schema before changing journal mode or schema objects, so an
older binary cannot downgrade future scheduler state. Read-only inspection
requires the current schema and never migrates it.

Normally the database has one row for each effective ready job. A job that
leaves ready is removed immediately unless it has an active run. An active row
is retained only until that run becomes terminal, preventing a stage move from
losing recovery state or allowing a concurrent replacement run.


## Task Semantics

A new ready task is pending immediately. A body edit changes its desired
revision:

- a pending task keeps its original `ready_at` and replaces the pending body;
- a running task continues with its captured revision and queues the latest
  revision for after completion;
- a terminal task becomes pending when its body revision changes;
- an unchanged terminal task does not repeat;
- an explicit reopen makes the same revision pending again.

A failed or canceled task does not retry automatically. Editing its body or
reopening it requests another run. One task has at most one active run and one
coalesced pending revision.


## Chore Semantics

A chore schedule is an RFC 5545 RRULE. `schedule_anchor` supplies a stable
`DTSTART` when the authored rule does not. Startup and body edits preserve the
anchor. Changing the RRULE establishes a new anchor and cursor.

`next_run_at` is the earliest scheduled occurrence not yet claimed. At a
scheduled dispatch, the scheduler coalesces missed occurrences and advances
from the schedule rather than from run completion:

```python
scheduled_at = rule.before(now, inc=True)
next_run_at = rule.after(scheduled_at, inc=False)
```

This produces one catch-up run after downtime, prevents long executions from
shifting the recurrence, and bounds backlog growth. If another occurrence
becomes due while the chore is running, it remains due and produces at most one
follow-up run after completion.

A body edit affects the next run but does not itself trigger a chore. A manual
run uses `ready_at` and does not change the RRULE cursor. Repeated manual
requests coalesce to one pending manual occurrence. Manual and scheduled
occurrences remain distinct and run serially.

When a finite RRULE is exhausted and no manual request remains, the chore is
done. Failed and canceled runs are occurrence outcomes; they do not stop a
recurring chore.


## Event Loops

Execution and scheduling have separate owner loops:

- The execution loop owns the API server, the single process-local
  `RunExecutor`, every run task, run control, tracing, and execution events.
- A dedicated scheduler thread and event loop own `JobScheduler`, `JobWatcher`,
  RRULE timers, the due heap, and `jobs.db` transitions.

The scheduler never calls `RunExecutor.run()` from its own loop and execution
never calls back into the scheduler. The scheduler submits one coroutine with
`asyncio.run_coroutine_threadsafe()`. That coroutine invokes `start()` and
awaits the returned handle entirely on the execution loop. The scheduler awaits
the resulting `concurrent.futures.Future` on its own loop and applies the
terminal result after it resolves. Dependency therefore remains one-way from
`toolang.work` to `toolang.execution`.

`AgentSetup`, `AgentState`, `Job`, and `RunSpec` values crossing the boundary
are immutable snapshots. Async tasks, SQLite connections, and loop-bound
futures never cross it.


## Scheduling Loop

`JobScheduler` owns the current job map, checkpoint map, active dispatches, and
one in-memory min-heap. The heap contains task `ready_at`, manual chore
`ready_at`, and chore `next_run_at`. Version tokens provide lazy invalidation
when a job changes; due jobs are not discovered by scanning SQLite.

The scheduler wakes for a ready snapshot, state snapshot, run completion,
manual control, the nearest heap timer, or the safety refresh. It captures the
latest setup and state snapshots when constructing a dispatch. The job body is
parsed into a policy-command prefix and `RunnableInputRaw`; the default runnable
is the job kind and falls back to `default`.

One job is always serial. Different jobs may run concurrently. `JobScheduler`
adds no separate bandwidth pool or limit. Any future process-wide admission
policy belongs at the execution boundary so API, chat, task, and chore traffic
share the same policy.


## Execution Attribution

The scheduler does not modify `RunSpec`, `RunExecutor`, or execution-owned run
context. The stable job thread's existing create control carries the minimum
attribution already accepted by the execution API:

```python
{
    "job": {
        "id": job.id,
        "kind": job.kind,
    }
}
```

Runs are associated with the job through that thread. Trigger, revision,
RRULE, scheduled timestamps, next timestamps, and scheduler status remain
exclusively in `jobs.db`. Adding root-run job context would change the execution
contract and requires a separately reviewed execution design.


## Dispatch And Recovery

Cross-database dispatch is recoverable without copying scheduler state into a
run:

1. Parse and validate the latest job against captured setup and state.
2. Preallocate a run id.
3. Atomically claim the activation in `jobs.db`, including its trigger and
   timestamp.
4. Submit and await `RunExecutor.run(run_id=...)` through one cross-loop
   future.
5. Persist the terminal run in `runs.db` before reporting completion.
6. Apply the terminal result to `jobs.db` on the scheduler loop.

If `RunExecutor` explicitly rejects a submitted start, the scheduler consumes
the activation as a scheduler failure and records the error. A crash between
claim and acceptance instead leaves a recoverable active checkpoint. At
startup the scheduler loads the ready snapshot and looks up only recorded
`active_run_id` values in `runs.db`:

- a missing run releases its claim and restores the captured activation;
- a terminal accepted run repairs the job transition;
- a still-pending or running accepted run is retained as blocked rather than
  duplicated, because interruption recovery is execution-owned behavior.

Normal scheduling never scans or queries `runs.db`. Run history is not used to
discover work. A checkpoint write failure makes the scheduler unhealthy and
halts new dispatch instead of continuing with ambiguous state.

An identical move out of and back into ready while the process is stopped is
intentionally unobservable. The final ready snapshot and existing checkpoint
win. Re-execution in that case requires a body edit, task reopen, or manual
chore run.


## Inspection And Control

Job inspection joins the authored/effective job with its scheduler record and
the latest run summary for the stable job thread. This is an inspection path,
not a scheduler dependency on `runs.db`. CLI job lists expose scheduler status,
latest-run status, the next chore occurrence, and the most relevant scheduler
or run error. The small control surface retains source meaning:

- list and get jobs;
- reopen or cancel a task;
- manually run a chore;
- stop an active run through `RunExecutor`.

There is no public claim, occurrence, generic Work, or scheduler-run control
abstraction.


## Ownership

`toolang.catalog` owns authored job files and stage transitions.
`toolang.work` owns `Job`, `JobRecord`, `JobWatcher`, `JobStore`, job inspection,
and `JobScheduler`. `toolang.execution` owns threads, runs, execution, and run
controls and does not depend on `toolang.work`.
