# Chat and Transcript Model

Chat is a projection over threads, runs, and messages.

It does not define a separate execution model.


## Threads And Runs

Chat uses the same runtime units as the rest of Toolang:

| Term | Meaning |
| --- | --- |
| `thread` | Durable conversation context |
| `run` | One handling attempt inside that thread |
| `step` | One execution unit inside the run |

One chat submission creates one run in one thread.

Thread ids use one underscore-delimited normalized form:

```text
<kind>_<id>
```

Examples:

- `task_3nprht9x`
- `chore_xy1234ab`
- `chat_ab12cd34`
- `web_def456gh`
- `tui_jk789mnp`
- `tg_123456789`

The parser splits on the first `_`; the trailing id may contain additional
underscores.

Run ids use:

```text
run_<id>
```

The `<id>` part is encoded with the `run` id family when Toolang owns the run
id. See [ids.md](./ids.md).


## Messages

The public message shape is:

- `id`
- `thread_id`
- `run_id`
- `step_index`
- `role`
- `parts`
- `created_at`
- `meta`

Current roles are:

- `user`
- `assistant`
- `tool`

Current part kinds are:

- `text`
- `image`
- `audio`
- `file`
- `tool_call`
- `tool_result`

The initial run input projects to the user message. Step output projects to
assistant or tool messages.


## Thread API

Thread list responses return:

- `id`
- `title`
- `updated_at`
- `origin`
- `peer`
- `parent`
- `run_count`
- `latest_run`

`peer` defaults to:

```json
{ "type": "user", "name": "user", "thread": null }
```

Agent-to-agent threads use `peer.type = "agent"` with the peer agent name and
that peer's local thread id when known. `parent` is a local parent thread id and
is not used for cross-agent thread references.

Thread detail returns:

- `info`
- `runs`

There is no separate top-level `thread.messages` field.

To build a full transcript, flatten:

1. each run input
2. each step message in run order


## Run API

Run detail returns:

- `info`
- `input`
- `output`

`output.steps` contains the projected step detail for the run. This is the
source used by trace and chat inspection pages.


## Chat API

Buffered chat:

- `GET /api/v1/chat/models`
- `POST /api/v1/chat`

request body:

- `thread`
- `client`: `web`, `tui`, or `chat`; defaults to `web` and controls the prefix
  for newly allocated chat thread ids
- `peer` optional; defaults to the user peer
- `message`
  - `role`
  - `parts`
- `model` optional selected model selector

returns:

- `thread_id`
- `run_id`
- `message`
- `assistant`

Streaming chat:

- `POST /api/v1/chat/stream`

returns an SSE stream for the same run.

`GET /api/v1/chat/models` returns the current chat-selectable model selectors
and the default selector after applying activation config and the `chat` thunk.


## Streaming Rule

The stream is the primary real-time output surface for a live chat exchange.

Thread and run detail endpoints are inspection surfaces used to:

- reload persisted history
- inspect past runs
- recover state after refresh

They are not the primary source for the in-flight assistant reply.
