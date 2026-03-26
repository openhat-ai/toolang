# Toolang Control Surfaces

This document defines the user-facing control surfaces for Toolang:

- the CLI
- the known-agent registry
- the per-agent HTTP API
- the shared bus HTTP API

Canonical ordered chat-message semantics live in [chat.md](./chat.md).


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
- `invoke` is caller-driven one-shot foreground execution
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

Toolang stores global agent registry state in:

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
  - `agent_started`
  - `agent_stopped`
  - `agent_updated`
- run lifecycle
  - `run_started`
  - `run_finished`
  - `run_failed`

Rules:

- `run` and `start` publish agent lifecycle events
- `invoke` and server-side run requests publish top-level run events
- event records use a monotonic `event_id`


## 4. Agent API

Each started agent exposes a local HTTP API for direct UI or client use.

Endpoints:

- `GET /healthz`
- `GET /api/v1/health`
- `GET /api/v1/agent`
- `GET /api/v1/profile`
- `GET /api/v1/runtime`
- `GET /api/v1/caps`
- `GET /api/v1/tasks`
- `PUT /api/v1/tasks/{task_id}`
- `PATCH /api/v1/tasks/{task_id}`
- `GET /api/v1/chores`
- `GET /api/v1/will`
- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`
- `GET /api/v1/chats`
- `GET /api/v1/chats/{thread_id}`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/events`
- `GET /api/v1/events/stream`
- `POST /api/v1/runs`

Responsibility summary:

- `/api/v1/agent`
  - current agent identity and running state
- `/api/v1/profile`
  - UI-facing name, handle, and profile metadata
- `/api/v1/runtime`
  - current runtime environment and trust context
- `/api/v1/caps`
  - synced capability metadata visible to the running agent
- `/api/v1/tasks`
  - local durable task documents under the agent room, plus latest pulse
    scheduler feedback when available
- `/api/v1/tasks/{task_id}`
  - create or update one local task document directly through the agent API
- `/api/v1/chores`
  - local recurring chore documents under the agent room, plus latest pulse
    scheduler feedback when available
- `/api/v1/will`
  - the local will document, if present, plus latest pulse scheduler feedback
- `/api/v1/chat*`
  - durable thread-based chat turns
  - `/api/v1/chat/stream` emits SSE events for streamed text and tool-call
    input/output chunks:
    - `text-start`
    - `text-delta`
    - `text-end`
    - `tool-input-start`
    - `tool-input-delta`
    - `tool-input-available`
    - `tool-output-available`
    - uses AI SDK-compatible camelCase fields such as `toolCallId`,
      `toolName`, and `inputTextDelta`
    - `finish`
- `/api/v1/chats`
  - thread summaries with stable server-provided `title`, `preview`, and
    `channel`
- `/api/v1/chats/{thread_id}`
  - one canonical ordered transcript:
    - `thread`
    - `messages[]`
  - each message contains ordered `parts[]`, so assistant text and tool parts
    do not need client-side reordering
- `/api/v1/runs*`
  - current and historical runs for this agent
- `/api/v1/events*`
  - ordered agent-scoped event stream


## 5. Bus API

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
