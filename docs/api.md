# Control Surfaces

This document defines the public CLI and local agent HTTP API.

Interactive CLI, TUI, and WebUI surfaces recognize and handle their slash and
shell commands before passing remaining input to `ContentBody` parsing and input
perceiving. Command dispatch belongs to the control surface rather than the
Toolang input syntax.


## CLI

The CLI entry points are:

- `toolang`
- `too`
- `caps`

Top-level commands are:

- `new`
- `clone`
- `remove`
- `list`
- `info`
- `chat`
- `steer`
- `cancel`
- `rewind`
- `fork`
- `threads`
- `runs`
- `inspect`
- `model`
- `run`
- `start`
- `stop`
- `task`
- `chore`
- `skill`
- `psyche`
- `service`
- `prompt`

Global options:

- `--root`
- `--version`

Cap commands:

- `caps [AGENT] list [--filter <csv>]`
- `caps [AGENT] <kind> list [--filter <csv>]`
- `caps [AGENT] <kind> new <name>`
- `caps [AGENT] <kind> edit <name>`
- `caps [AGENT] <kind> delete <name>`
- `caps [AGENT] <kind> add <ref>`
- `caps [AGENT] <kind> remove <name>`
- `caps [AGENT] <kind> template [template-name]`

`<kind>` is one of `psyche`, `skill`, `service`, or `prompt`. Without `AGENT`,
cap mutations target root caps. With `AGENT`, they target the selected agent home's caps.

List output uses:

- `KIND`
- `CAP`
- `SOURCE`
- `FORM`
- `SCOPE`

Kind-specific list commands omit `KIND`.

`SOURCE` is the authored source location. File sources are paths relative to
the Toolang root. Inline caps use `<path-to-agent.too>:<line>`. External GitHub
sources are shown as directly accessible `https://github.com/...` URLs.

`FORM` accepts `inline`, `ref`, `wired`, and `file`. `SCOPE` accepts
`root`, `home`, and `here`. `--filter` accepts kind, form, and scope values for
all-kind lists. Kind-specific lists accept only form and scope values. Values in
one group are unioned; different groups are intersected.

Typical usage:

```bash
toolang new alice
toolang list
PY_LOG=toolang.execution=info toolang ./examples/script-playground.too summarize "Summarize this workspace"
toolang ./examples/script-playground.too --help
toolang ./examples/script-playground.too summarize "Summarize this workspace"
toolang ./examples/file-agent.too --inbox ./inbox
toolang run alice
toolang run alice --sandbox docker
toolang run brice/alice
toolang run https://toolang.ai/alice.too
toolang clone brice/alice
toolang start alice
toolang start alice --sandbox docker
toolang stop alice
toolang info alice
toolang alice chat
toolang alice chat term_3nprht9x
toolang alice chat --sandbox docker
toolang alice threads
toolang alice runs --thread term_3nprht9x
toolang alice inspect run_ppkp9e94
toolang alice steer run_ppkp9e94 "Use the smaller patch"
toolang alice cancel term_3nprht9x
toolang alice rewind run_ppkp9e94
toolang alice fork run_ppkp9e94
toolang model list
```

Thread and run listing, inspection, steering, cancellation, rewind, and fork
open the selected agent's durable execution store directly. They do not start
or call the agent HTTP server. A run id selects its owning thread; a thread id
selects its active run for steering or cancellation and its latest terminal run
for rewind or fork. Fork retains the anchor run, while rewind removes it and the
following visible suffix.


## Agent Selectors

Runtime commands accept these selector forms:

| Form | Meaning |
| --- | --- |
| `name` | A local managed agent such as `alice` |
| `shorthand` | A convention-based remote selector such as `brice/alice` or `toolang.ai/alice` |
| `ref` | A canonical remote ref such as `github://brice/agents/alice.too@main` or `https://toolang.ai/alice.too` |

Current shorthand expansion rules are:

