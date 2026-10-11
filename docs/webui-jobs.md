# Chore API

Chore definitions are current files in `chores/` or inline `.too` declarations.
There are no task/job collections or draft/archive stages. See
[Chores](tasks.md) and [execution drivers](work.md).

| Request | Behavior |
| --- | --- |
| `GET /api/v1/chores` | List effective definitions and execution state |
| `GET /api/v1/chores/{name-or-id}` | Read effective definition and state |
| `POST /api/v1/chores` | Create a home definition with required `name` |
| `PATCH /api/v1/chores/{name-or-id}` | Update home `title`, `body`, or `schedule` |
| `DELETE /api/v1/chores/{name-or-id}` | Remove home definition, retain checkpoint/history |
| `POST /api/v1/chores/{name-or-id}/run` | Request a serialized manual occurrence |
| `POST /api/v1/chores/{name-or-id}/cancel` | Cancel active or pending work |

Creation accepts optional `title`, `body`, and `schedule`. Missing schedule means
one-off. Use a new name for a new identity. Root and inline definitions appear in
inspection but must be edited at their source.

Responses include `name`, `ref`, derived `id`, title, path, schedule, status, and
`runtime` with `thread_id`, `last_run`, `next_run_at`, and dispatch errors.
They have no `kind` or `stage`. Status is null before the driver creates a
checkpoint, then pending/running/done/failed/canceled. A recurring chore normally
remains pending after an occurrence when a future occurrence exists.

Authoring and inspection work with the chore driver disabled. Manual dispatch
returns 409 with `{ "code": "driver_disabled", "driver": "chore" }`.
Unknown definitions return 404. Refresh the collection after a mutation and
poll it for background progress. Execution history is available through the run
and thread APIs; chore threads cannot be forked or rewound.

Interactive WebUI execution must use `/api/v1/chat/runs` and carry `client_id`
and `request_id`. Thread creation uses `/api/v1/threads`. Disabling chat gates
these operations while retaining management and noninteractive script APIs.
