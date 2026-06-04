# Control Surfaces

This document defines the public CLI and local agent HTTP API.


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
PY_LOG=toolang.run=info toolang ./examples/invoke-playground.too summarize "Summarize this workspace"
toolang ./examples/invoke-playground.too --help
toolang ./examples/invoke-playground.too summarize "Summarize this workspace"
toolang run alice
toolang run brice/alice
toolang run https://toolang.ai/alice.too
toolang clone brice/alice
toolang start alice
toolang stop alice
toolang info alice
toolang alice chat "What changed today?"
toolang alice chat tui_3nprht9x "Continue"
toolang alice chat tui_3nprht9x --ui
toolang alice threads
toolang alice runs --thread tui_3nprht9x
toolang alice steer run_ppkp9e94 "Use the smaller patch"
toolang alice cancel tui_3nprht9x
toolang alice rewind run_ppkp9e94 "Try again from here"
toolang alice fork run_ppkp9e94 "Explore a different approach"
toolang model list
```


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
| Roaming | Local `.too` path invocation | No HTTP runtime port; the thunk is invoked directly |


## Invoke Surface

Roaming invoke uses one local `.too` source path directly:

```bash
toolang SCRIPT THUNK [OPTIONS] [PARAMS] [INPUT]...
```

Arguments:

- `SCRIPT` is the local Toolang script or agent file
- `THUNK` is the thunk to invoke
- `PARAMS` are named thunk parameters, written as `NAME=VALUE`
- `INPUT` values are assembled into one multimodal message

Behavior:

- one local `.too` path enters roaming invoke mode
- stdout is reserved for the final thunk result
- progress messages are written to stderr only when stderr is a TTY
- `-q` or `--quiet` suppresses progress messages
- `PY_LOG=toolang.run=info toolang a.too thunk ...` writes runtime logs under `.toolang/agents/<agent>/.runtime/logs/<thunk>/<run_id>.log`
- `PY_LOG=debug toolang a.too thunk ...` also writes lower-level provider and HTTP logs to that run log file
- `toolang a.too --help` lists invokable thunks
- `toolang a.too thunk --help` prints thunk-specific dynamic usage
- `toolang a.too` shows usage instead of invoking a default thunk
- roaming invoke exposes the agent's effective tools, subject to thunk tool directives
- `NAME=VALUE` sets one thunk named param when `NAME` matches the thunk signature
- `INPUT` rules:
  - `TEXT` adds one text part; use `@@TEXT` for literal text beginning with `@`
  - `@PATH` adds one path-based part; `.txt` and `.md` paths become text parts
  - image extensions such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, and `.svg` infer image parts
  - audio extensions such as `.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, and `.flac` infer audio parts
  - all other path extensions infer generic file parts
- `--` ends option parsing so later arguments stay `INPUT` values
- `--option` is reserved for Toolang runtime options
- `PY_LOG` uses env_logger-style directive formatting and does not affect stdout
- key execution events are recorded in `runs.db` for script runs just like chat,
  task, and chore runs


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
background `toolang run` command.

Agent entrypoints also share one logging policy resolver:

| Entrypoint | Log destination |
| --- | --- |
| `toolang run` | `stderr` |
| `toolang start` | `agent_log` under the agent `.runtime` directory |
| local `.too` invoke | `run_log` under the agent `.runtime` directory when `PY_LOG` is set, otherwise `none` |

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

Core endpoints are grouped as:

- `agent`
- `chat`
- `caps`
- `jobs`
- `activity`
- `hook`


## Agent Endpoints

- `GET /healthz`
- `GET /api/v1/profile`

`/api/v1/profile` returns:

- profile metadata
- environment summary
- overview metrics:

| Metric Group | Contents |
| --- | --- |
| `threads` | Thread totals grouped by chat, chore, and task |
| `steps` | Step totals grouped by `model_call`, `tool_call`, and `runtime` |
| `tokens` | Aggregated input, output, and total token usage |


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