| Shorthand | Expanded refs |
| --- | --- |
| `owner/name` | probes `github://owner/agents/agents/name.too@<default-branch>`, then `github://owner/agents/name.too@<default-branch>` |
| `owner/repo/name` | probes `github://owner/repo/agents/name.too@<default-branch>`, then `github://owner/repo/name.too@<default-branch>` |
| `host/name` | `https://host/name.too` |

Three-part shorthand specifies the repository exactly. It does not probe other
repository names.

GitHub refs must include one revision suffix:

- `github://owner/repo/path/to/agent.too@rev`

`rev` is one git revision token. Toolang does not distinguish branch, tag, and
commit in selector syntax.

Foreground runtime port selection depends on the agent mode:

| Mode | Selector | Default port behavior |
| --- | --- | --- |
| Resident | Local managed name such as `alice` | Reuse the agent's last port when available, otherwise choose from `7001-7999` |
| Visiting | Remote selector such as `brice/alice` or `https://toolang.ai/alice.too` | Reuse the visiting root's last port when available, otherwise choose an OS temporary port |
| Script run | Local `.too` path with an agic or flow name | No HTTP runtime port; the executable runs directly in the CLI process |
| Roaming file runtime | Local `.too` path with `--inbox` and no agic name | Choose an OS temporary port |


## Script Run Surface

A script run uses one local `.too` source path directly:

```bash
toolang SCRIPT RUNNABLE [OPTIONS] [ARGS] [INPUT]...
```

Arguments:

- `SCRIPT` is the local Toolang script or agent file
- `RUNNABLE` is the uniquely named public agic or flow to run
- `ARGS` provide named runnable parameters, written as `NAME=VALUE`
- `INPUT` values form a `ContentBody` perceived as the canonical primary
  `Percept`

Behavior:

- a local `.too` path enters script-run mode
- the hidden `toolang script` command displays the generic path-based usage;
  it is not a prefix and does not accept a script path
- default agics and generated internal agics are not exposed as script commands
- runnable command descriptions come only from their authored `doc`
- stdout is reserved for the final runnable result
- progress messages are written to stderr only when stderr is a TTY
- `-q` or `--quiet` suppresses progress messages
- `-v` or `--verbose` shows execution boundaries even when stderr is not a TTY
- `--model SELECTOR` chooses one model for the run
- execution happens directly in the current CLI process; script run does not
  currently accept `--sandbox`
- `PY_LOG=toolang.execution=info toolang a.too summarize ...` writes runtime logs
  under `.toolang/agents/<agent>/.runtime/logs/<runnable>/<run_id>.log`
- `PY_LOG=debug toolang a.too summarize ...` also writes lower-level provider
  and HTTP logs to that run log file
- `toolang a.too --help` lists public runnables
- `toolang a.too summarize --help` prints runnable-specific dynamic usage
- `toolang a.too` shows usage instead of running a default agic
- script run exposes the agent's effective tools, subject to runnable
  directives
- `NAME=VALUE` supplies one named argument and is coerced using its declared
  parameter type
- `INPUT` rules:
  - adjacent ordinary shell words are joined with spaces into one text item
  - `TEXT` adds one text part; use `@@TEXT` for literal text beginning with `@`
  - `@PATH` adds one path-based percept part; text-like paths become text parts
  - image extensions such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, and `.svg` infer image parts
  - `.mp3` and `.wav` infer audio parts
  - supported document extensions infer document parts
  - unsupported video, archive, executable, and binary formats are rejected
  - omitting input reads non-interactive stdin; `-` explicitly selects stdin
- `--` ends option parsing so later arguments stay `INPUT` values
- `--option` is reserved for Toolang runtime options
- `PY_LOG` uses env_logger-style directive formatting and does not affect stdout
- key execution events are recorded in `runs.db` for script runs just like chat,
  task, and chore runs

The same roaming source path selects its durable execution history:

```bash
toolang SCRIPT threads
toolang SCRIPT runs [--thread THREAD]
toolang SCRIPT inspect TARGET
```

These are the only agent-management commands currently routed for roaming
sources. Visiting selectors will gain the same three read-only commands when
their `run`, `start`, and `stop` target resolution is unified.

