# Control Surfaces

This document defines the public CLI and local agent HTTP API.


## CLI

The CLI entry points are:

- `toolang`
- `too`

Top-level commands are:

- `new`
- `clone`
- `remove`
- `list`
- `info`
- `run`
- `start`
- `stop`
- `task`
- `chore`
- `skill`
- `psyche`
- `service`
- `prompt`

Typical usage:

```bash
toolang new alice
toolang list
toolang ./examples/invoke-playground.too --help
toolang ./examples/invoke-playground.too summarize "Summarize this workspace"
toolang run alice
toolang run brice/alice
toolang run https://toolang.ai/alice.too
toolang clone brice/alice
toolang start alice
toolang stop alice
toolang info alice
```


## Agent Selectors

Runtime commands accept these selector forms:

| Form | Meaning |
| --- | --- |
| `name` | A local managed agent such as `alice` |
| `shorthand` | A convention-based remote selector such as `brice/alice` or `toolang.ai/alice` |
| `ref` | A canonical remote ref such as `github://brice/agents/alice.too@main` or `https://toolang.ai/alice.too` |

Current shorthand expansion rules are:

| Shorthand | Expanded ref |
| --- | --- |
| `owner/name` | `github://owner/agents/name.too` |
| `host/name` | `https://host/name.too` |

GitHub refs may add one revision suffix:

- `github://owner/repo/path/to/agent.too@rev`

`rev` is one git revision token. Toolang does not distinguish branch, tag, and
commit in selector syntax.


## Invoke Surface

Roaming invoke uses one local `.too` source path directly:

```bash
toolang path/to/agent.too THUNK [OPTIONS] [PARAMS] [PARTS]
```

Behavior:

- one local `.too` path enters roaming invoke mode
- `toolang a.too --help` lists invokable thunks
- `toolang a.too thunk --help` prints thunk-specific dynamic usage
- `toolang a.too` shows usage instead of invoking a default thunk
- bare arguments become message parts
- `NAME=VALUE` sets one thunk named param when `NAME` matches the thunk signature
- `PART` rules:
  - `TEXT` adds one text part; use `@@TEXT` for literal text beginning with `@`
  - `@PATH` adds one path-based part; `.txt` and `.md` paths become text parts
  - image extensions such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, and `.svg` infer image parts
  - audio extensions such as `.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, and `.flac` infer audio parts
  - all other path extensions infer generic file parts
- `--` ends option parsing so later arguments stay message parts
- `--option` is reserved for Toolang runtime options


## Runtime Commands

| Command | `name` | `shorthand` | `ref` |
| --- | --- | --- | --- |
| `toolang run` | yes | yes | yes |
| `toolang clone` | yes | yes | yes |
| `toolang start` | yes | no | no |

Behavior:

| Command | Behavior |
| --- | --- |
| `toolang run` | Runs a local agent, or fetches one remote agent program into a temporary local home and runs it in the foreground |
| `toolang clone` | Clones one local agent, or fetches one remote agent program into a new local managed agent |
| `toolang start` | Starts one local managed agent only. Remote selectors must be cloned first |


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

Write:

- `PUT /api/v1/psyches/{name}`
- `PUT /api/v1/skills/{name}`
- `PUT /api/v1/services/{name}`
- `PUT /api/v1/prompts/{name}`
- `DELETE /api/v1/psyches/{name}`
- `DELETE /api/v1/skills/{name}`
- `DELETE /api/v1/services/{name}`
- `DELETE /api/v1/prompts/{name}`


## Chat Endpoints

- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`

`POST /api/v1/chat` returns one completed user/assistant pair.

`POST /api/v1/chat/stream` returns one SSE stream that follows an AI SDK UI
message stream subset.


## Job Endpoints

- `GET /api/v1/tasks`
- `GET /api/v1/chores`
- `GET /api/v1/will`


## Activity Endpoints

- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/instructions/{instructions_hash}`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `GET /api/v1/events`
- `GET /api/v1/events/stream`

`/api/v1/runs/{run_id}` is the main trace-detail endpoint.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
