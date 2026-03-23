# Toolang Layout

This document defines Toolang filesystem layout and canonical agent identity.


## 1. Core Terms

- `toolang root`
  - the local Toolang system directory
  - default: `~/.toolang`
  - override: `TOOLANG_ROOT`
- `agent home`
  - the local directory that hosts an agent
- `agent room`
  - the private machine-managed area for one agent
- `resident agent`
  - an agent whose home lives under `${TOOLANG_ROOT}/agents/`
- `roaming agent`
  - an agent whose home stays at an external local path
- `visiting agent`
  - an agent discovered from a remote URL and materialized under
    `${TOOLANG_ROOT}/guests/`
- `agent_ref`
  - the raw source-facing selector used to locate an agent
- `agent selector`
  - a CLI selector that may be a source-facing `agent_ref`, an `agent_id`, or
    an `agent_name`
- `agent_uri`
  - the canonical identity string used for stable hashing
- `agent_id`
  - a short stable hash derived from `agent_uri`


## 2. Canonical Agent URIs

Canonical URI forms:

- `agent://<home_name>/<agent_name>.too`
  - resident agent
- `file:///absolute/path/to/<agent_name>.too`
  - roaming agent
- `https://<host>/<path>`
  - visiting agent

Rules:

- `agent_id = hash(agent_uri)`
- canonical identity stays separate from local placement
- `agent_uri` must not depend on the absolute `TOOLANG_ROOT` path
- `guest://...` is not a canonical URI


## 3. Shorthands And Resolution

Examples:

- `alice`
  - `agent://alice/alice.too`
- `alice/bob`
  - `agent://alice/bob.too`
- `guest:alice`
  - resolved to a real `https://...` URI
- `./bob.too`
  - normalized to an absolute `file://...` URI
- `abe.fun/alice`
  - may normalize to `https://abe.fun/alice`

Resolution order for source-facing selectors:

1. canonical URI input
2. `guest:...`
3. local path
4. `<name>`
5. `<home>/<agent>`
6. hosted shorthand such as `<host>/<name>`

Additional rules:

- relative local paths are allowed as input but not as canonical URI
- canonical `file://` URIs always use absolute normalized paths
- explicit source resolution yields both:
  - `agent_uri`
  - `agent_home`
- non-source selectors may also resolve through `agents.db` by:
  - unique `agent_id` prefix
  - exact `agent_name`


## 4. Toolang Root

```text
{TOOLANG_ROOT}/
  agents.db
  agents.too                # optional
  sync/
    skills/
    services/
    prompts/
    psyches/
  skills/
  services/
  prompts/
  psyches/
  agents/{HOME}/
  guests/{HOME}/
  sandbox/{AGENT_KEY}/
  bus/
    events.db
    bus.run
    bus.log
```

Notes:

- `agents.db`
  - known-agent registry and running-agent registry
- `agents.too`
  - optional global shared source
- `sync/`
  - global synced caps
- `{skills,services,prompts,psyches}/`
  - global local caps
- `agents/{HOME}/`
  - resident agent homes
- `guests/{HOME}/`
  - visiting agent homes
- `sandbox/{AGENT_KEY}/`
  - staged sandbox files for started agents
- `bus/events.db`
  - shared durable event projection


## 5. Agent Home

Kinds of agent home:

- resident
  - `${TOOLANG_ROOT}/agents/{HOME}/`
- visiting
  - `${TOOLANG_ROOT}/guests/{HOME}/`
- roaming
  - any external local directory

Layout:

```text
${AGENT_HOME}/
  {AGENT}.too
  agents.too                         # optional
  channels.toml                      # optional
  hooks.toml                         # optional
  .env                               # optional
  .toolang/                          # machine-managed, created lazily
    agents/{AGENT}/
    sync/
      {AGENT}.state.json
      skills/
      services/
      prompts/
      psyches/
    skills/
    services/
    prompts/
    psyches/
```

Notes:

- `{AGENT}.too`
  - runnable source for one agent
- `agents.too`
  - optional shared source for all agents in that home
- `channels.toml`
  - optional channel bindings for runtime loops
- `hooks.toml`
  - optional hook bindings for runtime loops
- `.toolang/sync/`
  - shared synced caps plus per-agent sync state
- `.toolang/{skills,services,prompts,psyches}/`
  - shared local caps
- `.toolang/` is created by runtime-managed commands, not by `new` or `clone`


## 6. Agent Room

Path:

```text
${AGENT_HOME}/.toolang/agents/{AGENT}/
```

Layout:

```text
${AGENT_ROOM}/
  agent.run
  agent.log
  execution.db
  pulse.json
  task_mirrors.json
  runs/
    {RUN_ID}/
      prompt.json
  poll/
  hooks/
  sync/
    skills/
    services/
    prompts/
    psyches/
  chats/
    chats.db
  sandbox/
  tasks/
    *.md
  chores/
    *.md
  will.md
```

Notes:

- `agent.run`
  - current running state for one started agent
- `agent.log`
  - managed runtime log
- `execution.db`
  - local execution truth layer for runs, threads, turns, and steps
- `pulse.json`
  - persisted scheduling state and latest run feedback for local task, chore,
    and will scans
- `task_mirrors.json`
  - remote-task mirror bindings keyed by provider and remote ref
- `runs/{RUN_ID}/prompt.json`
  - prompt-build diagnostics for one turn
- `poll/`
  - runtime-owned poll loop state
- `hooks/`
  - runtime-owned hook loop state
- `sync/`
  - agent-scoped synced caps
- `chats/chats.db`
  - durable chat threads and messages
- `tasks/`, `chores/`, and `will.md`
  - agent-local work state


## 7. Scope Roots

Capability scopes map to these roots:

- `agent`
  - source: `${AGENT_HOME}/{AGENT}.too`
  - sync: `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`
- `shared`
  - source: `${AGENT_HOME}/agents.too`
  - local: `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
  - sync: `${AGENT_HOME}/.toolang/sync/`
- `global`
  - source: `${TOOLANG_ROOT}/agents.too`
  - local: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`
  - sync: `${TOOLANG_ROOT}/sync/`

Scope semantics and runtime precedence are defined in
[capabilities.md](./capabilities.md).


## 8. Sandbox Paths

Supported sandbox specs:

- `host`
- `docker:<image>`

Rules:

- `none` normalizes to `host`
- `toolang run` uses `host` only
- `toolang start` may use `host` or `docker:<image>`
- `${TOOLANG_ROOT}/sandbox/{AGENT_KEY}/`
  - staged docker start files such as `args.json` and `exec.sh`
- `${AGENT_ROOM}/sandbox/`
  - runtime mount point for staged sandbox files
