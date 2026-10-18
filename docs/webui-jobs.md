# Chore API

Chore definitions are current files in `chores/` or inline `.too` declarations.
There are no task/job collections or draft/archive stages. See
[Chores](tasks.md) and [execution drivers](work.md).

| Request | Behavior |
| --- | --- |
| `GET /api/v0/chores` | List effective definitions and execution state |
| `GET /api/v0/chores/{name-or-id}` | Read effective definition and state |
| `POST /api/v0/chores` | Create a home definition with required `name` |
| `PATCH /api/v0/chores/{name-or-id}` | Update home `title`, `body`, or `schedule` |
| `DELETE /api/v0/chores/{name-or-id}` | Remove home definition, retain checkpoint/history |
| `POST /api/v0/chores/{name-or-id}/run` | Request a serialized manual occurrence |
| `POST /api/v0/chores/{name-or-id}/cancel` | Cancel active or pending work |

Creation accepts optional `title`, `body`, and `schedule`. Missing schedule means
one-off. Use a new name for a new identity. Root and inline definitions appear in
inspection but must be edited at their source.

Responses include `name`, `ref`, derived `id`, title, source `path` and optional
`line`, schedule, and `runtime`. Runtime contains `thread_id`, `status`,
`active_run_id`, `pending_at`, `next_scheduled_at`, `last_run`, `scheduler_error`,
and checkpoint `updated_at`. `synced` indicates whether the checkpoint describes
the current body and schedule; false means its status still belongs to an older
or unobserved definition. Definitions have no execution timestamps or guessed
remote issue fields. Status is null before the driver creates a checkpoint,
then pending/running/done/failed/canceled. A recurring chore remains pending
while future work exists; an already exhausted schedule starts as done.

CLI and API inspection read the same current source definitions without resolving
handlers. The driver uses accepted State for inline definitions and handlers.
PATCH/DELETE reject root or inline definitions with 409 and identify the source
to edit. Creating a home file hidden by an inline declaration also returns 409.

Authoring and inspection work with the chore driver disabled. Manual dispatch
returns 409 with `{ "code": "driver_disabled", "driver": "chore" }`.
Unknown definitions return 404. Refresh the collection after a mutation and
poll it for background progress. Execution history is available through the run
and thread APIs; chore threads cannot be forked or rewound.

Interactive WebUI execution must use `/api/v0/chat/runs` and carry `client_id`
and `request_id`. Thread creation uses `/api/v0/threads`. Disabling chat gates
these operations while retaining management and noninteractive script APIs.