## File Request Runtime

Roaming scripts can also start a foreground file request runtime without naming
a runnable:

```bash
toolang SCRIPT --inbox PATH [--inbox PATH...]
```

Behavior:

- `SCRIPT` is materialized into its sibling `.toolang` roaming root.
- Each `--inbox` value must name an existing directory.
- Startup enables `runner.file` and `trigger.file`; AgentState watching is always active.
- Startup requires an agic named `file` that accepts primary input and has no
  required named parameters.
- Files already present in an inbox at startup are eligible for processing.
- Newly discovered stable files are passed to the `file` agic using the same
  percept-part classification rules as `@PATH`.
- File request progress is stored in `.runtime/files.db`.
- Finished, failed, and canceled file fingerprints are not automatically retried.
- When a runnable name is present, such as `toolang SCRIPT summarize ...`,
  Toolang uses normal one-shot runnable invocation.


## Runtime Commands

| Command | `name` | `shorthand` | `ref` |
| --- | --- | --- | --- |
| `toolang run` | yes | yes | yes |
| `toolang clone` | yes | yes | yes |
| `toolang start` | yes | no | no |

Behavior:

| Command | Behavior |
| --- | --- |
| `toolang run` | Runs a local agent, or fetches one remote agent program into a stable visiting root and runs it in the foreground |
| `toolang clone` | Clones one local agent, or fetches one remote agent program into a new local managed agent |
| `toolang start` | Starts one local managed agent only. Remote selectors must be cloned first |

`toolang run` and `toolang start` share the same startup preparation path after
selector handling. Both resolve the runnable target, reject already-active
runtimes, build a `StartupSpec`, prepare the agent state, and then launch from
that resolved startup checkpoint. `run` starts the resolved runtime in the
foreground. `start` serializes the same resolved startup into the hidden
background `toolang run` command. The hosting shell resolves `--sandbox`; the
API process inside the selected host always runs the same server core and the
executor does not branch on its host.

When `--sandbox` is omitted, resident run/start commands use the effective
root/home `[sandbox]` binding, falling back to `none` when no binding exists.
An explicit selector, including `--sandbox none`, overrides that binding.

For host hosting, `run` keeps the API process in the current foreground process
and `start` launches a detached process. For managed hosting such as Docker,
both commands launch the same foreground API process inside the managed host;
`run` waits for that host and stops it on exit, while `start` returns after the
API is ready.

Agent entrypoints also share one logging policy resolver:

| Entrypoint | Log destination |
| --- | --- |
| `toolang run` | `stderr` |
| `toolang start` | `agent_log` under the agent `.runtime` directory |
| local `.too` script run | `run_log` under the agent `.runtime` directory when `PY_LOG` is set, otherwise `none` |

When `toolang start` runs without `--port`, Toolang first tries the agent's last
runtime port. If that port is not reusable, Toolang scans its auto-assigned
local range `7001-7999`, starting at `7001` and counting upward, skipping ports
already recorded by other local agents, instead of asking the OS for a random
ephemeral port.


## Model Commands

- `toolang model list`
- `toolang model providers`
- `toolang model adapters`

`toolang model list` shows selectable models, including:

- canonical ref under the `MODEL` column
- provider name
- profile details such as streaming, tool support, context window, output limits, and price metadata
- a summary count after the table

Pass `--models` to preview selector filtering, for example
`toolang model list --models "[remote]"` or
`toolang model list --models "openai/*[openrouter]"`.

`toolang model providers` shows provider and alias config health,
including missing key environment variables and endpoints. `toolang model
adapters` lists installed model adapter names.


## Plugin Commands

- `toolang plugin list`

`toolang plugin list` shows installed plugins by family. Model provider rows also
include richer discovery details such as:

- readiness based on required environment variables
- default API base URL when known
- discovered model count


## Agent HTTP API

Each running agent exposes one local FastAPI server.

