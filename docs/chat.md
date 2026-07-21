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

One chat submission creates one start command, one run, and one thread when no
thread id is supplied.

Thread ids use one underscore-delimited normalized form:

```text
<kind>_<id>
```

Examples:

- `tsk_3nprht9x`
- `chr_xy1234ab`
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

The initial `start` command projects to the user message. Later `steer`
commands project to additional user messages in the same run. Step output
projects to assistant or tool messages.


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

1. each run command with a message
2. each step message in run order

Forked chat threads store their source thread and anchor run in `parent`.
Inherited transcript context includes visible source-thread runs before the
anchor run.


## Run API

Run detail returns:

- `info`
- `input`
- `output`

`output.steps` contains the projected step detail for the run. This is the
source used by trace and chat inspection pages.

Run control endpoints are:

- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`

Thread lifecycle endpoints are:

- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`

`steer` and `cancel` require a running run. They can target chat, task, and
chore runs.

`rewind` replaces the visible suffix of a branchable chat thread from the
anchor run onward and starts a replacement run in the same thread. Superseded
runs remain inspectable by id but are hidden from normal thread projections.

`fork` creates a new chat thread from the context before the anchor run and
starts one run in the new thread.

Both lifecycle request bodies identify the anchor with `run_id`. Fork requests
may additionally set `include_anchor`.

Task and chore thread ids are derived from job ids, so job threads cannot be
rewound or forked. Job execution commands expose explicit job semantics such as
`task reopen <id>` and `chore run <id>` instead.


## Chat API

Buffered chat:

- `GET /api/v1/models`
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

- `thread`
- `run`
- `message`
- `assistant`

`thread` is `ThreadInfo`; `run` is `RunInfo`.

Streaming chat:

- `POST /api/v1/chat/stream`

returns an SSE stream for the same run.

`GET /api/v1/models` returns the current chat-selectable model selectors
and the default selector after applying activation config and the `chat` agic.


## Streaming Rule

The stream is the primary real-time output surface for a live chat exchange.

Runtime surfaces should treat the canonical thread and run event streams as the
source of progress truth. The chat SSE endpoint exposes an AI SDK UI message
stream adapter for web clients that use AI SDK Elements; it is not the canonical
execution protocol.

UIs should keep exactly one active mutable block for the visible run. Finalized
blocks can move into scrollback immediately instead of waiting for the whole run
to finish. Parallel tool calls, agic calls, or flow lanes are rendered inside
the current mutable block.

Thread and run detail endpoints are inspection surfaces used to:

- reload persisted history
- inspect past runs
- recover state after refresh

They are not the primary source for the in-flight assistant reply.
