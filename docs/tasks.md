# Job Model

Toolang uses durable job definitions and a shared execution model.

Current built-in job kinds are:

- `task`
- `chore`

The jobs API also exposes one `will` endpoint for a long-horizon definition.
When no will is configured, that endpoint returns `null`.


## Tasks

Tasks are collaboration-oriented work items.

Task files live under:

- `${TOOLANG_ROOT}/agents/<agent>/tasks/*.md`

Current task fields are:

- `id`
- `requester`
- `status`
- `paused`
- `body`

Current task statuses are:

- `todo`
- `doing`
- `done`
- `cancelled`

Each task maps to one stable thread:

- `task:local:<id>`


## Chores

Chores are recurring local jobs.

Chore files live under:

- `${TOOLANG_ROOT}/agents/<agent>/chores/*.md`

Current chore fields are:

- `title`
- `rrule`
- `paused`
- `body`

Each chore maps to one stable thread:

- `chore:<name>`


## Will

The jobs API exposes:

- `GET /api/v1/will`

This surface returns the current will definition or `null`.


## Execution Mapping

Jobs do not define a separate execution model.

Toolang executes job work as normal runs:

- one job submission or scheduled trigger creates one run
- job history is queried through runs and threads
- job definition status stays separate from run status


## Scheduling

Recurring jobs use RRULE scheduling.

Current runtime behavior:

- chores are scheduled from `rrule`
- new or changed chores are enqueued once immediately, then follow `rrule`


## HTTP API

Current read endpoints:

- `GET /api/v1/tasks`
- `GET /api/v1/chores`
- `GET /api/v1/will`
