# Toolang Design

Toolang has three layers:

- language
  - `.too` source files
- runtime
  - execution, resolution, state, and process tracking
- CLI
  - the `toolang` command

This document defines the source layout, references, capability model, storage,
CLI surface, and runtime phases.

Detailed filesystem layout and agent identity conventions live in
[layout.md](/Users/bryan/openhat-ai/toolang/docs/layout.md).


## 1. Terms

- `toolang root`
  - the local Toolang system directory
  - default: `~/.toolang`
- `workspace`
  - an external project directory used by an agent
- `agent`
  - one runnable `.too` source file
- `agent home`
  - the local directory that hosts an agent
- `agent room`
  - the private room inside an agent home
- `agent_name`
  - the stem of a `.too` filename
- `agent_uri`
  - the canonical identity string used for stable hashing
- `agent_id`
  - a short stable hash derived from `agent_uri`
- `resident agent`
  - an agent hosted under `${TOOLANG_ROOT}/agents/`
- `roaming agent`
  - an agent hosted at an external local path
- `visiting agent`
  - an agent materialized from a remote URL under `${TOOLANG_ROOT}/guests/`
- `cap_ref`
  - a capability reference used by `skill`, `service`, `prompt`, and `psyche`
    operations
- `agent_ref`
  - an input that resolves to agent source
- `agent selector`
  - a CLI input that may be a source-facing `agent_ref`, an `agent_id`, or an
    `agent_name`


## 2. Naming

- source file extension: `.too`
- main CLI command: `toolang`
- canonical agent URI schemes are `agent://`, `file://`, and `https://`
- an agent home may contain one or more `.too` files
- use descriptive filenames such as `reviewer.too` or `analyst.too`


## 3. Layout

The authoritative filesystem layout is defined in
[layout.md](/Users/bryan/openhat-ai/toolang/docs/layout.md).

Summary:

- `TOOLANG_ROOT` defaults to `~/.toolang`
- `TOOLANG_ROOT/agents/` stores resident agent homes
- `TOOLANG_ROOT/guests/` stores visiting agent homes
- roaming agent homes stay at their original local paths
- every agent home has a private agent room under
  `.toolang/agents/{AGENT}/`
- synced caps live under `.toolang/sync/`
- shared home-local caps live under `.toolang/{psyches,skills,services,prompts}/`


## 4. Global Storage

Global Toolang storage lives under `TOOLANG_ROOT`.

```text
{TOOLANG_ROOT}/
  agents.db
  agents.too                # optional
  agents/{HOME}/
  guests/{HOME}/
  sandbox/{AGENT}/
  bus/
    events.db
    bus.run
    bus.log
```

Responsibilities:

- `agents.db`
  - global registry of known agents and active started agents
- `bus/events.db`
  - shared lifecycle and run-history store
  - written directly by agent processes
  - readable even when no standalone bus server is running
- `agents/{HOME}/`
  - resident agent homes
- `guests/{HOME}/`
  - visiting agent homes
- `sandbox/{AGENT}/`
  - local execution sandboxes
- `bus/`
  - local bus and event state

Rules:

- `TOOLANG_ROOT` is a local placement detail and must not be part of
  `agent_uri`
- resident agent identity stays stable when a Toolang root is copied to a new
  machine or path
- visiting agent homes live under `guests/`, not under a separate hotel layout
- agent processes publish to `bus/events.db` directly, so local history survives
  even if a future bus server is offline

Reason:

- local placement and canonical identity are separate concerns
- resident, roaming, and visiting agents all need a local agent home


## 5. Capability Model

### 5.1 Capability Sources

Toolang currently uses these authored sources:

- `agent`
  - refs or inline definitions in `{AGENT}.too`
- `shared`
  - refs in `${AGENT_HOME}/agents.too`
  - local files or directories in `${AGENT_HOME}/.toolang/{kinds}/`
- `global`
  - refs in `${TOOLANG_ROOT}/agents.too`
  - local files or directories in `${TOOLANG_ROOT}/{kinds}/`

For skills, runtime precedence is:

1. `agent`
2. `shared`
3. `global`

Local skills are kept in the same scope as their authored directory and can
override refs from that same scope without collapsing other scopes at sync time.

### 5.2 Visibility Switches

