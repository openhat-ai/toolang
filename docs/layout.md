# Toolang Layout

This document defines canonical agent identity and filesystem layout.
Cap semantics live in [caps.md](./caps.md).


## 1. Terms

- `toolang root`
  - local Toolang system directory
  - default: `~/.toolang`
  - override: `TOOLANG_ROOT`
- `agent home`
  - local directory that hosts one or more agent source files
- `agent room`
  - private machine-managed runtime state for one agent
- `resident agent`
  - agent home under `${TOOLANG_ROOT}/agents/`
- `visiting agent`
  - agent home under `${TOOLANG_ROOT}/guests/`
  - source identity is remote
- `roaming agent`
  - agent home at an external local path


## 2. Canonical Agent URI

Canonical URI forms:

- resident
  - `agent://<home>/<agent>.too`
- roaming
  - `file:///absolute/path/to/<agent>.too`
- visiting
  - `https://<host>/<path>`

Rules:

- `agent id = hash(agent uri)`
- canonical identity is independent of `${TOOLANG_ROOT}`


## 3. Source-Facing Agent Selectors

Examples:

- `alice`
  - `agent://alice/alice.too`
- `agent:alice`
  - `agent://alice/alice.too`
- `alice/bob`
  - `agent://alice/bob.too`
- `./bob.too`
  - absolute `file://...`
- `<host>/<name>`
  - visiting `https://...`
- `guest:<name>`
  - one known visiting agent from registry
- `roaming:<name>`
  - one known roaming agent from registry

Resolution order:

1. canonical URI
2. `agent:<name>`
3. local path
4. `<name>`
5. `<home>/<agent>`
6. hosted shorthand


## 4. Toolang Root

```text
${TOOLANG_ROOT}/
  agents.db
  config.toml
  agents.too
  sync/
    defs/
      skills/
        inline/
        remote/
      services/
        inline/
        remote/
      prompts/
        inline/
        remote/
      psyches/
        inline/
        remote/
    index/
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

Key paths:

- `agents.db`
  - agent registry
- `agents.too`
  - global cap source file
- `{skills,services,prompts,psyches}/`
  - global local caps
- `sync/`
  - global materialized inline and remote caps
- `agents/{HOME}/`
  - resident agent homes
- `guests/{HOME}/`
  - visiting agent homes
- `sandbox/{AGENT_KEY}/`
  - staged sandbox files
- `bus/`
  - shared event state


## 5. Agent Home

Kinds:

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
  agents.too
  channels.toml
  hooks.toml
  .env
  .toolang/
    agents/{AGENT}/
    sync/
      defs/
        skills/
          inline/
          remote/
        services/
          inline/
          remote/
        prompts/
          inline/
          remote/
        psyches/
          inline/
          remote/
      index/
        skills/
        services/
        prompts/
        psyches/
      {AGENT}.state.json
    skills/
    services/
    prompts/
    psyches/
```

Key paths:

- `{AGENT}.too`
  - agent source file
- `agents.too`
  - home-scoped cap source file
- `.env`
  - service env vars for the home
- `.toolang/{skills,services,prompts,psyches}/`
  - home-scoped local caps
- `.toolang/sync/`
  - home-scoped materialized inline and remote caps
- `.toolang/`
  - machine-managed state


## 6. Agent Room

Path:

- `${AGENT_HOME}/.toolang/agents/{AGENT}/`

Layout:

```text
${AGENT_ROOM}/
  agent.origin.json
  agent.run
  agent.log
  execution.db
  pulse.json
  task_mirrors.json
  callbacks/
    {CALLBACK_ID}.json
  service_use/
    mcat/
      {SERVICE}/
        connection.json
        callback.json
        token.json
        session.json
  runs/
    {RUN_ID}/
      prompt.json
  poll/
  hooks/
  sync/
    defs/
      skills/
        inline/
        remote/
      services/
        inline/
        remote/
      prompts/
        inline/
        remote/
      psyches/
        inline/
        remote/
    index/
      skills/
      services/
      prompts/
      psyches/
  sandbox/
  tasks/
    *.md
  chores/
    *.md
  will.md
```

Key paths:

- `execution.db`
  - runtime truth for activations, threads, runs, steps, and transcript
- `callbacks/`
  - external callback state
- `service_use/`
  - provider-owned service state
- `runs/{RUN_ID}/prompt.json`
  - prompt trace for one run
- `sync/`
  - agent-scoped materialized inline and remote caps
- `sandbox/`
  - runtime sandbox mount point
- `tasks/`, `chores/`, `will.md`
  - agent-local work state


## 7. Cap Roots

Scope roots:

- `global`
  - source: `${TOOLANG_ROOT}/agents.too`
  - local: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`
  - sync: `${TOOLANG_ROOT}/sync/`
- `home`
  - source: `${AGENT_HOME}/agents.too`
  - local: `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
  - sync: `${AGENT_HOME}/.toolang/sync/`
- `agent`
  - source: `${AGENT_HOME}/{AGENT}.too`
  - sync: `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`

Sync layout:

- `${SCOPE_SYNC_ROOT}/defs/{kind}/{source}/{definition_key}/`
- `${SCOPE_SYNC_ROOT}/index/{kind}/{name}.json`

Rules:

- local caps stay in local roots
- inline and remote caps materialize under `sync/defs/`
- `definition_key` is derived from the canonical locator


## 8. Sandbox

Supported specs:

- `host`
- `docker:<image>`

Rules:

- `none` normalizes to `host`
- `toolang run` uses `host`
- `toolang start` may use `host` or `docker:<image>`
- `${TOOLANG_ROOT}/sandbox/{AGENT_KEY}/`
  - staged start files
- `${AGENT_ROOM}/sandbox/`
  - runtime sandbox mount point
