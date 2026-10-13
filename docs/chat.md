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

One terminal `ChatInput` resolves to either one `ChatCommand` or one aggregate
`RunOverride` paired with `CallInput[str]`. Only the runnable-input branch
creates a run control and a run in an existing thread. A client creates the
thread explicitly before the first run.

Standalone slash commands such as `/help`, `/models`, `/tools`, `/caps`,
`/agics`, `/flows`, `/output`, and `/keys` inspect or control Chat. `/show`
remains an alias for `/output`. Colon commands such as `:model`, `:runnable`,
`:allow`, `:limit`, and `:workdir` inspect or change session settings.

Run directives describe one submission: leading `/model`, `/allow`, `/limit`,
and `/workdir` lines override that run; `/NAME` selects its root runnable;
`$PROMPT` expands a prompt and `@RESOURCE` includes a resource. `/help` and `/?`
cover the complete syntax; `:?` focuses on session settings. See
[input-syntax.md](./input-syntax.md) for dispatch and migration, and
[call-input.md](./call-input.md) for input boundaries and signatures.

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

Chat owns mutable model, runnable, workdir, allow, and limit session defaults. Model
parameters, including reasoning effort or token budget, live on the session's
concrete `ModelRequest`. Each submission snapshots that state with input-local
overrides into one self-contained `RunRequest`; queued submissions retain their
snapshot when the visible session changes. A submitted colon setting command
requires a body, except the `:workdir` query, and never opens a picker. Completion
may edit the command draft but does not submit it or mutate the session.

Interactive Chat uses one send-oriented input model. Enter submits runnable
input when idle and appends it to the queue while a run is starting or active.
Meta+Enter sends the current normalized draft directly to the active run as
literal steer input; it does not parse leading slash, colon, dollar, or at-sign
syntax. A rejected steer retains the draft, while a locally accepted steer
records it in input history and clears the unchanged draft. Ctrl+J inserts a
newline, as does Shift+Enter when the terminal exposes it distinctly.

Each submitted control keeps one blank row above and below its complete message,
with two-cell text insets. Each bar accents the first cell of its first message
line. Root input bars share the output width (up to 120 columns by default) and show
the submitted runnable, model, and reasoning value in their bottom-right
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
dismisses transient status and never changes focus. Ctrl+L clears the display
when idle and places the input area at the top without a leading blank row,
preserving prior output in native scrollback. Ctrl+Q exits from either area. `/keys`
groups Input, Queue, and global actions explicitly.

Chat and Talk retain native terminal scrollback during resize. Repainting keeps
the live input from drifting across repeated resizes; the terminal may still
reflow historical rows or retain rows it scrolled before notifying the application.

Input, Queue, and both status bars share one width limit. Set
`TOOLANG_INPUTBOX_MAX_WIDTH` to a positive integer to control this entire input
area independently from message output. When unset, it uses
`TOOLANG_PROGRESS_MAX_WIDTH` (120 columns by default). Both limits narrow to fit
the terminal; empty or invalid configured values are errors. For example:

```sh
TOOLANG_PROGRESS_MAX_WIDTH=120 TOOLANG_INPUTBOX_MAX_WIDTH=80 too chat
```


Submitted controls and message output retain the progress width. A non-empty
Queue appears expanded above Input without taking focus and directly joins it
without a separator row.
The areas retain distinct backgrounds. Queue accents its leading cell with Steer's
magenta, so a left accent bar runs beside the summary and entries much as
Input's cyan accent frames the prompt. Expanded Queue has a right-aligned
summary immediately above up to eight one-line previews, and a trailing blank
row that separates Queue from Input; collapsed Queue keeps only its summary.
There is no omitted-item count row. Short terminals show fewer entries to keep
Input, status, and the summary frame visible. Entry icons (`↳`) align with
Input text and remain dim; body text stays normal. While focused, selection is
shown only by Input's background, starting one cell after the accent and
reaching Queue's right edge. There is no selection marker. The selected entry
reserves its right side for slightly brighter dim action hints, separated from
the body by at least two cells. Entry hints end two cells from Queue's right
edge. The status bar insets its text by two cells on each side.
Losing focus hides its highlight and action hints while preserving the selected index.