- `GET /api/v1/chat/models`
- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`

`GET /api/v1/chat/models` returns the effective selectable model selectors for
chat runs after applying the current activation config and the `chat` thunk's
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

Chat request body uses:

- `thread`
- `client`: `web`, `tui`, or `chat`; defaults to `web` and controls the prefix
  for newly allocated chat thread ids
- `peer` optional thread peer descriptor
  - `type`: `user` or `agent`; defaults to `user`
  - `name`: peer name; defaults to `user`
  - `thread`: peer-local thread id; defaults to `null`
- `message`
  - `role`
  - `parts`
- `model` optional selected model selector for this run

`message.parts` accepts canonical message parts such as:

- `text`
- `image`
- `audio`
- `file`

Actual part support still depends on the selected model route. For example, the
built-in OpenAI `responses` routes currently accept text, image, and file
inputs, but not audio inputs.

For multipart payload details:

- `image.image_url` may be a remote URL or a local `data:` URL
- `audio.data` should be base64 payload; `audio.data_url` is also accepted as an alias and is normalized to base64
- `file.file_url` is for remote files
- `file.file_data` should carry the provider-facing file payload and may be a full `data:...;base64,...` URL
- `file.data_url` is also accepted as an alias and is normalized to `file_data`

`POST /api/v1/chat` returns one completed user/assistant pair.

`POST /api/v1/chat/stream` returns one SSE stream that follows an AI SDK UI
message stream subset.

The CLI command for chat-style input is `toolang <agent> chat [thread] [message]`.
Without a thread id, the CLI creates a terminal chat thread. With a thread id,
it continues or opens that thread. Job thread ids are inspectable and
controllable through thread and run commands, but `chat` does not implicitly
reopen tasks or create manual chore runs.


## Job Endpoints

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `PATCH /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/archived`
- `GET /api/v1/jobs/archived/{job_id}`
- `DELETE /api/v1/jobs/archived/{job_id}`
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
- `DELETE /api/v1/chores/archived/{chore_id}`
- `GET /api/v1/will`

`GET /api/v1/jobs` returns tasks and chores in one response. Use `kind=task` or
`kind=chore` to filter the unified list. `GET /api/v1/tasks` and
`GET /api/v1/chores` return the same projections split by kind.

List endpoints return authored job fields at the top level and runtime-derived
status under `runtime`.

Task items include:

- `id`
- `kind`
- `lifecycle`
- `status`
- `title`
- `path`
- `updated_at`
- `runtime`

Chore items include:

- `id`
- `kind`
- `lifecycle`
- `status`
- `schedule`
- `title`
- `path`
- `updated_at`
- `runtime`

`lifecycle` values are:

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
- `next_run`

`last_run` is the latest run object or `null`. If `last_run.status` is
`running`, that run is the active run. `next_run` is the next scheduled chore
run or `null`.

Default job list endpoints return ready jobs. Draft and archived jobs are
available only through explicit `/archived` routes.

Detail endpoints return the same item shape plus `body`.

Task create requests accept:

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks."
}
```

Task patch requests accept any subset of `title` and `body`. Lifecycle actions
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
Lifecycle actions use the chore `draft`, `ready`, and `archive` endpoints.
`chore run` starts one manual occurrence without changing the schedule.
Delete is destructive and is available only through archived routes.


## Activity Endpoints

- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`
- `POST /api/v1/runs/{run_id}/rewind`
- `POST /api/v1/runs/{run_id}/fork`
- `GET /api/v1/instruct/{hash}`
- `GET /api/v1/context/{hash}`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `GET /api/v1/events`
- `GET /api/v1/events/stream`

`/api/v1/runs/{run_id}` is the main trace-detail endpoint.

`steer` and `cancel` operate on running runs. `rewind` and `fork` operate on
branchable chat threads by taking a run id as the anchor; task and chore threads
cannot be rewound or forked because their thread ids are derived from job ids.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
