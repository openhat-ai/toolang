# Toolang Execution Model

This document defines the current runtime model.

Layout lives in [layout.md](./layout.md).
Caps live in [caps.md](./caps.md).
API surfaces live in [api.md](./api.md).


## 1. State Forms

Toolang uses three forms of state:

- `durable`
  - persisted source of truth
- `prepared`
  - immutable compiled snapshot
- `live`
  - in-memory state used by the next run

Rules:

- authored definitions are durable
- operational facts are also durable, but separate from authored definitions
- `prepared` is generated from authored definitions only
- `live` is loaded from one `prepared` snapshot and patched by operational facts
- one run binds one snapshot and must not switch snapshots mid-run


## 2. Durable State

Durable state has two parts:

- `definitions`
  - `.too`
  - local caps
  - task, chore, and will documents
- `operational facts`
  - run truth
  - task status
  - scheduler cursors
  - prepare status

Rules:

- definition changes may require a new `prepared` snapshot
- operational changes must not trigger `prepare` by default
- `execution.db` is the durable store for runtime truth and operational facts


## 3. Runtime Process

One runtime process owns:

- `live caps`
- `live jobs`
- run queue
- active runs

Built-in loops:

- `chat`
- `pulse`
- `poll`
- `hook`
- `control`
- `inspect`
- `watcher`

Rules:

- `chat`, `pulse`, `poll`, and `hook` produce run requests
- `control` writes durable state
- `inspect` returns merged durable, prepared, and live views
- `watcher` observes definition changes, prepares a new snapshot, and swaps `live`
- loops do not execute model work directly; the runner does


## 4. Term Rules

Toolang uses these terms:

- `job`
  - the umbrella term for task, chore, and will definitions
- `task`
  - one collaboration-oriented job definition
- `chore`
  - one recurring job definition
- `will`
  - one long-horizon recurring job definition
- `run`
  - one execution attempt

Rules:

- use `job` for the shared definition layer
- use `run` for runtime execution
- do not use `running task` or `running job` for an executing run
- one runtime process owns active runs, not active tasks


## 5. Prepare And Live Refresh

Rules:

- `prepare` builds immutable snapshots such as `prepared.caps` and `prepared.jobs`
- dirty checks use definition fingerprints, not operational facts
- unchanged definitions reuse the existing prepared snapshot
- successful prepare updates `prepared` first, then swaps `live` atomically
- a Web UI write may appear in `inspect` before it becomes `live`

Useful inspect states:

- `prepare_pending`
- `prepare_complete`
- `prepare_error`
- `live`


## 6. Jobs And Task State

Tasks use two separate axes:

- operational status
  - `todo`
  - `doing`
  - `done`
  - `cancelled`
- placement
  - `active`
  - `archived`

Rules:

- task status is an operational fact
- active task definitions enter `prepared.jobs`
- archived tasks do not enter `prepared.jobs`
- `todo`, `doing`, `done`, and `cancelled` must not trigger `prepare`
- archiving removes a task from the active prepare set
- pulse admission uses active definitions overlaid with operational facts


## 7. Run Pipeline

Each admitted run uses five stages:

1. `bind`
2. `assemble`
3. `execute`
4. `commit`
5. `emit`

`bind` fixes the run-local inputs:

- `run_id`
- `thread_id`
- `origin`
- `thunk`
- `snapshot_id`

`assemble` is a separate runner stage.

It reads:

- the bound prepared snapshot
- operational facts
- message history
- runtime metadata

It returns one `PromptBundle`:

- model
- messages
- runtime context
- tool runtime
- trace payload

Rules:

- prompt assembly is read-only
- prompt assembly must not fetch, sync, prepare, or mutate durable state
- `execute` runs the model and tools
- `commit` writes runs, steps, messages, and operational updates
- `emit` sends replies and events
