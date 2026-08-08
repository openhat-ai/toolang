# Control Surfaces

This document defines the public CLI and local agent HTTP API.

Interactive CLI, TUI, and WebUI surfaces resolve `Submission` text according
to their declared submission profile. Chat surfaces may apply `QuickCommand`
and `SettingCommand` results locally; every execution surface turns an
accepted `RunnableCall` into the structured run request defined by
[input-syntax.md](./input-syntax.md).


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
- `retry`
- `rerun`
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
toolang alice info
toolang ./examples/deep_search.too info
toolang alice chat
toolang alice chat term_3nprht9x
toolang alice chat --sandbox docker
toolang alice threads
toolang alice runs --thread term_3nprht9x
toolang alice inspect run_ppkp9e94
toolang alice retry run_ppkp9e94 --limit tokens=200000 --limit time=900
toolang alice rerun run_ppkp9e94 --default model=openai/gpt-5
toolang alice steer run_ppkp9e94 "Use the smaller patch"
toolang alice cancel term_3nprht9x
toolang alice rewind run_ppkp9e94
toolang alice fork run_ppkp9e94
toolang model list
```

Top-level routing uses three command shapes:

- catalog commands are command-first only: `new`, `clone`, `list`, and
  `remove AGENT`
- agent-self commands accept either order: `info`, `run`, `start`, and `stop`
- commands for an agent's threads, runs, caps, tasks, or chores require the
  target first, such as `toolang alice retry RUN` or
  `toolang alice skill list`

A command name wins whenever an unassigned token could be either a command or
a dynamic name. Use `agent:NAME` to force a colliding resident target. After a
local `.too` target, use `agic:NAME`, `flow:NAME`, or `runnable:NAME` to force a
colliding runnable name. A token ending in `.too` selects a local source path
even when that path does not exist; use `agent:NAME` for a resident name ending
in `.too`. Once a command is selected, its remaining operands are parsed by
that command and are not reclassified.

A target without a command shows the commands accepted by that placement.
Plain resident names are recognized from the selected root's agent catalog;
explicit resident selectors and remote selectors are unambiguous. Showing
remote target help does not resolve or fetch the agent. An incomplete selected
command shows its own help before target existence or other runtime validation.

Thread and run listing, inspection, retry, rerun, steering, cancellation,
rewind, and fork open the selected agent's durable execution store directly.
They do not start or call the agent HTTP server. A run id selects its owning
thread; a thread id selects its active run for steering or cancellation and its
latest terminal run for retry, rerun, rewind, or fork. Retry reopens the same
root run and optionally starts at `--anchor`; rerun starts a new root run from
the source invocation. Fork retains the anchor run, while rewind removes it and
the following visible suffix.


## Agent Selectors

Runtime commands accept these selector forms:

| Form | Meaning |
| --- | --- |
| `name` | A local managed agent such as `alice` |
| `agent:name` | An explicit local managed agent, including a name that collides with a command |
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

Script progress, inspection output, and chat TUI activity use the shared
execution presentation language defined in
[execution-presentation.md](./execution-presentation.md). Script mode retains
its stdout/stderr contract; it does not use the TUI renderer.

Arguments:

- `SCRIPT` is the local Toolang script or agent file
- `RUNNABLE` is the uniquely named public agic or flow to run
- `ARGS` provide named runnable parameters, written as `NAME=VALUE`
- `INPUT` values form the content portion of one `Submission`; script mode
  accepts only a resolved `RunnableCall` and uses its evaluated content

Behavior:

- a local `.too` path enters script-run mode
- `agic:NAME`, `flow:NAME`, and `runnable:NAME` explicitly select a runnable
  when its name collides with a top-level command
- default agics and generated internal agics are not exposed as script commands
- runnable command descriptions come only from their authored `doc`
- stdout is reserved for the final runnable result
- progress messages are written to stderr only when stderr is a TTY
- `-q` or `--quiet` suppresses progress messages
- `-v` or `--verbose` shows execution boundaries even when stderr is not a TTY
- `--default model=SELECTOR` supplies the invocation's setup model binding
- `--limit FIELD=VALUE` overrides one run-limit field; it may be repeated
- `--allow DOMAIN=SELECTORS` sets model, tool, cap, or cap-kind allow fields and
  may be repeated
- execution happens directly in the current CLI process; script run does not
  currently accept `--sandbox`
- `PY_LOG=toolang.execution=info toolang a.too summarize ...` writes runtime logs
  under `.toolang/agents/<agent>/.runtime/logs/<runnable>/<run_id>.log`
- `PY_LOG=debug toolang a.too summarize ...` also writes lower-level provider
  and HTTP logs to that run log file
- `toolang a.too --help` lists public runnables
- `toolang a.too summarize --help` prints runnable-specific dynamic usage
- `toolang a.too` shows usage instead of running a default agic
- a runnable missing a required named argument or required primary input shows
  its dynamic help and does not create a run; omitted input is first read from
  stdin when available
- script run reads the complete setup snapshot and resolves effective resources
  inside the executor from `AgentSetup.ceiling`, captured state, and runnable
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

The same roaming source path can select agent commands:

```bash
toolang SCRIPT info
toolang SCRIPT run
toolang SCRIPT chat [THREAD]
toolang SCRIPT threads
toolang SCRIPT runs [--thread THREAD]
toolang SCRIPT inspect TARGET
toolang SCRIPT retry RUN [--anchor STEP]
toolang SCRIPT rerun RUN
```

It also supports `steer`, `cancel`, `rewind`, and `fork`. These command names
immediately following the source are interpreted as agent commands. Prefix a
same-named executable with `agic:`, `flow:`, or `runnable:` to invoke it.

Visiting selectors support the same agent-self and execution-history commands:

```bash
toolang brice/alice info
toolang brice/alice run
toolang brice/alice chat [THREAD]
toolang brice/alice inspect TARGET
toolang brice/alice retry RUN
```

Commands that execute or inspect current program state (`info`, `run`, `chat`,
`retry`, and `rerun`) resolve and materialize the remote program. History-only
commands derive the stable visiting layout and read its existing `runs.db`
without fetching the source.

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

`toolang run` and `toolang start` resolve the same `LaunchSpec` and call the
same hosting lifecycle. A hidden `toolang serve` command is the only
AgentServer process entrypoint. The hosting implementation launches that
entrypoint locally, in Docker, or in another environment; the server and
executor do not branch on hosting.

Both commands accept repeatable `--allow DOMAIN=SELECTORS`,
`--default FIELD=VALUE`, and `--limit FIELD=VALUE` options. The CLI parses these
with `TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*`, and `TOOLANG_LIMIT_*` into frozen
field overrides passed to `SetupWatcher`.

Setup policy uses the following TOML shape in root and agent-home `config.toml`
files:

```toml
[allow]
models = ["gateway/*"]
tools = ["shell/*"]
skills = ["reviewer"]

