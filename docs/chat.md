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

One terminal `ChatInput` resolves to either one `QuickCommand` or one aggregate
`RunOverride` paired with `RunnableInputRaw`. Only the runnable-input branch
creates a run control and a run in an existing thread. A client creates the
thread explicitly before the first run.

Terminal interactions use complete slash commands such as `/help`, `/model`,
`/runnable`, `/allow`, `/limit`, `/models`, `/tools`, `/caps`, `/agics`,
`/flows`, `/output`, `/queue`, `/steer`, and `/keys`. `/show` remains an alias
for `/output`. Model, runnable, allow, and limit slash commands update
`SessionSetting`; matching colon-prefixed lines form the one-run `RunOverride`.
The plural resource commands inspect collections without changing the session.
`/help` and `/?` are the main guide and direct users to the special `:?`
interaction for leading run overrides. Dollar-prefixed `Content` lines expand
reusable prompts. See
[input-syntax.md](./input-syntax.md) for the namespace contract and
[call-input.md](./call-input.md) for runnable and prompt Call Input forms.

The three plural discovery commands produce compact, structured tables:

- `/models [-a] [QUERY]` shows `MODEL`, `PRICE ($/1M)`, and `EFFORT`. The current
  session model has a trailing ` *`; price is base input/output USD per million
  tokens; advertised effort levels retain catalog order.
- `/caps [-a] [QUERY]` shows `CAP`, `SCOPE`, `FORM`, and `DESCRIPTION`. Description
  display falls back from title metadata to description metadata and then the
  first content paragraph.
- `/tools [-a] [QUERY]` shows `TOOL` and `DESCRIPTION`. Tools in a structured
  toolset whose name starts with `_` are hidden from this display only.

The default base is the current session-allowed collection. `-a` changes the
base to all available resources and adds an `ALLOWED` column. A supplied query
is intersected with the base; summaries state the displayed count and the
allowed or available denominator. `/agics` and `/flows` use one-column tables
to list every available item of that kind and mark the current runnable with
the same ` *` suffix.

Each result derives its own display-cell column widths. A neutral separator
follows the header, every row remains one terminal line, and flexible cells use
`…` when the current output width cannot contain their full values. This
presentation does not change query matching or resource allow semantics.

Chat owns mutable model, runnable, allow, and limit session defaults. Model
parameters, including reasoning effort or token budget, live on the session's
concrete `ModelRequest`. Each submission snapshots that state with input-local
overrides into one self-contained `RunRequest`; queued submissions retain their
snapshot when the visible session changes. A submitted slash setting command
requires a body and never opens a picker. Completion may edit the command draft
but does not submit it or mutate the session.

Interactive Chat uses one send-oriented input model. Enter submits runnable
input when idle and appends it to the queue while a run is starting or active.
Meta+Enter sends the current normalized draft directly to the active run as
literal steer input; it does not parse leading slash, colon, dollar, or at-sign
syntax. A rejected steer retains the draft, while a locally accepted steer
records it in input history and clears the unchanged draft. Ctrl+J inserts a
newline, as does Shift+Enter when the terminal exposes it distinctly.

A non-empty Queue appears expanded above Input without taking focus. It fills
the terminal width and directly joins Input without a separator row. The areas
retain distinct backgrounds. Expanded Queue has a centered summary at the
top, up to four one-line previews, and panel hints at the bottom right. There is
no omitted-item count row. Entry numbers align with Input text. While focused,
selection is shown only by Input's background, without covering the accent
column; there is no selection marker. The selected entry reserves its right
side for dim edit, delete, and steer hints. Losing focus hides its highlight and
action hints while preserving the selected index.

