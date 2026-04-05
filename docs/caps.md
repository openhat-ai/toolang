# Toolang Caps

Caps are composable agent primitives.

This document defines cap identity, visibility, locator syntax, and sync.
Exact paths live in [layout.md](./layout.md). API surfaces live in [api.md](./api.md).


## 1. Cap Definition

A cap definition is one authored cap.

Each definition has:

- `kind`
- `name`
- `scope`
- `source`
- `locator`
- `content`
  - `meta`
    - front matter
  - `doc`
    - primary body
  - `assets`
    - optional sibling files

Authoritative identity is:

- `(kind, scope, source, locator)`

`name` is not a unique identifier.

Authoring and runtime are separate:

- authored state stores every cap definition
- runtime exposes one effective cap set after precedence is applied


## 2. Cap Kinds

Toolang currently defines four cap kinds:

- `psyche`
- `skill`
- `service`
- `prompt`


## 3. Cap Scopes

Toolang defines three scopes:

- `global`
  - visible to every agent under `${TOOLANG_ROOT}`
  - source file: `${TOOLANG_ROOT}/agents.too`
  - local cap roots: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`
- `home`
  - visible to every agent in one agent home
  - source file: `${AGENT_HOME}/agents.too`
  - local cap roots:
    `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
- `agent`
  - visible only to the owning agent
  - source file: `${AGENT_HOME}/{AGENT}.too`

Precedence is:

1. `agent`
2. `home`
3. `global`

Definitions in different scopes remain separate even when `kind` and `name`
match.


## 4. Cap Sources

Toolang defines three sources:

- `inline`
  - authored inside a `.too` file
- `local`
  - authored as a local file or directory
  - the local path is already authoritative
- `remote`
  - authored as a remote locator
  - the remote source is authoritative

Typical authored forms:

- `inline`
  - `${TOOLANG_ROOT}/agents.too`
  - `${AGENT_HOME}/agents.too`
  - `${AGENT_HOME}/{AGENT}.too`
- `local`
  - file caps:
    - `${TOOLANG_ROOT}/{services,prompts,psyches}/{name}.md`
    - `${AGENT_HOME}/.toolang/{services,prompts,psyches}/{name}.md`
  - directory caps:
    - `${TOOLANG_ROOT}/skills/{name}/`
    - `${AGENT_HOME}/.toolang/skills/{name}/`
  - skills conventionally use `SKILL.md` as the entry document
- `remote`
  - `.too` refs such as `use skill owner/name`
  - `.too` refs such as `use prompt github://owner/repo/prompts/rewrite.md`

Materialization rule:

- local caps are already materialized
- inline caps are materialized before runtime use
- remote caps are resolved, fetched, and materialized before runtime use


## 5. Cap Locator

`locator` points to the authoritative source.
It never points to a sync artifact.

Canonical locator syntax:

- `inline`
  - `<program_locator>#cap/<kind>/<name>`
  - examples:
    - `agent://team/alice.too#cap/prompt/summarize`
    - `file:///Users/bryan/.toolang/agents.too#cap/psyche/reviewer`
- `local`
  - `file:///absolute/path/to/<cap-file-or-dir>`
- `remote`
  - `<registry>://<authority>/<path>`
  - optional revision pin: `<registry>://<authority>/<path>@<rev>`

GitHub remote locators:

- source form in `.too`
  - `owner/name`
- canonical form
  - `github://owner/repo/path`
  - `github://owner/repo/path@rev`

GitHub probe rules for `owner/name`:

- `skill`
  - repos: `agent-skills`, `skills`
  - paths: `skills/<name>/`, `<name>/`
- `service`
  - repos: `agent-services`, `services`
  - paths: `services/<name>.md`, `<name>.md`
- `prompt`
  - repos: `agent-prompts`, `prompts`
  - paths: `prompts/<name>.md`, `<name>.md`
- `psyche`
  - repos: `agent-psyches`, `psyches`
  - paths: `psyches/<name>.md`, `<name>.md`

Rules:

- shorthand remote refs resolve to canonical locators before sync
- definitions with the same `(scope, kind, name)` and different locators are
  different authored definitions
- definitions with the same `(scope, kind, name)` do not override each other
  inside the same scope


## 6. Markdown And Assets

For Markdown-backed caps:

- `meta` is front matter
- `doc` is Markdown body
- `raw_text` is the authoritative text form

Sync may parse `meta` and `doc`, but it must preserve the authored document.

Assets belong to the cap definition that owns the locator.


## 7. Materialization

Materialization writes runtime-ready local artifacts for `inline` and `remote`
definitions.

Scope sync roots:

- `global`
  - `${TOOLANG_ROOT}/sync/`
- `home`
  - `${AGENT_HOME}/.toolang/sync/`
- `agent`
  - `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`

Materialized definitions live at:

- `${SCOPE_SYNC_ROOT}/defs/{kind}/{source}/{definition_key}/`

Lookup metadata may live at:

- `${SCOPE_SYNC_ROOT}/index/{kind}/{name}.json`

Rules:

- local caps stay in local cap roots
- inline and remote caps never materialize into local cap roots
- materialized paths must not be keyed only by `(kind, name)`
- `definition_key` is derived from the canonical locator
- two different locators must never share one materialized path

Each materialized definition should record:

- `kind`
- `name`
- `scope`
- `source`
- `locator`
- `materialized_path`

Remote materialization should also record resolved source details such as:

- `repo`
- `path`
- `rev`


## 8. Effective Cap Set

The effective cap set is the runtime-visible result for one activation.

Derivation:

1. collect visible definitions from `global`, `home`, and `agent`
2. materialize `inline` and `remote` definitions
3. reject same-scope duplicates for one `(scope, kind, name)`
4. group candidates by `(kind, name)`
5. select the winner by scope precedence
6. mark the rest as shadowed

Rules:

- one activation uses one effective cap set
- runs inherit the effective cap set of their activation
- runtime reads the effective cap set
- authoring surfaces target one concrete cap definition
- same-scope duplicates are authored conflicts


## 9. Current Sync Cache

The current implementation caches sync state at three layers:

- freshness cache
  - `ensure_agent_synced()` returns cached state when:
    - input fingerprints match
    - global sync output matches expected entries
    - home sync output matches expected entries
    - agent sync output matches expected entries
- ref cache
  - resolved remote refs are stored in `SyncState`
  - if the same `use <kind> <ref>` appears again, sync reuses the locked
    `repo`, `path`, and `rev` and skips remote ref resolution
- materialization cache
  - before fetch, sync checks the target sync root for an existing materialized
    cap with matching metadata and file set
  - if it matches, sync skips fetch

The current implementation does not keep a persistent download cache:

- `fetch_github_artifact()` downloads into a temporary directory
- the temporary directory is deleted after materialization

Current deduplication boundary:

- `global` scope deduplicates across all agents under one `${TOOLANG_ROOT}`
- `home` scope deduplicates across all agents in one `${AGENT_HOME}`
- `agent` scope does not deduplicate across agents

Current fetch behavior for one remote cap referenced by multiple agents:

- if the cap is referenced in `${TOOLANG_ROOT}/agents.too`, it is fetched once
  per Toolang root
- if the cap is referenced in `${AGENT_HOME}/agents.too`, it is fetched once
  per agent home
- if the cap is referenced separately in multiple `{AGENT}.too` files, it is
  fetched once per agent

Current sync orchestration rule:

- `sync_agent(agent)` syncs every `.too` file in the same agent home, not only
  the selected agent

Current path keying:

- current sync artifacts are keyed by `(scope root, kind, name)`
- current sync artifacts are not keyed by canonical locator
