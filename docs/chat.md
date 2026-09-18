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
`RunOverride` paired with `CallInput[str]`. Only the runnable-input branch
creates a run control and a run in an existing thread. A client creates the
thread explicitly before the first run.

Terminal interactions use complete slash commands such as `/help`, `/model`,
`/runnable`, `/allow`, `/limit`, `/models`, `/tools`, `/caps`, `/agics`,
`/flows`, `/output`, and `/keys`. `/show` remains an alias
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

Each submitted control keeps one blank row above and below its complete message,
with two-cell text insets. Each bar accents the first cell of its first message
line. Root input bars fill the terminal width and show the submitted
runnable, model, and reasoning value in their bottom-right
padding, for example `agic:research · openai/gpt-5 · high`. This is the request
snapshot, including queued overrides; later default changes do not alter it.
Without a request model, the label reads `model unspecified`. Otherwise, the
corner shows the requested effort or token budget, or `auto` when the request
has no reasoning override. It reads the submission snapshot without querying
model metadata or persisted records.

Steers retain independent magenta-accented bars. A single live `•` line below them shows
how many are sending or waiting to apply after the current step (or waiting for
the next model call when no step is active). A blank row above and below separates
this explanation from the bars and Queue; very short viewports prioritize the
text over this spacing. Control receipts and the consuming step's control
references determine adoption. Adopted bars move into history
without extra labels or success messages. If a run ends with a steer confirmed
unapplied, only that bar's bottom-right padding says `not applied`. Uncertain
delivery uses existing error feedback without asserting non-adoption. Steer
rule colours remain unchanged across states. The status bar insets its text by two
cells on each side so it lines up with the areas above it.

Keyboard controls replace `/queue`, `/q`, `/steer`, and `/s`; those names are
unregistered. Esc Esc, Ctrl+C, and Ctrl+D apply only while Input is focused.
They never cancel the run, clear the draft, or exit Chat from Queue. Esc only
dismisses transient status and never changes focus. Ctrl+L retains its global
clear-display behavior when idle, and Ctrl+Q exits from either area. `/keys`
groups Input, Queue, and global actions explicitly.

A non-empty Queue appears expanded above Input without taking focus. It fills
the terminal width and directly joins Input without a separator row. The areas
retain distinct backgrounds. Queue accents its leading cell with Steer's
magenta, so a left accent bar runs beside the title and entries much as Input's
cyan accent frames the prompt. Expanded Queue has a left-aligned summary, a
blank gap row, and up to eight one-line previews. There is no omitted-item
count row. Short terminals show fewer entries to keep Input, status, the
summary, and the gap visible. Entry icons (`↳`) align with Input text and
remain dim; body text stays normal. While focused, selection is shown only by
Input's background, starting one cell after the accent and reaching Queue's
right edge. There is no selection marker. The selected entry reserves its
right side for
slightly brighter dim action hints, separated from the body by at least two
cells. Entry hints end two cells from Queue's right edge, both when expanded
and when collapsed. The status bar insets its text by two cells on each side.
Losing focus hides its highlight and action hints while preserving the selected index.

Tab and Shift+Tab only switch focus, yielding to active input completion.
Space toggles the focused Queue without moving focus or selection. Collapsed
Queue occupies one row with the left-aligned summary and its inline state hint.
Entry actions are disabled while collapsed. Input keeps normal typing and
draft steering when focused.
Expanded Queue provides ↑/↓ or Ctrl+P/Ctrl+N selection, e editing, Meta+Enter
steering, and d or Del removal. Queue's summary reads `N queued` followed by one
dim parenthesized state hint: `(tab focus)` while unfocused, `(sp collapse)`
while focused and expanded, and `(sp expand)` while focused and collapsed. The
selected entry shows `m-enter steer · e edit · d delete` at its right. Inline
hints use dim lowercase `key action` without brackets; `sp` abbreviates Space
and `m-enter` abbreviates Meta+Enter. `/keys` retains standard labels and
alternate keys in parentheses, such as `d (Del)`. On narrow terminals selected
previews truncate first, then the summary's state hint, keeping the count. Queue
indicates focus only through its selection highlight; the summary always uses
normal text. Its leading cell carries the accent bar and content never shifts.
There is no separate header style.
Input's accent stays cyan and Queue's stays magenta; Input's cursor hides while
Queue has focus and returns to the preserved editing position afterward.

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
native `RunEvent` values. `--dev [PATH]` may provide a Toolang wheel, or a
directory containing one, when Chat creates that temporary non-host runtime.
Bare `--dev` searches the process working directory (`.`); omitting the option
keeps the existing package selection. It cannot modify an attached server and
does not apply to embedded host mode.
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

Status diagnostics occupy one physical line, put a visible `!` marker in the first
column, and are elided at the terminal edge. Once a runnable input is accepted,
its terminal diagnostics and status summaries belong to the run and are finalized
through native events.

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

## Tmux Marks

Chat running inside a tmux pane records what it hosts, each value at the scope it
belongs to, so a tmux-side view can read it without knowing anything about Toolang:

| option | scope | value | written |
| --- | --- | --- | --- |
| `@toolang_agent` | session | agent name | when `too <agent> chat` determines the agent's session |
| `@toolang_thread_id` | window | full thread id, e.g. `term_6xp42qxg` | as soon as the thread exists: at start with `--thread`, otherwise when chat creates it |
| `@toolang_thread_title` | window | thread title, single line, at most 60 display columns | once the thread has runs |
| `@toolang_pad` | pane | `chat` | when chat starts |

Each value lives at exactly one scope, because tmux inherits user options: a window
with no value of its own reads its session's, and a pane with none reads its
window's. The agent is session metadata, so every window in the agent's session
reports it; the thread marks stay on the one window that shows that thread; the pad
mark names what the pane runs. A pad is a readable and writable thread view — `chat`
today, later `shell`, `logs` and friends, all in the same window under the same
thread id.

The names follow the same convention: the agent's session is named after the agent,
and a thread's window after its thread id — `new_chat` while a new chat has no id yet,
the full id once chat knows it. Names and options are container state: they outlive
chat and are never restored, because the next chat reuses the same session and window.

Marks are best-effort, and only the pad mark ends with chat: the pane stops claiming
to be a chat while the container keeps its name and thread. They are written only when
the process runs inside a pane (`TMUX` and `TMUX_PANE` are set), and a failing tmux
call never reaches the UI. `TOOLANG_TMUX_MARKS=0` disables the feature entirely, and
`TOOLANG_TMUX_DEBUG=1` reports skipped writes on stderr.

The values are not read back by the CLI: they exist for tmux. A status line can show
the current window's thread with `#{@toolang_thread_title}`, and
`tmux list-windows -a -F '#{window_name} #{@toolang_thread_id}'` finds the windows
that host chat.

### Recommended Configuration

tmux reads none of these options by default, so `prefix w` shows only the container
names (`term_xxx`, or `new_chat` before the thread exists). This window line keeps the
default tree and reads the marks instead — the thread title once it exists, the thread
id until a run has named it, the pad kind for a pane that hosts a pad, and the window
name for anything else:

```tmux
# ~/.tmux.conf
bind -N 'Choose a window' w choose-tree -Zw -F '#{?pane_format,#{pane_index}: #{pane_current_command}#{pane_flags},#{?window_format,#{window_index}: #{?@toolang_thread_title,#{@toolang_thread_title},#{?@toolang_thread_id,#{@toolang_thread_id},#{?@toolang_pad,[#{@toolang_pad}],#{window_name}}}}#{window_flags},#{session_windows} windows#{?session_attached, (attached),}}}'
```

```text
a: 3 windows (attached)
  0: hello world
  1: shell
  2: [chat]
```

A window line reads the active pane's pad, so `[chat]` appears before the thread
exists. A status line reads the same values from the current window, for example
`set -g status-right '#{?@toolang_thread_title,#{@toolang_thread_title},#{?@toolang_agent,#{@toolang_agent},}}'`;
prefixing the session branch with `#{?@toolang_agent,#{@toolang_agent} · ,}` shows the
agent beside the session name.

## Tmux Agent Sessions

Inside tmux, `too <agent> chat` runs in the agent's own session in the user's own
server, so `prefix w` lists one entry per open chat:

```text
eve
  term_xxx
  new_chat
```

The session is named after the agent and each window after its thread, so the tree
reads without any configuration; the
[recommended configuration](#recommended-configuration) shows the thread titles
instead.

| situation | behaviour |
| --- | --- |
| not inside tmux | chat runs in the current terminal |
| `--thread` container with a live chat pad | the client switches to that window, focusing the chat pane |
| `--thread` container without a live chat pad | a chat pad opens in that window and the client switches to it |
| the current session is the agent's session | chat runs in this pane |
| otherwise | the agent's session is ensured, a window opens running `too <agent> chat …`, and the client switches to it |

The `--thread` rows come first: an open thread is reused even when chat was started
inside the agent's session, so one thread never gets a second chat.

A run that is moved elsewhere prints one line, `↪ opened in tmux session <agent>`,
and exits 0; the pane it started in returns to its shell.

The agent's session is found by its `@toolang_agent` session option first and by
its derived name second; determining it records the option, so the name is only a
fallback. The derived name is the agent name reduced to lowercase
`[a-z0-9-]`; a name another agent already owns is suffixed (`eve-2`) instead of
renamed.

A thread's window is a container: the launcher creates it and names it after the
thread (`new_chat` until the id exists, then `term_xxx`). Chat only adds or removes its
pad, so a window whose chat exited is reused: `--thread` finds it by the window mark,
switches when a live chat pad is anywhere in it — focusing that pane — and otherwise
opens a fresh chat pad there instead of opening a second window for the thread.

An opened window runs the chat command under tmux's own environment, like any
other tmux window, so it does not inherit variables that only the calling shell
exported. Exiting a chat closes its pane: a window whose only pane it was is
destroyed, and the agent's session with it when that was the only window; placement
turns `detach-on-destroy` off on the sessions it creates, so the client returns to
the session it came from instead of being detached.

Placement is best-effort and shares the marks kill switch: outside tmux, with
`TOOLANG_TMUX_MARKS=0`, or when a tmux call fails, chat runs in the current
terminal.