Tab and Shift+Tab only switch focus, yielding to active input completion.
Space toggles the focused Queue without moving focus or selection. Collapsed
Queue occupies one row with the centered summary and right-aligned panel hints.
Entry actions are disabled while collapsed. Input keeps normal typing and
draft steering when focused.
Expanded Queue provides ↑/↓ or Ctrl+P/Ctrl+N selection, e editing, Meta+Enter
steering, and d or Del removal. Inline hints use dim lowercase `key action`
without brackets, separated by ` · `; `sp` abbreviates Space. `/keys` retains
standard labels and alternate keys in parentheses, such as `d (Del)`.
Unfocused Queue shows only `tab focus`. Focused, collapsed Queue shows
`sp expand · tab input`; expanded Queue shows `↑↓ select · sp collapse · tab input`
at the bottom right and `meta+enter steer · e edit · d delete` on the selected row.
Frequent actions for the current state come first in each tier.
On narrow terminals footer hints flow between complete actions, and selected
previews truncate to reserve action hints. Collapsed hints omit actions that
cannot fit beside the centered count, prioritizing expansion. Queue indicates
focus through its summary: normal text when focused and dim text when unfocused,
in both modes. Its reserved left column always blends into Queue's background
without shifting content. Key hints stay dim.
There is no separate header style.
Input's accent stays cyan; its cursor hides while Queue has focus and returns
to the preserved editing position afterward.

Editing does not overwrite an existing draft. Queue steering and
blocked-state handling retain the item when the steer cannot be accepted.
Run completion continues to submit queued requests in FIFO order and keeps
selection valid without changing the expanded/collapsed choice. Emptying the
queue resets the next non-empty Queue to expanded.

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

The initial `run` control keeps the authored input and the effective resolved
locals. Transcript and input-history projections use authored source, including
`$prompt` calls. Conversation recall uses resolved user-message parts. Later
`steer` controls project to additional user messages in the same run. Step
output projects to assistant or tool messages.


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
have no pending or running runs; callers must cancel active runs before rewinding.

Task and chore thread ids are derived from job ids, so job threads cannot be
rewound or forked. Job execution commands expose explicit job semantics such as
`task reopen <id>` and `chore run <id>` instead.


## Chat API

The HTTP API models chat as thread management plus normal run execution. It
does not expose a separate `/chat` resource.

A client starts a new conversation by calling:

1. `POST /api/v1/threads` with `client` and an optional peer.
2. `GET /api/v1/runs/defaults` once to adopt concrete session defaults.
3. `POST /api/v1/runs/authored/stream` with the returned thread id, concrete
   runnable and model requests, authored input, and materialized policy.

Subsequent turns reuse the same thread id. The client explicitly selects the
chat/default runnable. Persisted state is read through the normal thread and run
detail endpoints.

`GET /api/v1/models` returns concrete refs from the server's current effective
`AgentSetup.models` collection, base per-million input/output prices, and
structured reasoning-effort metadata. A run
resolves the submitted ref with singular-selection semantics, validates its
typed model parameters, then applies its selected runnable's `models`
directive. Ambiguous routes must be narrowed by the configured model queries
before they are usable by Chat.


## Streaming Rule

The canonical root-run stream is the primary real-time output surface for a
live chat exchange. It includes events from the complete recursive run tree.
Runtime surfaces should treat the canonical thread and root-run event streams
as the source of progress truth. A web client adapts native `RunEvent` values
into any UI-specific protocol locally.

The TUI selects one `ExecutionRuntime` after materializing the agent layout. A
healthy running AgentServer is reused for resident, roaming, and visiting
layouts. An explicit `--sandbox` must match that runtime; Chat never stops or
reconfigures an attached server. When no server is active, Chat resolves the
explicit selector, then the merged root/agent `[sandbox]` binding, then `host`.
Host execution uses the process-local `LocalRunClient`; a non-host selector
starts a temporary AgentServer and uses `RemoteRunClient` through its API.
Chat stops only the temporary workload it launched. Both paths render the same
native `RunEvent` values. `--dev PATH` may provide a Toolang wheel, or a
directory containing one, when Chat creates that temporary non-host runtime;
it cannot modify an attached server and does not apply to embedded host mode.
On exit, Chat reports the stop and sandbox-release stages while it cleans up a
temporary runtime. Attached AgentServers are left running and need no cleanup
progress.

