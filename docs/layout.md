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
- `agent ref`
  - the raw source-facing selector used to locate an agent
- `agent selector`
  - a CLI selector that may be a source-facing agent ref, an agent id, or an
    agent name
- `agent uri`
  - the canonical identity string used for stable hashing
- `agent id`
  - a short stable hash derived from `agent uri`


## 2. Canonical Agent URIs

Canonical URI forms:

- `agent://<home_name>/<agent_name>.too`
  - resident agent
- `file:///absolute/path/to/<agent_name>.too`
  - roaming agent
- `https://<host>/<path>`
  - visiting agent

Rules:

- `agent id = hash(agent uri)`
- canonical identity stays separate from local placement
- canonical agent URI must not depend on the absolute `TOOLANG_ROOT` path
- `guest://...` is not a canonical URI


## 3. Shorthands And Resolution

Examples:

- `alice`
  - `agent://alice/alice.too`
- `agent:alice`
  - `agent://alice/alice.too`
- `alice/bob`
  - `agent://alice/bob.too`
- `guest:alice`
  - resolved to one known visiting agent named `alice`
- `roaming:charlie`
  - resolved to one known roaming agent named `charlie`
- `./bob.too`
  - normalized to an absolute `file://...` URI
- `abe.fun/alice`
  - may normalize to `https://abe.fun/alice`

Resolution order for source-facing selectors:

1. canonical URI input
2. `agent:...`
3. local path
4. `<name>`
5. `<home>/<agent>`
6. hosted shorthand such as `<host>/<name>`

Additional rules:

- `guest:<name>` and `roaming:<name>` are CLI selectors for already known
  non-resident agents, not source-facing selectors that synthesize a URI
- relative local paths are allowed as input but not as canonical URI
- canonical `file://` URIs always use absolute normalized paths
- explicit source resolution yields both:
  - agent URI
  - agent home
- non-source selectors may also resolve through `agents.db` by:
  - unique agent-id prefix
  - exact agent name


## 4. Toolang Root

```text
{TOOLANG_ROOT}/
  agents.db
  config.toml               # optional
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
- `config.toml`
  - optional root-level defaults such as Web UI base URL and allowed CORS origins
- `agents.too`
  - optional global source
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
  - optional home-scoped source for all agents in that home
- `channels.toml`
  - optional channel bindings for runtime loops
- `hooks.toml`
  - optional hook bindings for runtime loops
- `.env`
  - optional agent-home env file
  - service capabilities read required env vars from here using the concrete
    env var names declared in service front matter
- `.toolang/sync/`
  - home-scoped synced caps plus per-agent sync state
- `.toolang/{skills,services,prompts,psyches}/`
  - home-scoped local caps
- `.toolang/` is created by runtime-managed commands, not by `new` or `clone`


## 6. Agent Room

Path:

```text
${AGENT_HOME}/.toolang/agents/{AGENT}/
```

Layout:

```text
${AGENT_ROOM}/
  agent.origin.json
  agent.run
  agent.log
  execution.db
  pulse.json
  task_mirrors.json
  service_use/
    mcat/
      {SERVICE}/
        auth.json
        token.json
        session.json
        proxy.json
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
  - local execution truth layer for activations, threads, runs, and steps
- `pulse.json`
  - persisted RRULE scheduling state and latest local scan feedback for task,
    chore, and will definitions
- `task_mirrors.json`
  - remote-task mirror bindings keyed by provider and remote ref
- `service_use/`
  - provider-owned MCP session and proxy state
- `runs/{RUN_ID}/prompt.json`
  - prompt-build diagnostics for one run
- `poll/`
  - runtime-owned poll loop state
- `hooks/`
  - runtime-owned hook loop state
- `sync/`
  - agent-scoped synced caps
- `chats/chats.db`
  - durable chat threads and ordered messages
- `tasks/`, `chores/`, and `will.md`
  - agent-local work state


## 7. Capability Scope Roots

Capability scopes map to these roots:

- `agent`
  - source: `${AGENT_HOME}/{AGENT}.too`
  - sync: `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`
- `home`
  - source: `${AGENT_HOME}/agents.too`
  - local: `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
  - sync: `${AGENT_HOME}/.toolang/sync/`
- `global`
  - source: `${TOOLANG_ROOT}/agents.too`
  - local: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`
  - sync: `${TOOLANG_ROOT}/sync/`

Legacy note:

- some current implementation surfaces still spell the `home` scope as
  `shared`

Scope semantics, visibility, and precedence are defined in
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
