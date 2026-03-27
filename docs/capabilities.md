# Toolang Capability Model

This document defines capability forms, scopes, refs, and sync materialization.

Exact filesystem paths live in [layout.md](./layout.md).
Top-level lifecycle and runtime-resource vocabulary lives in
[model.md](./model.md).


## 1. Capability Kinds

Toolang currently manages four capability kinds:

- `skill`
- `service`
- `prompt`
- `psyche`


## 2. Capability Forms

Toolang uses three authored capability forms:

- `ref`
  - declared with `use ...`
  - may live in `{AGENT}.too` or `agents.too`
- `inline`
  - declared directly in `{AGENT}.too`
- `local`
  - human-edited files or directories kept in local cap roots

Rules:

- `inline` is agent-scoped only
- `local` does not appear in `.too`
- `ref`, `inline`, and `local` may coexist for the same kind


## 3. Capability Scopes

Toolang uses three capability scopes:

- `agent`
  - source: `{AGENT_HOME}/{AGENT}.too`
- `shared`
  - source: `{AGENT_HOME}/agents.too`
  - local caps: `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
- `global`
  - source: `${TOOLANG_ROOT}/agents.too`
  - local caps: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`

Runtime precedence is:

1. `agent`
2. `shared`
3. `global`

Important rule:

- same-name caps in different scopes stay separate through sync
- effective override is chosen at runtime, not collapsed at sync time


## 4. Runtime Scope Visibility

`invoke`, `run`, and `start` always load agent-scoped caps.

Two CLI switches control whether wider scopes are enabled at runtime:

- `--shared/--no-shared`
- `--global/--no-global`

Default visibility depends on agent kind:

- `resident`
  - `shared=on`, `global=on`
- `roaming`
  - `shared=on`, `global=off`
- `visiting`
  - `shared=off`, `global=off`

These switches affect runtime visibility only. Sync still materializes each
scope in its own root.


## 5. Capability References

`cap_ref` is kind-neutral. Resolution depends on the selected cap kind.

Examples:

- `toolang skill add by3gus/pdf-processing`
- `toolang service add by3gus/github`
- `toolang prompt add by3gus/rewrite`
- `toolang psyche add by3gus/reviewer`
- `use skill by3gus/pdf-processing`

The same ref syntax is used in:

- `.too` `use` declarations
- `agents.too` shared source files
- CLI commands that manage refs


## 6. GitHub Resolution

The first supported registry is GitHub.

Bare ref format:

- `user-or-org/cap-name`

Probe rules:

- `service`
  - repos: `agent-services`, `services`
  - paths: `services/<cap-name>.md`, `<cap-name>.md`
- `prompt`
  - repos: `agent-prompts`, `prompts`
  - paths: `prompts/<cap-name>.md`, `<cap-name>.md`
- `psyche`
  - repos: `agent-psyches`, `psyches`
  - paths: `psyches/<cap-name>.md`, `<cap-name>.md`
- `skill`
  - repos: `agent-skills`, `skills`
  - paths: `skills/<cap-name>/SKILL.md`, `<cap-name>/SKILL.md`

Canonical layout for new GitHub caps:

- `service`
  - repo: `agent-services`
  - path: `services/<cap-name>.md`
- `prompt`
  - repo: `agent-prompts`
  - path: `prompts/<cap-name>.md`
- `psyche`
  - repo: `agent-psyches`
  - path: `psyches/<cap-name>.md`
- `skill`
  - repo: `agent-skills`
  - path: `skills/<cap-name>/SKILL.md`


## 7. Sync Outputs

`toolang sync` materializes three scope roots plus one per-agent state file.

Materialized roots:

- global
  - `${TOOLANG_ROOT}/sync/`
- shared
  - `${AGENT_HOME}/.toolang/sync/`
- agent
  - `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`

Per-agent state:

- `${AGENT_HOME}/.toolang/sync/<agent>.state.json`

The state file stores:

- freshness metadata
- the compiled synced program
- scope-separated exact refs
  - `agent_refs`
  - `shared_refs`
  - `global_refs`

The state file does not store:

- raw synced cap files
- local authored caps
- process state


## 8. Sync Contract

`toolang sync` uses this high-level flow:

1. parse top-level `.too` files
2. analyze declarations, `use` refs, and inline caps
3. parse shared and global `agents.too`
4. read shared and global local cap roots
5. resolve refs to exact immutable targets
6. materialize global, shared, and agent sync roots
7. rewrite one `<agent>.state.json` per top-level `.too`

Freshness rules:

- each state file records input fingerprints such as `mtime_ns` and file size
- `invoke` may skip parse and sync when current inputs still match the recorded
  state
- if inputs changed or sync artifacts are missing, runtime syncs again before
  execution

Reason:

- unchanged agents should not need reparsing to recover inline caps
- scope separation keeps runtime precedence explicit and reversible


## 9. Capability Commands

All four cap kinds share the same command shape.

For `<kind> in {skill, service, prompt, psyche}`:

- `toolang <kind> add <cap_ref> --scope=agent|shared|global`
- `toolang <kind> remove <name> --scope=agent|shared|global`
- `toolang <kind> local new <name> --scope=shared|global`
- `toolang <kind> local new <name> --from <cap_ref> --scope=shared|global`
- `toolang <kind> local path <name> --scope=shared|global`
- `toolang <kind> local delete <name> --scope=shared|global`

Command intent:

- `add`
  - add a ref to the selected authored source scope
- `remove`
  - remove a ref by name from the selected authored source scope
- `local new`
  - create a local editable cap
- `local new --from`
  - initialize a local editable cap from a remote ref
- `local path`
  - print the target local cap path without creating directories
- `local delete`
  - remove a local editable cap


## 10. Runtime API Projection

The runtime API exposes the effective visible capability set for the current
activation through:

- `GET /api/v1/caps`

Current response categories are:

- `psyches`
- `prompts`
- `skills`
- `services`
- `counts`

Rules:

- the API uses `service` and `services`, not `server` and `servers`
- the payload is a runtime visibility view, not a full authored capability
  inventory
- counts should use the same names as the visible capability arrays