The process assembles one `RunExecutor`, one `StateWatcher`, and five catalog
instances for the application lifetime: one `AuthoredJobs`, private and shared
`AuthoredCaps`, and private and shared `WiredCaps`. These objects are
fields of one `ApiContext` stored on `app.state`. One request dependency returns
that context, and route functions use its fields directly. Application-wide
FastAPI dependencies are reserved for side-effect-only concerns such as
authentication or common validation. FastAPI lifespan owns required startup and
shutdown; module globals, `ContextVar`, and router-factory closures do not carry
application state.

`RunExecutor.start()` returns a `RunHandle`; the application retains handles
only when its own protocol needs additional lifecycle bookkeeping.

Core endpoints are grouped as:

- `agent`
- `chat`
- `caps`
- `jobs`
- `runs`
- `threads`

Non-interactive execution uses `POST /api/v1/runs/stream`. It accepts an agic
or flow's unique `runnable` name, primary input, optional model, and optional
declared arguments, and returns the canonical trace event stream for HTTP
clients. CLI script runs and TUI execution do not consume this endpoint.


## Agent Endpoints

- `GET /healthz`
- `GET /api/v1/profile`
- `GET /api/v1/models`
- `GET /api/v1/agics`
- `GET /api/v1/flows`

`/api/v1/profile` returns:

- profile metadata
- environment summary
- overview metrics:

| Metric Group | Contents |
| --- | --- |
| `threads` | Thread totals grouped by chat, chore, and task |
| `steps` | Step totals grouped by `model_call`, `tool_call`, and `runtime` |
| `tokens` | Aggregated input, output, and total token usage |

`GET /api/v1/models` returns the effective selectable model selectors for chat
runs after applying the current activation config and the `chat` agic's
`models` directive. The response includes:

- `default`
- `items`
  - `selector`
  - `name`
  - `ref`
  - `provider`
  - `model`
  - `adapter`
  - `tools`
  - `streaming`

`GET /api/v1/agics` and `GET /api/v1/flows` list the agent's executable
definitions.


## Cap Endpoints

Summary:

- `GET /api/v1/caps`

Collections:

- `GET /api/v1/psyches`
- `GET /api/v1/skills`
- `GET /api/v1/services`
- `GET /api/v1/prompts`

Detail:

- `GET /api/v1/psyches/{name}`
- `GET /api/v1/skills/{name}`
- `GET /api/v1/services/{name}`
- `GET /api/v1/prompts/{name}`

Templates:

- `GET /api/v1/psyches/templates`
- `GET /api/v1/skills/templates`
- `GET /api/v1/services/templates`
- `GET /api/v1/prompts/templates`
- `GET /api/v1/psyches/templates/{template_name}`
- `GET /api/v1/skills/templates/{template_name}`
- `GET /api/v1/services/templates/{template_name}`
- `GET /api/v1/prompts/templates/{template_name}`

Write:

- `PUT /api/v1/psyches/{name}/file`
- `PUT /api/v1/skills/{name}/file`
- `PUT /api/v1/services/{name}/file`
- `PUT /api/v1/prompts/{name}/file`
- `DELETE /api/v1/psyches/{name}/file`
- `DELETE /api/v1/skills/{name}/file`
- `DELETE /api/v1/services/{name}/file`
- `DELETE /api/v1/prompts/{name}/file`
- `PUT /api/v1/psyches/{name}/wired`
- `PUT /api/v1/skills/{name}/wired`
- `PUT /api/v1/services/{name}/wired`
- `PUT /api/v1/prompts/{name}/wired`
- `DELETE /api/v1/psyches/{name}/wired`
- `DELETE /api/v1/skills/{name}/wired`
- `DELETE /api/v1/services/{name}/wired`
- `DELETE /api/v1/prompts/{name}/wired`

File write bodies use:

- `visibility`: `private` or `shared`; defaults to `private`
- `content`: raw cap content

Wired write bodies use:

- `visibility`: `private` or `shared`; defaults to `private`
- `ref`: external cap ref

Delete routes accept `visibility=private|shared` as a query parameter. Cap read
items include:

