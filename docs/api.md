# Toolang Control Surfaces

This document defines the user-facing control surfaces for Toolang:

- the CLI
- the known-agent registry
- the per-agent HTTP API
- the shared bus HTTP API

Canonical lifecycle and runtime-resource vocabulary lives in
[model.md](./model.md).
Canonical chat and message semantics live in [chat.md](./chat.md).


## 1. CLI Surface

Top-level command groups:

- agent lifecycle
  - `new`
  - `clone`
  - `remove`
  - `list`
- state materialization
  - `sync`
- execution
  - `invoke`
  - `run`
  - `start`
- capability management
  - `skill`
  - `service`
  - `prompt`
  - `psyche`
- shared bus
  - `bus serve`

Hidden helper commands:

- `home`
- `source`
- `room`
- `init`

Rules:

- all execution commands accept an `agent selector`
- root-level defaults may be configured in `${TOOLANG_ROOT}/config.toml`
- `invoke` is a caller-driven foreground run
- `run` runs the `server` runtime loop in the foreground and may enable extra
  loops with repeated `--loop` flags
- `start` launches the selected runtime-loop set in the background
- current `run` and `start` surfaces always include the `server` loop because
  the long-lived runtime process is still hosted by the per-agent FastAPI app
- `start` defaults to `server,poll,pulse`
- `run` defaults to `server,pulse`
- grammar inspection belongs in the sibling grammar package, not in the
  Toolang runtime CLI


## 2. Known-Agent Registry

Toolang stores global registry state in:

- `${TOOLANG_ROOT}/agents.db`

Logical tables:

- `agents`
  - known agents keyed by `agent_uri`
- `running_agents`
  - active started agents keyed by `agent_uri`

Known-agent fields include:

- `agent_uri`
- `agent_id`
- `agent_name`
- `agent_home`
- `source_file`
- `updated_at`

Running-agent fields include:

- `agent_uri`
- `pid`
- `status`
- `sandbox`
- `started_at`
- `heartbeat_at`
- `endpoint`

Rules:

- `invoke` may add or refresh a known-agent record
- only `run` and `start` create running-agent records
- at most one active started process may exist per `agent_uri`


## 3. Shared Event Projection

Toolang stores shared bus projection state in:

- `${TOOLANG_ROOT}/bus/events.db`

This database is:

- written directly by local agent processes
- readable even when no standalone bus server is running
- a projection, not the only execution truth

Core event families:

- agent lifecycle
  - `agent_created`
  - `agent_removed`
  - `agent_started`
  - `agent_stopped`
- local changes
  - `caps_updated`
  - `code_updated`
  - `config_updated`
  - `task_updated`
  - `chore_updated`
  - `will_updated`
- run lifecycle
  - `run_started`
  - `run_finished`
  - `run_failed`

Rules:

- `run` and `start` publish agent lifecycle events
- `new`, `clone`, and `remove` publish managed-agent lifecycle events
- `agent_started` and `agent_stopped` correspond to activation boundaries
- agent API event feeds are scoped to the current agent incarnation, starting
  at the latest `agent_created`
- `invoke`, chat submissions, and server-side run requests publish top-level
  run events
- event records use a monotonic `event_id`


## 4. Agent API

Each started agent exposes a local HTTP API for direct UI or client use.

Core endpoints:

- `GET /healthz`
- `GET /api/v1/health`
- `GET /api/v1/agent`
- `GET /api/v1/profile`
- `GET /api/v1/runtime`
- `GET /api/v1/runtime/diagnostics`
- `GET /api/v1/diagnostics`
- `GET /api/v1/caps`
- `GET /api/v1/tasks`
- `PUT /api/v1/tasks/{task_name:path}`
- `PATCH /api/v1/tasks/{task_name:path}`
- `GET /api/v1/chores`
- `PUT /api/v1/chores/{chore_id:path}`
- `PATCH /api/v1/chores/{chore_id:path}`
- `GET /api/v1/will`
- `PUT /api/v1/will`
- `PATCH /api/v1/will`
- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `GET /api/v1/chats`
- `GET /api/v1/chats/{thread_id}`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/prompt`
- `POST /api/v1/runs`
- `GET /api/v1/events`
- `GET /api/v1/events/stream`


## 5. Agent API Responsibilities

### 5.1 Lifecycle And Runtime

- `/api/v1/agent`
  - current agent identity and running-state summary
- `/api/v1/profile`
  - UI-facing name, handle, and profile metadata
- `/api/v1/runtime`
  - current runtime environment and trust context
  - includes `activation_id` for the current online activation
- `/api/v1/runtime/diagnostics`
  - current scheduler, channel, and runtime diagnostics snapshot
- `/api/v1/caps`
  - effective capability metadata visible to the current activation
  - uses `services`, not `servers`
  - current response lists:
    - `psyches`
    - `skills`
    - `services`
    - `counts`

### 5.2 Definition Endpoints

Definition endpoints return authored state only. They do not expose pulse
runtime feedback such as latest run timestamps or latest run status.

- `/api/v1/tasks`
  - local durable task documents under the agent room
  - `TaskItem.status` is task definition status, not run status
- `/api/v1/tasks/{task_name:path}`
  - create or update one local task document directly through the agent API

Current task response fields include:

- `id`
- `name`
- `body`
- `status`
- `requester`
- `mirrored`
- `provider`
- `remote_ref`
- `thread_id`
- `path`
- `updated_at`
- `paused`

- `/api/v1/chores`
  - local recurring chore definitions
  - current summary shape:
    - `id`
    - `title`
    - `rrule`
    - `paused`
- `/api/v1/chores/{chore_id:path}`
  - create or update one local chore definition
  - write requests accept:
    - `title`
    - `body`
    - `rrule`
    - `paused`

- `/api/v1/will`
  - the local will definition, if present
  - current summary shape:
    - `id`
    - `title`
    - `rrule`
    - `paused`
- `/api/v1/will`
  - write requests accept:
    - `title`
    - `body`
    - `rrule`
    - `paused`

Rules:

- tasks, chores, and will are authored definitions, not runtime history
- chore and will scheduling is RRULE-driven
- create or update may trigger immediate local scheduling, but the definition
  endpoints still return definition data only
- clients should query `/api/v1/runs` for execution history or current runtime
  status

### 5.3 Chat And Threads

- `POST /api/v1/chat`
  - submit one chat message
  - creates one run in one thread
  - returns:
    - `thread_id`
    - `run_id`
    - `message`
    - `assistant`
- `POST /api/v1/chat/stream`
  - streamed version of the same operation
  - emits AI SDK-compatible SSE chunks such as:
    - `text-start`
    - `text-delta`
    - `text-end`
    - `tool-input-start`
    - `tool-input-delta`
    - `tool-input-available`
    - `tool-output-available`
    - `tool-output-error`
    - `finish`
    - `error`

- `GET /api/v1/threads`
  - list thread summaries
  - accepts:
    - `kind`
    - `limit`
- `GET /api/v1/threads/{thread_id}`
  - one detailed thread view:
    - `thread`
    - `runs[]`
    - `messages[]`

Compatibility aliases:

- `GET /api/v1/chats`
  - alias for `/api/v1/threads?kind=chat`
- `GET /api/v1/chats/{thread_id}`
  - alias for `/api/v1/threads/{thread_id}`

Rules:

- `title` is a stable thread title
- `preview` is the rolling latest-summary field
- message objects use `run_id`, not `turn_id`
- `/api/v1/chats*` is a chat-oriented alias over the thread model, not a
  separate persistence model

### 5.4 Runs

- `GET /api/v1/runs`
  - current and historical runs for this agent
  - accepts:
    - `origin`
    - `thread_id`
    - `status`
    - `limit`
- `GET /api/v1/runs/{run_id}`
  - one run detail payload:
    - `run`
    - `steps[]`
    - `events[]`
    - `messages[]`
- `GET /api/v1/runs/{run_id}/prompt`
  - prompt trace for one run
- `POST /api/v1/runs`
  - one direct stateless run invocation
  - request shape:
    - `thunk`
    - `input`
    - `model`
  - response shape:
    - `run_id`
    - `output`

Run response fields include:

- `id`
- `origin`
- `thread_id`
- `activation_id`
- `channel`
- `sender`
- `execution_strategy`
- `input_text`
- `output_text`
- `summary`
- `status`
- `type`
- `agent_id`
- `parent_run_id`
- `error`
- `created_at`
- `started_at`
- `finished_at`
- `updated_at`

Rules:

- `run.status` is the runtime status field for one execution
- task, chore, and will runtime history is queried through runs, not through
  definition endpoints
- `origin` values are the built-in runtime sources:
  - `invoke`
  - `chat`
  - `task`
  - `chore`
  - `will`

### 5.5 Events

- `/api/v1/events`
  - ordered agent-scoped event listing
- `/api/v1/events/stream`
  - agent-scoped SSE feed


## 6. Bus API

Toolang also exposes a root-level HTTP API over `bus/events.db`.

Endpoints:

- `GET /healthz`
- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `POST /api/v1/agents/{agent_id}/chat`
- `POST /api/v1/agents/{agent_id}/chat/stream`
- `GET /api/v1/runs`
- `GET /api/v1/events`
- `GET /api/v1/agents/{agent_id}/events`
- `GET /api/v1/events/stream`
- `GET /api/v1/agents/{agent_id}/events/stream`

Responsibilities:

- list known and active agents from the shared projection
- list global or per-agent runs and events
- proxy chat requests to active agent endpoints
- provide one local endpoint for multi-agent Web UI integration


## 7. API Boundary Rules

- lifecycle endpoints describe current agent, incarnation, or activation state
- capability endpoints describe effective visible caps
- definition endpoints describe authored task, chore, and will state
- runtime execution endpoints describe threads, runs, steps, messages, and
  events
- no endpoint should overload one `status` field to mean both definition state
  and runtime state
