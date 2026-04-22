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