- `name`
- `description`
- `scope`
- `origin`
- `form`
- `ref`
- `definition_file`
- `line` when known
- `editable`

`visibility` is an HTTP write-placement field, not a CLI list concept:
`shared` maps to root-authored caps and `private` maps to the current
agent's authored caps. Read payloads expose runtime `form`, `scope`, and
`origin`; CLI list commands project those into `SOURCE`, `FORM`, and runtime
`SCOPE`.


## Chat Endpoints

- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`

Chat request body uses:

- `thread`
- `client`: `web` or `term`; defaults to `web` and controls the prefix for
  newly allocated chat thread ids
- `peer` optional thread peer descriptor
  - `type`: `user` or `agent`; defaults to `user`
  - `name`: peer name; defaults to `user`
  - `thread`: peer-local thread id; defaults to `null`
- `message`
  - `role`: must be `user`
  - `parts`
- `model` optional selected model selector for this run
- `runnable` optional executable name; omission uses the chat/default runnable

`message.parts` accepts canonical message parts such as:

- `text`
- `image`
- `audio`
- `document`

Actual part support still depends on the selected model route. The built-in
OpenAI Chat Completions and Responses adapters map text, image, audio, and
document inputs. Chat Completions rejects a `DocumentPart` that has only a
document URL; the caller must first provide document data or a provider file
id.

For multipart payload details:

- an image part's `image_url` may be a remote URL or a local `data:` URL
- an audio part's `data` should be base64 payload; `data_url` is also accepted
  as an alias and is normalized to base64
- a document part's `url` is for remote documents
- a document part's `data` carries inline provider-facing document data and may
  be a full `data:...;base64,...` URL
- `file_id` references a document already uploaded to the selected provider

`POST /api/v1/chat` returns one completed `ChatResult` containing `thread`,
`run`, `message`, and `assistant` projections.

`POST /api/v1/chat/stream` returns one SSE stream that follows an AI SDK UI
message stream subset. This endpoint is an adapter for chat UI clients. The
canonical progress protocol is exposed through the live run and thread
streams.

The CLI command for interactive chat is
`toolang <agent> chat [thread] [--sandbox <selector>]`.
Without a thread id, the TUI creates a terminal chat thread on first input. With
a thread id, it continues that thread. The TUI runs in its own process, assembles
the same core objects, calls `RunExecutor` directly, and observes native
`RunEvent` values through a `RunTracer`. It does not depend on the chat or run
SSE endpoints. Starting an agent HTTP server remains a separate CLI operation.
Job thread ids are inspectable and controllable through thread and run commands,
but `chat` does not implicitly reopen tasks or create manual chore runs.


## Job Endpoints

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/archived`
- `GET /api/v1/jobs/archived/{job_id}`
- `GET /api/v1/tasks`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `PATCH /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/draft`
- `POST /api/v1/tasks/{task_id}/ready`
- `POST /api/v1/tasks/{task_id}/archive`
- `POST /api/v1/tasks/{task_id}/reopen`
- `POST /api/v1/tasks/{task_id}/cancel`
- `GET /api/v1/tasks/archived`
- `GET /api/v1/tasks/archived/{task_id}`
- `PATCH /api/v1/tasks/archived/{task_id}`
- `DELETE /api/v1/tasks/archived/{task_id}`
- `GET /api/v1/chores`
- `POST /api/v1/chores`
- `GET /api/v1/chores/{chore_id}`
- `PATCH /api/v1/chores/{chore_id}`
- `POST /api/v1/chores/{chore_id}/draft`
- `POST /api/v1/chores/{chore_id}/ready`
- `POST /api/v1/chores/{chore_id}/archive`
- `POST /api/v1/chores/{chore_id}/run`
- `POST /api/v1/chores/{chore_id}/cancel`
- `GET /api/v1/chores/archived`
- `GET /api/v1/chores/archived/{chore_id}`
- `PATCH /api/v1/chores/archived/{chore_id}`
- `DELETE /api/v1/chores/archived/{chore_id}`