Runtime visibility is controlled with CSV switches:

- `--caps=<csv>`
  - set visibility for all kinds
- `--skills=<csv>`
  - override visibility for skills
- `--services=<csv>`
  - override visibility for services
- `--prompts=<csv>`
  - override visibility for prompts
- `--psyches=<csv>`
  - override visibility for psyches

Allowed CSV values:

- `agent`
- `shared`
- `global`

Rules:

- omitted switches use `agent,shared,global`
- `--caps` applies to all kinds
- a kind-specific switch overrides `--caps` for that kind
- CSV order does not change precedence

Examples:

- `--caps=agent`
- `--caps=agent,shared`
- `--caps=shared,global`
- `--caps=agent --skills=agent,shared,global`

### 5.3 Capability References

`cap_ref` is kind-neutral. Resolution depends on the capability kind.

Examples:

- `toolang skill add briceyan/pdf-processing`
- `toolang service add by3gus/github`
- `toolang prompt add by3gus/rewrite`
- `toolang psyche add by3gus/reviewer`
- `use skill briceyan/pdf-processing`

The same ref syntax is currently used in:

- `.too` `use` declarations
- `agents.too` shared source files
- CLI commands that manage capability refs

### 5.4 GitHub Resolution

The first supported registry is GitHub.

Bare ref format:

- `user-or-org/cap-name`

Probe rules:

- for `service`
  - repos: `agent-services`, `services`
  - paths: `services/<cap-name>.md`, `<cap-name>.md`
- for `prompt`
  - repos: `agent-prompts`, `prompts`
  - paths: `prompts/<cap-name>.md`, `<cap-name>.md`
- for `psyche`
  - repos: `agent-psyches`, `psyches`
  - paths: `psyches/<cap-name>.md`, `<cap-name>.md`
- for `skill`
  - repos: `agent-skills`, `skills`
  - paths: `skills/<cap-name>/SKILL.md`, `<cap-name>/SKILL.md`

Canonical layout for new GitHub caps:

- service
  - repo: `agent-services`
  - path: `services/<cap-name>.md`
- prompt
  - repo: `agent-prompts`
  - path: `prompts/<cap-name>.md`
- psyche
  - repo: `agent-psyches`
  - path: `psyches/<cap-name>.md`
- skill
  - repo: `agent-skills`
  - path: `skills/<cap-name>/SKILL.md`


## 6. Capability Scopes

Toolang currently uses three capability scopes:

- `agent`
  - authored source: `{AGENT_HOME}/{AGENT}.too`
