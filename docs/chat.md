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

One terminal `ChatInput` resolves to one `QuickCommand`, one or more
`RunOverride` values, or a policy-command sequence paired with
`RunnableInputRaw`. Only the runnable-input branch creates a start control and a
run in an existing thread. A client creates the thread explicitly before the
first run.

Thread ids use one underscore-delimited normalized form:

```text
<kind>_<id>
```

Examples:

- `task_3nprht9x`
- `chore_xy1234ab`
- `web_def456gh`
- `term_jk789mnp`
- `script_pqr234st`
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

Current roles are:

- `user`
- `assistant`
- `tool`

Messages use the shared canonical part vocabularies:

```text
PerceptPart = TextPart | ImagePart | AudioPart | DocumentPart
Percept     = PerceptPart[]
MessagePart = PerceptPart | ToolCallPart | ToolResultPart
Message     = { role: MessageRole, parts: MessagePart[] }
```

User messages contain only `PerceptPart` values. Assistant messages may
additionally contain `ToolCallPart` values, while tool messages contain only
`ToolResultPart` values.

The initial `start` control projects to the user message. Later `steer`
controls project to additional user messages in the same run. Step output
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

1. each run control with a message
2. each step message in run order

Forked chat threads store their source thread and anchor run in `parent`.
Inherited transcript context includes the anchor run. Run and step rows are not
copied into the new thread.


## Run API

Run detail returns:

- `input`
- `output`
- `controls`
- `steps`

The inherited `RunInfo` fields contain summary and lifecycle information.
`output` contains the canonical message parts resolved from the run's durable
output edge. `steps` contains the projected step detail used by trace and chat
inspection pages.

Run control endpoints are:

- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`

Thread lifecycle endpoints are:

- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`

`steer` and `cancel` require a running run. They can target chat, task, and
chore runs.

`rewind` removes the visible suffix of a branchable chat thread from the anchor
run onward. Superseded runs remain inspectable by id but are hidden from normal
thread projections. It does not start a replacement run.

`fork` creates a new chat thread whose inherited context ends with the anchor
run. It does not start a run in the new thread.

Both lifecycle request bodies may identify the anchor with `run_id`. Omitting
it selects the last visible top-level run. An anchor must be terminal. Fork
includes its anchor and may select an earlier terminal run while a later run
remains active. Rewind discards its anchor and requires the entire thread to
have no pending or running runs; callers must stop active runs before rewinding.

Task and chore thread ids are derived from job ids, so job threads cannot be
rewound or forked. Job execution commands expose explicit job semantics such as
`task reopen <id>` and `chore run <id>` instead.


## Chat API

The HTTP API models chat as thread management plus normal run execution. It
does not expose a separate `/chat` resource.

A client starts a new conversation by calling:

1. `POST /api/v1/threads` with `client` and an optional peer.
2. `POST /api/v1/runs/stream` with the returned thread id, runnable, percept
   input, optional model, and optional runnable arguments.

Subsequent turns reuse the same thread id. The client explicitly selects the
chat/default runnable. Persisted state is read through the normal thread and run
detail endpoints.

`GET /api/v1/models` returns model selectors inside the server's current
`AgentSetup.resource_filter` and reports `AgentSetup.bindings.model` when one is set.
A run applies its selected runnable's `models` directive after it starts.


## Streaming Rule

The canonical root-run stream is the primary real-time output surface for a
live chat exchange. It includes events from the complete recursive run tree.
Runtime surfaces should treat the canonical thread and root-run event streams
as the source of progress truth. A web client adapts native `RunEvent` values
into any UI-specific protocol locally.

The TUI does not consume the HTTP API. Its process calls `RunExecutor` directly
and renders native `RunEvent` values received through a `RunTracer`.

The chat TUI keeps only live mutable blocks in its live area. Stable blocks
move into terminal scrollback progressively instead of waiting for the whole
run to finish. Parallel tool calls, agic calls, and flow lanes are summarized by
their owning visible operation rather than finalized in completion order. See
[execution-presentation.md](./execution-presentation.md) for the shared display
language and the TUI's existing control-bar, streaming, alignment, and
scrollback constraints.

The input-box status bar is for transient editor and control feedback that has
no submitted timeline owner. Runnable input owns a scrollback block as soon as
it is submitted. If it is rejected before `RunBegin`, its diagnostic is
finalized in scrollback without a run id or run-status summary. After
`RunBegin`, terminal diagnostics and status summaries belong to the accepted
run and are finalized through its native events.

Thread and run detail endpoints are inspection surfaces used to:

- reload persisted history
- inspect past runs
- recover state after refresh

They are not the primary source for the in-flight assistant reply.