`GET /api/v1/jobs` returns tasks and chores in one response. Use `kind=task` or
`kind=chore` to filter the unified list. `GET /api/v1/tasks` and
`GET /api/v1/chores` return the same projections split by kind.
The unified `/jobs` collection is read-only; mutations use the concrete
`/tasks` or `/chores` collection selected by the job kind.

List endpoints return authored job fields at the top level and runtime-derived
status under `runtime`.

Task items include:

- `id`
- `kind`
- `stage`
- `status`
- `title`
- `path`
- `updated_at`
- `runtime`

Chore items include:

- `id`
- `kind`
- `stage`
- `status`
- `schedule`
- `title`
- `path`
- `updated_at`
- `runtime`

`stage` values are:

- `ready`
- `draft`
- `archived`

Task status values are:

- `todo`
- `running`
- `done`
- `failed`
- `canceled`

Chore status values are:

- `todo`
- `running`
- `done`

`runtime` contains:

- `thread_id`
- `last_run`
- `next_run_at`

`last_run` is the latest run object or `null`. If `last_run.status` is
`running`, that run is the active run. `next_run_at` is the next scheduled
chore timestamp or `null`.

Default job list endpoints return ready jobs. Draft and archived jobs are
available only through explicit `/archived` routes.

Detail endpoints return the same item shape plus `body`.

Collection endpoints return JSON arrays directly, and detail or mutation
endpoints return the projected resource directly. Destructive cap and archived
job deletion endpoints return `204 No Content`.

Task create requests accept:

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks."
}
```

Task patch requests accept any subset of `title` and `body`. Stage actions
use the task `draft`, `ready`, and `archive` endpoints. `task reopen` sets a
completed, failed, or canceled task back to scheduler status `todo`.
Delete is destructive and is available only through archived routes.

Chore create requests accept:

```json
{
  "title": "Check stale PRs",
  "body": "Check stale pull requests and summarize blockers.",
  "schedule": "FREQ=HOURLY;INTERVAL=6"
}
```

Chore patch requests accept any subset of `title`, `body`, and `schedule`.
Stage actions use the chore `draft`, `ready`, and `archive` endpoints.
`chore run` starts one manual occurrence without changing the schedule.
Delete is destructive and is available only through archived routes.


## Run And Thread Endpoints

- `POST /api/v1/runs/stream`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/stream`
- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`
- `POST /api/v1/threads`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`
- `GET /api/v1/threads/{thread_id}/stream`

`/api/v1/runs/{run_id}` is the main trace-detail endpoint.

Run collections return `RunInfo` arrays directly. `RunInfo` combines run
identity, status, input text, output summary, failure, and timestamps; there is
no separate `RunSummary` response type.

`steer` and `cancel` operate on running runs. Thread `rewind` and `fork` request
bodies take an optional `run_id` anchor and `request_id`. An omitted run id
selects the last visible run. Task and chore threads cannot be rewound or forked
because their thread ids are derived from job ids.

`steer` and `cancel` return the accepted `RunControlInfo`. An accepted manual
chore start returns its `RunInfo`. Thread create and fork return the created
thread; rewind returns the updated existing thread representation. None of
these thread operations starts a follow-up run.

Run and thread streams expose only live events; Toolang does not persist an
exact event log or provide a historical `/events` collection. A reconnecting
client first reads run or thread detail from durable records, then observes new
events from the live stream.

Streams use SSE framing directly: the SSE `event` field is the canonical event
type, and `data` is that event's serialized payload. The API does not wrap a
`RunEvent` or `ThreadEvent` in a second transport event type.

Canonical run progress event names are:

- `run_begin`
- `step_begin`
- `part_begin`
- `part_delta`
- `part_end`
- `step_end`
- `run_end`

Run control acceptance and status are durable `RunControlRecord` truth, not
synthetic stream events. A thread stream may additionally carry
`thread_created`, `thread_forked`, and `thread_rewound`, and aggregates live run
events belonging to that thread.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