- `shared`
  - authored source: `{AGENT_HOME}/agents.too`
  - local editable caps: `{AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
- `global`
  - authored source: `{TOOLANG_ROOT}/agents.too`
  - local editable caps: `{TOOLANG_ROOT}/{skills,services,prompts,psyches}/`

Sync keeps the scopes separate and runtime applies precedence in this order:

1. `agent`
2. `shared`
3. `global`

Current scope categories are:

- `ref`
  - declared with `use ...` in `.too` or `agents.too`
- `inline`
  - declared directly in `.too`
- `local`
  - human-edited files under the local cap directories

Current implementation details:

- `agents.too` currently supports shared and global `use` refs for `skill`,
  `service`, `prompt`, and `psyche`
- local cap directories are created lazily when the first local cap is created
  in that scope
- same-name caps in different scopes are intentionally deferred to runtime
  overlay instead of collapsing at sync time


## 7. Per-Agent Sync State

Each top-level `.too` file writes one durable sync state file:

- `${AGENT_HOME}/.toolang/sync/<agent>.state.json`

The state file stores:

- freshness metadata for the whole agent home
- the compiled synced program for that agent
- scope-separated exact cap entries used by that agent:
  - `agent_refs`
  - `shared_refs`
  - `global_refs`

It does not store:

- raw synced cap files
- local authored caps
- agent runtime logs or process state

Minimal shape:

```json
{
  "version": 1,
  "synced_at": "2026-03-18T21:00:00Z",
  "source_file": "reviewer.too",
  "inputs": {
    "agent/reviewer.too": { "mtime_ns": 1742302800000000000, "size": 812 },
    "shared/agents.too": { "mtime_ns": 1742302810000000000, "size": 241 },
    "global/skills/repo-search/SKILL.md": {
      "mtime_ns": 1742302820000000000,
      "size": 112
    }
  },
  "program": {},
  "agent_refs": {
    "skills": {
      "pdf-processing": {
        "ref": "by3gus/pdf-processing",
        "repo": "by3gus/agent-skills",
        "path": "skills/pdf-processing",
        "rev": "8f3c2d4a5b6c7d8e9f00112233445566778899aa"
      }
      }
    },
    "services": {},
    "prompts": {},
    "psyches": {}
  },
  "shared_refs": {
    "skills": {
      "repo-search": {
        "path": "skills/repo-search"
      }
    },
    "services": {
      "github": {
        "ref": "by3gus/github",
        "repo": "by3gus/agent-services",
        "path": "services/github.md",
        "rev": "8f3c2d4a5b6c7d8e9f00112233445566778899aa"
      }
    },
    "prompts": {},
    "psyches": {}
  },
  "global_refs": {
    "skills": {},
    "services": {},
    "prompts": {},
    "psyches": {}
  }
}
```

Rules:

- one state file exists per top-level `.too` file
- the state file is generated and should not be hand-edited
- scope-separated refs let runtime resolve precedence late instead of forcing
  same-name overrides at sync time
- raw synced caps are materialized into separate roots:
  - `${TOOLANG_ROOT}/sync/`
  - `${AGENT_HOME}/.toolang/sync/`
  - `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`


## 8. Sync

`toolang sync` builds generated resolution state from authored inputs.

Inputs:

- top-level `.too` files
- `${AGENT_HOME}/agents.too`
- `${TOOLANG_ROOT}/agents.too`
- local cap directories in shared and global scope

Outputs:

- `${TOOLANG_ROOT}/sync/`
- `${AGENT_HOME}/.toolang/sync/`
- `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`
- one `${AGENT_HOME}/.toolang/sync/<agent>.state.json` per top-level `.too`

Contract:

1. parse top-level `.too` files with Tree-sitter
2. analyze per-agent declarations, `use` statements, and inline caps
3. parse shared and global `agents.too` refs
4. read shared and global local cap directories
5. resolve each ref to an exact immutable target
6. materialize global, shared, and agent cap scopes into separate sync roots
7. write agent inline caps into `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`
8. rewrite `${AGENT_HOME}/.toolang/sync/<agent>.state.json` for each agent

Rules:

- each `${AGENT_HOME}/.toolang/sync/<agent>.state.json` stores the last sync
  time and input fingerprints used for freshness checks
- freshness uses recorded input metadata such as `mtime_ns` and file size for:
  - `.too` files in the agent home
  - shared and global `agents.too`
  - shared and global local cap directories
- `toolang invoke` may skip parse and sync when the current inputs still match
  the stored freshness metadata in
  `${AGENT_HOME}/.toolang/sync/<agent>.state.json`
- if any recorded input changed, if generated sync artifacts are missing, or if
  any expected state file is stale, `toolang invoke` triggers sync before
  execution
- inline caps are still materialized during sync so unchanged agents do not
  need to be reparsed just to recover inline definitions
- same-name caps in different scopes are intentionally allowed to coexist in
  their own sync roots
- runtime chooses the effective cap with precedence `agent > shared > global`
- sync may reuse or refresh data under `TOOLANG_ROOT`

Reason:

- `${TOOLANG_ROOT}/sync/` holds global synced caps
- `${AGENT_HOME}/.toolang/sync/` holds shared synced caps and per-agent state
- `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/` holds agent-scoped synced caps


## 9. References

### 9.1 Agent Selectors

An `agent selector` may be:

- a source-facing `agent_ref`
- an `agent_id`
- an `agent_name`

Examples:

- `alice`
- `alice/bob`
- `guest:alice`
- `bob.too`
- `./bob.too`
- `/path/to/some/dir/bob.too`
- `4f3a20b1c1d9`
- `agent://alice/alice.too`
- `file:///path/to/some/dir/bob.too`
- `https://example.com/reviewer.too`

Resolution rules:

- explicit source selectors resolve directly:
  - `alice` resolves to `agent://alice/alice.too`
  - `<home>/<agent>` resolves to `agent://<home>/<agent>.too`
  - `guest:<name>` resolves through a guest resolver to a real `https://...`
    URI
  - relative local paths normalize to absolute `file://...` URIs
- if the selector is not an explicit source selector, Toolang checks
  `agents.db`:
  1. match `agent_id` by unique prefix
  2. match `agent_name` exactly
  3. fall back to source-selector resolution
- `agent_uri` is the canonical identity and must not include `TOOLANG_ROOT`
- `agent_id` is a short stable hash derived from `agent_uri`
- an explicit source selector resolves to:
  - `agent_uri`
  - `agent_home`
  - `agent_name`

### 9.2 URL Materialization

Visiting agents materialize under `TOOLANG_ROOT/guests/`.

Rules:

- a visiting `https://...` URI creates or reuses a visiting agent home
- visiting agent placement is local and separate from canonical identity
- repeated runs may reuse the visiting agent home unless a refresh is requested
- `guest://...` is not a canonical URI in v1


## 10. Message Model

Toolang uses a single `Message` object as the runtime input for one turn.

Minimal fields:

- `origin`
- `channel`
- `sender`
- `thread_id`
- `text`
- `meta`

`origin` defines why the current turn exists:

- `invoke`
  - a caller-driven one-shot non-interactive execution
- `chat`
  - an interactive conversation turn
- `task`
  - a managed task turn
- `chore`
  - a scheduled or periodic turn
- `will`
  - an agent-local self-directed turn

`channel` defines the transport for chat turns.

Common values:

- `tui`
- `webui`
- `api`
- `telegram`

`sender` defines who sent the message relative to the current agent:

- `owner`
- `peer`
- `guest`
- `self`

Rules:

- `origin == chat` requires a non-null `channel`
- `origin in {invoke, task, chore, will}` requires `channel = null`
- `task`, `chore`, and `will` use `sender = self`
- `invoke` is usually `sender = owner`, but may use `peer` for agent-to-agent
  calls
- `channel` is only for chat transport and must not be used as a generic
  execution mode field
- `serve` and `start` are process surfaces, not message origins

Reason:

- `origin` answers why the turn exists
- `channel` answers how a chat message arrived
- `sender` answers who the agent is responding to

### 10.1 Runtime Loops

Toolang has four long-lived `runtime loops` that generate messages for the
shared turn engine:

- `server`
  - accepts local requests and can generate `invoke` or `chat` turns
- `poll`
  - polls external channels and usually generates `chat` turns
- `hook`
  - reacts to external hooks and usually generates `invoke` turns
- `pulse`
  - emits internal `task`, `chore`, and `will` turns

Rules:

- runtime loops are trigger sources, not message origins
- `toolang invoke` starts no runtime loops
- `toolang serve` starts only the `server` loop
- `toolang start` starts a loop set chosen by agent-kind defaults or an
  explicit `--loops=...` override

Default loop policy:

- resident agent
  - start all configured loops
- visiting agent
  - start `server` by default
  - `poll`, `hook`, and `pulse` require explicit opt-in
- roaming agent
  - start no loops by default
  - every loop requires explicit opt-in

Reason:

- roaming agents are often used for one-shot local work such as scripts,
  Makefiles, and editor actions
- visiting agents should be reachable by default without automatically gaining
  long-lived polling or self-driven behavior
- resident agents are the natural home for fully managed long-running behavior


## 11. CLI

### 11.1 Execution

- `toolang invoke <agent>`
- `toolang serve <agent>`
- `toolang start <agent>`

Rules:

- all execution commands accept an `agent selector`
- `invoke` is caller-driven one-shot foreground execution
- `invoke` checks sync freshness before execution and triggers sync when
  required
- `invoke` updates the known-agent registry but does not create a
  running-agent record
- `serve` runs the `server` loop in the foreground and registers one active
  started process for its `agent_uri`
- `start` launches the selected runtime loop set in the background
- `start` uses the default loop policy for the resolved agent kind unless
  `--loops=...` overrides it
- if `server` is part of the active loop set, `start` also reports the local
  endpoint
- v1 allows at most one active started process per `agent_uri`

Grammar inspection and AST-oriented tooling belong in the sibling grammar
package rather than the Toolang runtime CLI.

### 11.2 Resident Agent Management