Remote acceptance records the root run id before the first event so cancel and
steer remain addressable. If an accepted stream disconnects, the TUI keeps the
queue paused and polls durable run detail after 500 ms, 1 s, 2 s, and then every
5 s. Terminal durable truth finalizes the run without inventing missed events
and directs the user to `/output RUN_ID` for the complete output. An ambiguous
pre-acceptance failure, missing accepted run, or invalid recovery identity
blocks further submissions until Chat restarts; read-only commands and exit
remain available. Chat never retries a submission or falls back to embedded
execution after selecting the remote runtime.

The startup banner keeps the TUI process, executor, and sandbox identities
separate. Its metadata order is always `Toolang`, `executor`, `sandbox`, then
`home`:

```text
Toolang   v0.2.7-87-g69439a4e*
executor  http://localhost:7001 · v0.2.7-88-gc73484a9
sandbox   docker:python:3.13-slim · 5741cca76066
home      ~/.toolang/agents/eve
```

Host execution, including embedded Chat, renders a plugin-supplied operating
system identity such as `sandbox  host · macOS 27.0 arm64`. Remote
endpoints are terminal hyperlinks. A remote executor version is omitted only
when it exactly matches the known, clean TUI version; matching dirty versions
and `unknown` remain visible because they do not prove identical source.
Adjacent identity values use a dim ` · ` separator, and every form keeps the
same panel padding.

The sandbox description is optional runtime-profile presentation metadata. Its
absence or a `null` value does not block Chat: host execution uses the local host
sandbox plugin description, and Docker continues to use the reported container
instance. Profile readers ignore unknown additive fields, allowing the TUI and
executor to use different source releases without coupling their deployment.

The chat TUI keeps only live mutable blocks in its live area. Stable blocks
move into terminal scrollback progressively instead of waiting for the whole
run to finish. Parallel tool calls, agic calls, and flow lanes are summarized by
their owning visible operation rather than finalized in completion order. See
[execution-presentation.md](./execution-presentation.md) for the shared display
language and the TUI's existing control-bar, streaming, alignment, and
scrollback constraints.

The input-box status bar is for editable rejected input, local controls, and
unresolved asynchronous state. A bare or unknown slash command and any run
rejected before dispatch or queue insertion retain their text and cursor so the
user can edit or retry them. Their transient diagnostic clears on the next edit,
Esc, recognized command, or accepted run. Empty Enter is a no-op. Connection and
submission-safety diagnostics are persistent: they survive edits, command
results, and setting refreshes until the corresponding recovery state or Chat
restart. Persistent diagnostics take precedence over transient ones.

Status diagnostics occupy one physical line, use a visible `!` marker, and are
elided at the terminal edge. Once a runnable input is accepted, its terminal
diagnostics and status summaries belong to the run and are finalized through
native events.

A submitted slash command likewise owns an immutable scrollback interaction.
Its summary states the concrete effect or result without a generic `Success:`
or `Result:` prefix; usage and errors retain explicit `Usage:` and `Error:`
labels. Setting commands refresh the status bar after committing the new
session value, but the status bar is not their confirmation channel. Slash
summary and detail rows align with other output using a two-space indent and no
leading marker column.

The status bar's right side shows the canonical session model ref without a
field label. An empty effective model collection appears as
`[no models available]`. An explicit
effort level or token budget appears as `MODEL · VALUE`; a model that advertises
effort-level or token-budget control but has no explicit session value appears
as `MODEL · auto`. Models without either control, toggle-only models, and models
whose metadata cannot be resolved show only `MODEL`. Status metadata lookup is
limited to the exact selected model and does not block Chat when it fails. At
narrow widths the model ref is elided before an applicable effort suffix.

`/?`, `:?`, and `/keys` are read-only scrollback interactions. Their purpose and
composition constraint appear before copyable forms. Keyboard help is generated
from the same Toolang-owned shortcut metadata used to bind interactive Chat;
ordinary terminal cursor and text-editing keys are intentionally omitted.

Thread and run detail endpoints are inspection surfaces used to:

- reload persisted history
- inspect past runs
- recover state after refresh

They are not the primary source for the in-flight assistant reply.