[default]
model = "gateway/chat"
runnable = "agic:chat"

[limit]
agic_model_calls = 200
agic_tool_calls = "none"
tokens = 200000
cost = "2.50"
time = 900
```

Limit fields are non-negative. Quoted `"none"` disables a limit. Empty allow
arrays deny all resources in that field; `"none"` clears a default binding.
Text CLI/environment values use `none` for an empty allow set, a cleared
binding, or an unlimited limit, according to the target field. `all` removes
an allow restriction. Empty text is always invalid.

The precedence order is built-in values, root config, agent-home config,
runtime environment, CLI, then any request-level binding or limit fields.
Config is re-read dynamically; environment and CLI mappings remain fixed for
the process lifetime. Each `--default` or `--limit` field may occur once;
repeated `--allow` values for the same domain accumulate within the CLI layer.

When `--sandbox` is omitted, resident run/start commands use the effective
root/home `[sandbox]` binding, falling back to `none` when no binding exists.
An explicit selector, including `--sandbox none`, overrides that binding.

For every hosting implementation, AgentServer is the environment's primary
foreground workload. `run` waits for that workload and releases it on exit,
while `start` returns after the health endpoint is ready. `stop` reloads the
persisted `HostingState`, stops the primary workload, and releases its hosting
resources.

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

Pass `--filter` to preview selector filtering, for example
`toolang model list --filter "[remote]"` or
`toolang model list --filter "openai/*[openrouter]"`.

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

The process keeps one `AgentCore`, `CapsManager`, and `JobsManager` for the
application lifetime. `AgentCore` owns the process-local executor, history,
thread manager, setup watcher, and state watcher. These owners are stored on
`app.state` and exposed through small typed request dependencies. The API also
owns one process-local `LiveEventRelay` for live SSE subscribers.
Application-wide FastAPI dependencies are reserved for side-effect-only
concerns such as authentication or common validation. FastAPI lifespan owns
required startup and shutdown; module globals and `ContextVar` do not carry
application state.

`RunExecutor.start()` returns a `RunHandle`; the application retains handles
only when its own protocol needs additional lifecycle bookkeeping.

Core endpoints are grouped as:

- `agent`
- `caps`
- `jobs`
- `runs`
- `threads`

Non-interactive execution uses `POST /api/v1/runs/stream`. It accepts an agic
or flow's unique `runnable` name, primary input, optional model, and optional
declared arguments, and returns the canonical trace event stream for HTTP
clients. An omitted model uses the current `AgentSetup.bindings.model`. CLI
script runs and TUI execution do not consume this endpoint.


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

`GET /api/v1/models` returns the selectable model routes inside the server's
current `AgentSetup.ceiling`. Runnable `models` directives are applied when a
run starts, not by this inspection endpoint. The response includes:

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


## Chat Client Orchestration

The HTTP API has no separate chat submission endpoint. A chat client creates a
thread when needed and then starts each turn through the canonical run stream:

1. `POST /api/v1/threads` with the client and optional peer descriptor.
2. `POST /api/v1/runs/stream` with the returned thread id, runnable, input,
   optional model, and optional runnable arguments.

An existing chat thread can be passed directly to `POST /api/v1/runs/stream`;
the client does not create another thread for every turn. The client selects
the chat/default runnable explicitly rather than relying on a chat-only API
default.

Run `input` accepts canonical percept parts such as:

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

`POST /api/v1/runs/stream` returns the canonical `RunEvent` SSE protocol. A
WebUI that needs another protocol adapts these events client-side; the API does
not maintain a second chat event vocabulary.

The CLI command for interactive chat is `toolang <agent> chat [thread]
[--sandbox <selector>] [--allow DOMAIN=SELECTORS] [--default FIELD=VALUE]
[--limit FIELD=VALUE]`.
Without a thread id, the TUI creates a terminal chat thread on first input. With
a thread id, it continues that thread. The TUI runs in its own process, assembles
the same core objects, calls `RunExecutor` directly, and observes native
`RunEvent` values through a `RunTracer`. It does not depend on the HTTP run
stream. Starting an agent HTTP server remains a separate CLI operation.
Direct chat currently accepts only the `none` sandbox selector; placing the TUI
process inside a hosted sandbox is a separate follow-up.
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

- `pending`
- `running`
- `done`
- `failed`
- `canceled`

Chore status values are:

- `pending`
- `running`
- `done`

`runtime` contains:

- `thread_id`
- `last_run`
- `next_run_at`
- `error`

`last_run` is the latest run object or `null`. If `last_run.status` is
`running`, that run is the active run. `next_run_at` is the next scheduled
chore timestamp or `null`. `runtime.error` is the current scheduler-side error;
`last_run.error` is the execution failure for that run.

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
completed, failed, or canceled task back to scheduler status `pending`.
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
- `POST /api/v1/runs/{run_id}/retry`
- `POST /api/v1/runs/{run_id}/rerun`
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

`RunDetail.output` contains the canonical message parts resolved from the
run's durable output edge. It is `null` until the run has an output edge and
may be an empty array when the resolved runnable result is empty.

`steer` and `cancel` operate on running runs. `retry` and `rerun` accept a
terminal root run. Retry reopens that run from an optional canonical step-path
`anchor`; omitting it selects the latest retryable step. Rerun starts a new root
run from the source invocation and replaces the source in the visible thread
projection. Both accept optional `request_id`, `model`, and partial `limits`,
return `202 Accepted`, and execute on the server's owner event loop.

Thread `rewind` and `fork` request bodies take an optional `run_id` anchor and
`request_id`. An omitted run id selects the last visible run. Task and chore
threads cannot be rewound or forked because their thread ids are derived from
job ids.

`steer` and `cancel` return the accepted `RunControlInfo`. An accepted manual
chore start returns its `RunInfo`. Thread create and fork return the created
thread; rewind returns the updated existing thread representation. None of
these thread operations starts a follow-up run.

`POST /api/v1/runs/stream` accepts:

- `thread`: required existing thread id
- `request_id`: optional globally unique caller-supplied control identifier
- `runnable`: required unique agic or flow name
- `input`: canonical percept-part array
- `model`: optional model selector; omission uses the current setup binding
- `args`: optional runnable argument mapping
- `limits`: optional partial run-limit mapping

HTTP limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`,
and `time`. Omitted fields inherit the latest valid setup snapshot; an explicit
JSON `null` disables that field for the run.

Clients create a thread explicitly with `POST /api/v1/threads` before the first
run. The thread request accepts `web`, `term`, `tui`, `chat`, or `script` as its
client placement; `script` creates a `script_*` thread.

Run and thread streams expose only live events; Toolang does not persist an
exact event log or provide a historical `/events` collection.
`GET /api/v1/runs/{run_id}/stream` accepts only a root run id and carries the
complete recursive run tree. Child runs remain individually inspectable through
their run-detail endpoint. A child-run stream request returns `409` and
identifies the root run to subscribe to.

A reconnecting client establishes and buffers the live stream before reading
run or thread detail from durable records. It then uses the durable detail as
its baseline and applies buffered and subsequent events idempotently. Streams
do not emit SSE ids and ignore `Last-Event-ID`, because the server cannot replay
a precise historical cursor.

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

Every payload retains its canonical `type` discriminator. A `part_begin`
payload uses `part_type` for the message-part kind so it does not collide with
the event discriminator.

Run control acceptance and status are durable `RunControlRecord` truth, not
synthetic stream events. A thread stream may additionally carry
`thread_created`, `thread_forked`, and `thread_rewound`, and aggregates live run
events belonging to that thread.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