- `toolang new <resident_target>`
- `toolang clone <source_agent> <resident_target>`
- `toolang remove <resident_agent>`

Command intent:

- `agent new`
  - create a new resident agent source file under `TOOLANG_ROOT/agents/{HOME}/`
- `agent clone`
  - copy the source `.too` file from an existing agent into a new resident
    target
- `agent remove`
  - remove a resident agent source file and its agent-local generated state

Resident targets use resident shorthand or canonical resident URIs:

- `alice`
  - `${TOOLANG_ROOT}/agents/alice/alice.too`
- `team/reviewer`
  - `${TOOLANG_ROOT}/agents/team/reviewer.too`
- `agent://team/reviewer.too`
  - `${TOOLANG_ROOT}/agents/team/reviewer.too`

### 11.3 Capability Management

- `toolang skill add <cap_ref> --scope=agent|shared|global`
- `toolang skill remove <name> --scope=agent|shared|global`
- `toolang skill local new <name> --scope=shared|global`
- `toolang skill local new <name> --from <cap_ref> --scope=shared|global`
- `toolang skill local path <name> --scope=shared|global`
- `toolang skill local delete <name> --scope=shared|global`
- `toolang service add <cap_ref> --scope=agent|shared|global`
- `toolang service remove <name> --scope=agent|shared|global`
- `toolang service local new <name> --scope=shared|global`
- `toolang service local new <name> --from <cap_ref> --scope=shared|global`
- `toolang service local path <name> --scope=shared|global`
- `toolang service local delete <name> --scope=shared|global`
- `toolang prompt add <cap_ref> --scope=agent|shared|global`
- `toolang prompt remove <name> --scope=agent|shared|global`
- `toolang prompt local new <name> --scope=shared|global`
- `toolang prompt local new <name> --from <cap_ref> --scope=shared|global`
- `toolang prompt local path <name> --scope=shared|global`
- `toolang prompt local delete <name> --scope=shared|global`
- `toolang psyche add <cap_ref> --scope=agent|shared|global`
- `toolang psyche remove <name> --scope=agent|shared|global`
- `toolang psyche local new <name> --scope=shared|global`
- `toolang psyche local new <name> --from <cap_ref> --scope=shared|global`
- `toolang psyche local path <name> --scope=shared|global`
- `toolang psyche local delete <name> --scope=shared|global`
- `toolang sync`

Command intent:

- `add`
  - add a capability ref to the selected authored source scope
- `remove`
  - remove a capability ref by name from the selected authored source scope
- `local new`
  - create a local editable cap
- `local new --from`
  - initialize a local editable cap from a remote ref
- `local path`
  - print the target local cap path without creating directories
- `local delete`
  - remove a local editable cap
- `sync`
  - rebuild:
    - `${TOOLANG_ROOT}/sync/`
    - `${AGENT_HOME}/.toolang/sync/`
    - `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`
    - `${AGENT_HOME}/.toolang/sync/<agent>.state.json`

### 11.4 Agent Registry And Running-Agent Commands

- `toolang list`
- `toolang ps`
- `toolang inspect <agent>`
- `toolang logs <agent>`
- `toolang stop <agent>`


## 12. Agent State

Each agent has a private room under `.toolang/agents/{AGENT}/` inside its agent
home.

Typical contents:

- `agent.run`
- `agent.log`
- `sandbox/`
- `tasks/`
- `chores/`
- `will.md`

Rules:

- an agent room is private to one agent
- the agent room stores agent-local runtime state
- the agent room lives under `.toolang/` because it is machine-managed

`will.md` stores durable agent-local intent and working state.

Toolang tracks agents in `~/.toolang/agents.db`.

The database has two logical tables:

- `agents`
  - known agent registry
  - keyed by `agent_uri`
- `running_agents`
  - active started agents
  - keyed by `agent_uri`

Known-agent records include:

- `agent_uri`
- `agent_id`
- `agent_name`
- `agent_home`
- `source_file`
- `updated_at`

Running-agent records include:

- `agent_uri`
- `loops`
- `pid`
- `status`
- `started_at`
- `heartbeat_at`
- `endpoint` when applicable

Rules:

- `invoke` may add or refresh an `agents` record
- only `serve` and `start` create `running_agents` records
- `agent.run` in the agent room mirrors the current running state and active
  loop set for one agent
- `agent.log` stores the managed runtime log for one agent
- `bus/events.db` stores the shared durable event stream for agent lifecycle and
  run history

Bus event families:

- agent lifecycle
  - `agent_started`
  - `agent_stopped`
  - `agent_updated`
- run lifecycle
  - `run_started`
  - `run_finished`
  - `run_failed`

Rules:

- `serve` and `start` publish agent lifecycle events
- `invoke` and server-side `POST /api/v1/runs` publish top-level run events
- `sync` may publish `agent_updated`
- event records use a monotonic `event_id` so a later bus server or web UI can
  backfill and resume from a cursor


## 13. Agent API

Each started agent exposes a local HTTP API for direct web UI integration.

Current endpoints:

- `GET /healthz`
- `GET /api/v1/health`
- `GET /api/v1/agent`
- `GET /api/v1/profile`
- `GET /api/v1/runtime`
- `GET /api/v1/caps`
- `POST /api/v1/chat`
- `POST /api/v1/chat/stream`
- `GET /api/v1/chats`
- `GET /api/v1/chats/{thread_id}`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/events`
- `GET /api/v1/events/stream`
- `POST /api/v1/runs`

Responsibilities:

- `/api/v1/agent`
  - basic agent identity and current started state
- `/api/v1/profile`
  - basic agent profile for UI identity and labeling
- `/api/v1/runtime`
  - current runtime environment and trust context for the started agent
- `/api/v1/caps`
  - synced capability metadata for the current agent
- `/api/v1/chat`
  - one full chat turn with durable transcript storage
- `/api/v1/chat/stream`
  - text-streaming chat surface for Web UI
- `/api/v1/chats`
  - list chat threads for this agent
- `/api/v1/chats/{thread_id}`
  - list recent turns and messages for one thread
- `/api/v1/runs`
  - current and historical top-level runs for this agent
- `/api/v1/runs/{run_id}`
  - run detail with related events and chat turn, when available
- `/api/v1/events`
  - ordered agent-scoped event stream from `bus/events.db`
- `/api/v1/events/stream`
  - SSE event feed for incremental UI updates

Reason:

- a direct single-agent endpoint lets Web UI work before the multi-agent bus
  server is involved
- the same runtime event model can also be projected through a central bus API


## 14. Bus API

Toolang also exposes a root-level bus HTTP API over `bus/events.db`.

Current endpoints:

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

- list known and active agents from the shared bus projection
- list global or per-agent runs and events
- proxy chat requests to active agent endpoints
- provide one local endpoint for multi-agent Web UI integration

Reason:

- agent processes already publish durable events directly to `bus/events.db`
- a standalone bus server can stay thin and only project/query shared state


## 15. Runtime Flow

Primary runtime flow:

1. `parse`
2. `sync`
3. `invoke`

Fast path:

- if `${AGENT_HOME}/.toolang/sync/<agent>.state.json` is still valid, runtime
  skips parse and sync and invokes from the existing synced artifacts

Definitions:

- `parse`
  - use Tree-sitter to read `.too` source into structured syntax data
- `sync`
  - build durable generated state for one agent home
- `invoke`
  - load synced state from `${AGENT_HOME}/.toolang/sync/`, assemble runtime
    inputs, and execute one non-interactive turn

Internal sync steps:

- `analyze`
  - semantic checks
- `resolve`
  - dependency and capability resolution
- `compile`
  - convert agent, shared, and global inputs into per-agent synced records
- `materialize`
  - update `${AGENT_HOME}/.toolang/sync/` and
    `${AGENT_HOME}/.toolang/sync/<agent>.state.json`

Foreground and background execution build on the same prepared agent:

- `invoke`
  - prepare one synced agent and execute a thunk once
- `server`
  - accept local requests and feed the shared turn engine
- `poll`
  - read external channels and feed the shared turn engine
- `hook`
  - react to external hooks and feed the shared turn engine
- `pulse`
  - emit `task`, `chore`, and `will` turns for the shared turn engine
- `serve`
  - prepare one synced agent and run the `server` loop only
- `start`
  - spawn the selected runtime loop set as a background process and wait for
    registration
