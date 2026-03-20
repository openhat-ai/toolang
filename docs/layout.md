# Toolang Layout

This document defines Toolang filesystem layout and agent identity.


## 1. Terms

- `toolang root`
  - the local Toolang system directory
  - default: `~/.toolang`
  - override: `TOOLANG_ROOT`
- `agent`
  - one runnable `.too` source file
- `agent home`
  - the local directory that hosts an agent
  - every agent has an agent home, regardless of how it was discovered
- `agent room`
  - the private room inside an agent home
  - path: `${AGENT_HOME}/.toolang/agents/{AGENT}/`
- `resident agent`
  - an agent whose home lives under `${TOOLANG_ROOT}/agents/`
- `roaming agent`
  - an agent whose home stays at an external local path
- `visiting agent`
  - an agent discovered from a remote URL and materialized under
    `${TOOLANG_ROOT}/guests/`
- `agent_ref`
  - the raw CLI input used to locate an agent
- `agent selector`
  - a CLI input that may be a source-facing `agent_ref`, an `agent_id`, or an
    `agent_name`
- `agent_uri`
  - the canonical identity string used for stable hashing
- `agent_id`
  - a short stable hash derived from `agent_uri`


## 2. Canonical Agent URIs

Toolang keeps canonical identity separate from local placement.

Canonical URI forms:

- `agent://<home_name>/<agent_name>.too`
  - resident agent
- `file:///absolute/path/to/<agent_name>.too`
  - roaming agent
- `https://<host>/<path>`
  - visiting agent

Examples:

- `agent://alice/alice.too`
- `agent://alice/bob.too`
- `file:///path/to/some/dir/bob.too`
- `https://a.com/alice.too`

Rules:

- `agent_id = hash(agent_uri)`
- `agent_uri` must stay stable across different `TOOLANG_ROOT` locations
- resident identity must not include the absolute local root path
- local placement is described by `agent_home`, not by `agent_uri`
- `guest://...` is not a canonical URI in v1


## 3. Shorthands

The CLI accepts concise agent references and normalizes them into canonical
URIs.

Examples:

- `alice`
  - `agent://alice/alice.too`
- `alice/alice`
  - `agent://alice/alice.too`
- `alice/bob`
  - `agent://alice/bob.too`
- `guest:alice`
  - resolved by a guest resolver to a real `https://...` URI
- `bob.too`
  - normalized to an absolute `file:///.../bob.too` URI
- `./bob.too`
  - normalized to an absolute `file:///.../bob.too` URI
- `/path/to/some/dir/bob.too`
  - `file:///path/to/some/dir/bob.too`
- `https://a.com/alice.too`
  - already canonical
- `abe.fun/alice`
  - may normalize to `https://abe.fun/alice`


## 4. Resolution Rules

Resolution order for source-facing `agent_ref` values:

1. If the input already contains `://`, treat it as a canonical URI.
2. If the input starts with `guest:`, resolve it to a real `https://...` URI.
3. If the input looks like a local path, normalize it to an absolute
   `file://...` URI.
4. If the input is `<name>`, normalize it to `agent://<name>/<name>.too`.
5. If the input is `<home>/<agent>`, normalize it to
   `agent://<home>/<agent>.too`.
6. If the input is `<host>/<name>` and the first segment is a hostname, resolve
   it to `https://<host>/<name>`.

Additional rules:

- relative local paths are allowed as input but not as canonical URI
- canonical `file://` URIs always use absolute normalized paths
- an explicit `agent_ref` resolves to both:
  - `agent_uri`
  - `agent_home`
- `agent_home` is always local, even for visiting agents
- an `agent selector` may also be:
  - an `agent_id` prefix from `agents.db`
  - an exact `agent_name` from `agents.db`
- if an `agent selector` is not an explicit source selector, Toolang checks
  `agents.db` before falling back to resident shorthand resolution


## 5. Toolang Root

The default Toolang root is `~/.toolang`.

```text
{TOOLANG_ROOT}/
  agents.db
  agents.too                # optional
  agents/{HOME}/            # resident agent home
  guests/{HOME}/            # visiting agent home
  sandbox/{AGENT}/
  bus/
    events.db
    bus.run
    bus.log
```

Notes:

- `agents/{HOME}/` stores resident agent homes
- `guests/{HOME}/` stores visiting agent homes
- `sandbox/{AGENT}/` is reserved for local execution sandboxes
- `bus/` stores local event and bus state
- `bus/events.db` is the shared durable event store used by local agents and a
  future standalone bus server
- `agents.db` stores:
  - known agents keyed by `agent_uri`
  - active started agents keyed by `agent_uri`
- `agents.too` is optional


## 6. Agent Homes

Kinds of agent home:

- resident agent home
  - `${TOOLANG_ROOT}/agents/{HOME}/`
- visiting agent home
  - `${TOOLANG_ROOT}/guests/{HOME}/`
- roaming agent home
  - any external local directory, for example `/path/to/some/dir/`

Agent home layout:

```text
${AGENT_HOME}/
  {AGENT}.too
  agents.too                         # optional
  .env                               # optional
  .toolang/
    agents/{AGENT}/                  # private room
    sync/
      {AGENT}.state.json
      psyches/
      skills/
      services/
      prompts/
    psyches/
    skills/
    services/
    prompts/
```

Notes:

- `{AGENT}.too` is the runnable source file for that agent
- `agents.too` is optional
- `.toolang/sync/` stores synced capabilities for this agent home
- `.toolang/sync/{AGENT}.state.json` stores one generated sync record per
  top-level `.too` file
- `.toolang/{psyches,skills,services,prompts}/` stores shared caps for this
  agent home


## 7. Agent Room

Agent room path:

```text
${AGENT_HOME}/.toolang/agents/{AGENT}/
```

Agent room layout:

```text
${AGENT_ROOM}/
  agent.run
  agent.log
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

- the agent room is private to one agent
- `agent.run` mirrors the current running state and active loop set of one
  started agent
- `agent.log` stores the managed runtime log for one agent
- `chats/chats.db` stores durable chat threads and messages for one agent
- `sandbox/` stores the local execution sandbox for that agent
- `tasks/`, `chores/`, and `will.md` store agent-local work state