Tab and Shift+Tab only switch focus, yielding to active input completion.
Space toggles the focused Queue without moving focus or selection. Entry actions
are disabled while collapsed. Input keeps normal typing and draft steering when
focused. Expanded Queue provides ↑/↓ or Ctrl+P/Ctrl+N selection, e editing,
Meta+Enter steering, and d or Del removal. Queue's summary shows only `N queued`,
right-aligned with two cells of right padding and always dim, including while
focused or collapsed. The selected entry shows
`(m-enter)steer (e)dit (d)elete` at its right in dim text; `m-enter` abbreviates
Meta+Enter. `/keys` retains standard labels and alternate keys in parentheses,
such as `d (Del)`. On narrow terminals selected previews truncate first, then
action hints truncate. The summary reduces right padding before truncating the
count. Queue indicates focus through its selection highlight when expanded;
collapsed focus does not change its appearance. Its leading cell carries the
accent bar and content never shifts. There is no separate header style.
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

- `talk_3nprht9x`
- `chore_xy1234ab`
- `chat_def456gh`
- `work_jk789mnp`
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

- `POST /api/v1/chat/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`

Thread lifecycle endpoints are:

- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`

`steer` and `cancel` require a running run. They can target chat, talk, and
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

Only chat threads can be rewound or forked. Use `chore run <name>` to request
another chore occurrence. Chore identities and checkpoints stay stable.


## Chat API

The HTTP API models chat as thread management plus normal run execution. It
does not expose a separate `/chat` resource.

A client starts a new conversation by calling:

1. `POST /api/v1/threads` with `client_id`, `request_id`, and an optional peer.
2. `GET /api/v1/runs/defaults` once to adopt concrete session defaults.
3. `POST /api/v1/chat/runs/authored/stream` with the returned thread id, concrete
   runnable and model requests, authored input, and materialized policy.

Subsequent turns reuse the same thread id. The client explicitly selects the
chat/default runnable. Persisted state is read through the normal thread and run
detail endpoints.

`GET /api/v1/models` returns concrete refs from the server's current effective
`AgentSetup.models_effective()` collection, base per-million input/output prices, and
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

The TUI ensures one AgentServer is ready after materializing the agent layout. A
healthy running server is reused for resident, roaming, and visiting
layouts. An explicit `--sandbox` must match that runtime; Chat never stops or
reconfigures an attached server. When no server is active, Chat resolves the
explicit selector, then the merged root/agent `[sandbox]` binding, then `host`.
Both host and non-host selectors start a persistent AgentServer and use
`RemoteRunClient` through its API. Concurrent starters converge on that server;
Chat waits for an existing startup and reports readiness failures.
`--dev [PATH]` may provide a Toolang wheel, or a directory containing one, when
Chat creates a new non-host runtime.
Bare `--dev` searches the process working directory (`.`); omitting the option
keeps the existing package selection. It cannot modify an attached server and
does not apply to host mode. Closing Chat leaves the server running, including
one it started. Use `too stop <agent>` to stop it explicitly.

Remote acceptance records the root run id before the first event so cancel and
steer remain addressable. An interrupted stream first resumes with GET and the
last committed cursor; completed structure replaces partial rendering. If these
bounded attempts fail, the TUI keeps the queue paused and polls durable run detail after 500 ms, 1 s, 2 s, and then every
5 s. Terminal durable truth finalizes the run without inventing missed events
and directs the user to `/output RUN_ID` for the complete output. An ambiguous
pre-acceptance failure, missing accepted run, or invalid recovery identity
blocks further submissions until Chat restarts; read-only commands and exit
remain available. Chat never retries a submission or falls back to embedded
execution after selecting the remote runtime.

The startup banner caption is `Chat <client-version>`. Its three metadata rows
are always `runtime`, `sandbox`, then `workspaces`:

```text
runtime     v0.3.8
sandbox     docker · python:3.13-slim
workspaces  lab, toolang
```

The runtime row shows only its version, including when it matches the client
version, is dirty, or is `unknown`. Known versions have one `v` prefix. Use
`too -V` for the client version and `too AGENT info` for the API endpoint.
Host execution uses the same sandbox format, for example
`sandbox  host · macOS 27.0 arm64`. Docker shows the complete image name and tag
or digest. Container IDs and agent-home paths are omitted from the banner;
the runtime profile still exposes its container identity.

The workspace names are a startup snapshot from the connected runtime's
`GET /api/v1/workspaces`. Only available workspaces are listed, in response
order, without filesystem paths. An empty list renders `none`; failed or
malformed inspection renders `unavailable` without blocking Chat. The existing
status bar continues to show the current workspace as it changes. Use
`too AGENT workspace list` to inspect current workspace mappings and availability.

Sandbox driver and environment use a dim ` · ` separator. The banner preserves its
padding and folds long metadata values in narrow terminals. An overlong caption
moves inside the panel; very narrow terminals omit decoration. Two blank rows
separate its bottom border from both the initial input box and the first
submitted run control bar. The input placeholder is `Describe your task`.
Chat requests a blinking block cursor and resets the cursor shape to the
terminal default on exit.

The host sandbox description remains optional runtime-profile presentation
metadata. A missing or `null` value uses the local host sandbox plugin's
fallback description. Docker uses the reported image selector for display and
still validates the runtime's container identity. Profile and workspace readers
ignore unknown additive fields so presentation metadata can evolve independently.

The chat TUI keeps only live mutable blocks in its live area. Stable blocks
move into terminal scrollback progressively instead of waiting for the whole
run to finish. Parallel tool calls, agic calls, and flow lanes are summarized by
their owning visible operation rather than finalized in completion order. See
[execution-presentation.md](./execution-presentation.md) for the shared display
language and the TUI's existing control-bar, streaming, alignment, and
scrollback constraints.

The input-box status bar is for editable rejected input, local controls, and
unresolved asynchronous state. Unknown colon commands and run submissions
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

A submitted command likewise owns an immutable scrollback interaction.
Its summary states the concrete effect or result without a generic `Success:`
or `Result:` prefix; usage and errors retain explicit `Usage:` and `Error:`
labels. Setting commands refresh the status bar after committing the new
session value, but the status bar is not their confirmation channel. Command
summary and detail rows align with other output using a two-space indent and no
leading marker column.

The session status bar below Input keeps the session runnable in its left
corner and the session model plus effort in its right corner. The center anchor
is the Chat agent and workspace in `agent@workspace` form; it is fixed at the
status line's absolute center. The workspace comes from session settings while
idle and from the active root run while running. The active runnable is never
duplicated beside the model. Successful root `runtime.chdir` changes update
the workspace. `:workdir` changes the session workspace for subsequent runs; after
the current run ends, the center returns to the latest session workspace. Only
the workspace name is shown, not the path. If the full status line overflows,
the centered identity stays intact, with one space between adjacent status
items; the session runnable and model truncate toward the center with a single
`…` when needed. On extremely narrow terminals, both session edge labels are
hidden to show the center alone; the center is truncated only if it cannot fit
by itself.

A separate run status bar sits immediately above the Queue panel, or Input when
no inputs are queued. It uses two rows: a blank separator followed by dim elapsed
time indented two cells from the left. The label is `Working` below one second,
then `Working for 1s`, `Working for 1m3s`, or `Working for 1h1m1s`. Both rows
remain blank while idle. Ctrl+L collapses these rows until the next run starts;
after that run settles, the blank rows remain to keep Input stable. The right
side is reserved for future context information and displays nothing yet. The session
status bar never shows elapsed time, and its errors do not interrupt the run
bar's timer. On very short terminals, the run bar yields rows to keep Input and
steer feedback usable.

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

## Terminal titles and tmux

Chat runs in the invoking pane and publishes `alice: thread title` through OSC 0,
falling back to `alice` until a title is available. Existing asynchronous lookup
opportunities update the title; starting Chat does not eagerly create a thread.
The agent component is limited to 12 display columns; the complete title to 32.

Toolang never creates, selects, or renames tmux containers. Each foreground pane
publishes its own `@toolang_kind=chat`, root, agent, and thread marks; the thread
starts empty and updates when normally created. See
[terminal configuration and marks](api.md#terminal-titles-and-pane-marks).

To display OSC titles in tmux's window bar:

```tmux
set -g window-status-format '#I:#{pane_title}'
set -g window-status-current-format '#I:#{pane_title}'
```

A window displays its active pane's title. tmux must allow title updates;
Toolang does not change that setting. A binding can choose its own pane:

```tmux
bind-key C-g split-window 'exec too eve chat'
```
