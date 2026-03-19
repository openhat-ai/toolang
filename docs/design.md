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
  `.toolang/agent/{AGENT}/`
- synced caps live under `.toolang/.sync/`
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
  - global registry of known agents and active served agents
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

Reason:

- local placement and canonical identity are separate concerns
- resident, roaming, and visiting agents all need a local agent home


## 5. Capability Model

### 5.1 Capability Sources

Toolang combines three authored capability sources:

- source caps
  - refs or inline definitions in `.too`
- config caps
  - managed entries in `toolang.toml`
- local caps
  - files or directories in
    `.toolang/{skills,services,prompts,psyches}/` inside an agent home

Default visible set:

- source
- config
- local

Precedence for the same kind and name:

1. local
2. config
3. source

Reason:

- local overrides make it easy to patch or test a cap without editing `.too`
  or `toolang.toml`

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

- `source`
- `config`
- `local`

Rules:

- omitted switches use `source,config,local`
- `--caps` applies to all kinds
- a kind-specific switch overrides `--caps` for that kind
- CSV order does not change precedence

Examples:

- `--caps=source`
- `--caps=source,config`
- `--caps=source,local`
- `--caps=source --skills=source,config,local`

### 5.3 Capability References

`cap_ref` is kind-neutral. Resolution depends on the capability kind.

Examples:

- `toolang skill add briceyan/pdf-processing`
- `toolang service add github/github`
- `use skill briceyan/pdf-processing`

The same ref syntax is used in:

- `toolang.toml`
- `.too` `use` declarations
- CLI commands that manage refs

### 5.4 GitHub Resolution

The first supported registry is GitHub.

Bare ref format:

- `user-or-org/cap-name`

Probe rules:

- for `service`
  - repos: `agent-services`, `services`
  - paths: `services/<cap-name>.md`, `<cap-name>.md`
- for `skill`
  - repos: `agent-skills`, `skills`
  - paths: `skills/<cap-name>/SKILL.md`, `<cap-name>/SKILL.md`

Canonical layout for new GitHub caps:

- service
  - repo: `agent-services`
  - path: `services/<cap-name>.md`
- skill
  - repo: `agent-skills`
  - path: `skills/<cap-name>/SKILL.md`


## 6. Home Configuration

`toolang.toml` stores managed caps and model configuration.

```toml
[skills]
"pdf-processing" = { ref = "briceyan/pdf-processing" }
"create-skill" = { ref = "anthropic/create-skill" }

[services]
"linear" = { ref = "acme/linear" }

[prompts]
"release-notes" = { path = "prompts/release-notes.md" }

[psyches]
"reviewer" = { path = "psyches/reviewer.md" }

[models]
default = ["local-qwen", "gpt-5.3"]

[models.local-qwen]
provider = "openai-compatible"
model = "qwen3-32b"
base_url = "http://127.0.0.1:11434/v1"
api_key_env = "LOCAL_MODEL_API_KEY"

[models.gpt-5.3]
provider = "openai"
model = "gpt-5.3"
api_key_env = "OPENAI_API_KEY"
```

Rules:

- top-level tables are grouped by kind
- the table key is the managed capability name
- entries use authored inline tables such as `{ ref = "owner/name" }`
- `toolang.toml` does not store resolved results


## 7. Per-Agent Sync State

Each top-level `.too` file writes one durable sync state file:

- `${AGENT_HOME}/.toolang/.sync/<agent>.state.json`

The state file stores:

- freshness metadata for the whole agent home
- the compiled synced program for that agent
- the exact resolved refs used by that agent

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
    "reviewer.too": { "mtime_ns": 1742302800000000000, "size": 812 },
    "shared.too": { "mtime_ns": 1742302810000000000, "size": 241 }
  },
  "program": {},
  "refs": {
    "skills": {
      "pdf-processing": {
        "ref": "by3gus/pdf-processing",
        "repo": "by3gus/agent-skills",
        "path": "skills/pdf-processing",
        "rev": "8f3c2d4a5b6c7d8e9f00112233445566778899aa"
      }
    }
  }
}
```

Rules:

- one state file exists per top-level `.too` file
- the state file is generated and should not be hand-edited
- `refs` keep agent-level provenance, even when multiple agents in one home use
  the same cap name
- raw synced caps under `${AGENT_HOME}/.toolang/.sync/` remain the materialized
  union for the whole agent home


## 8. Sync

`toolang sync` builds generated resolution state from authored inputs.

Inputs:

- top-level `.too` files
- `toolang.toml`
- local cap directories

Outputs:

- `${AGENT_HOME}/.toolang/.sync/`
- one `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` per top-level `.too`

Contract:

1. parse top-level `.too` files with Tree-sitter
2. analyze declarations, `use` statements, and source-defined caps
3. read managed refs from `toolang.toml`
4. compute the managed set keyed by kind and managed name
5. resolve each managed ref to an exact immutable target
6. compile per-agent synced records
7. write synced capabilities under `${AGENT_HOME}/.toolang/.sync/`
8. rewrite `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` for each agent
9. make `${AGENT_HOME}/.toolang/.sync/` exactly match the current synced cap set
10. report local shadowing without rewriting local authored caps

Rules:

- each `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` stores the last sync
  time and input fingerprints used for freshness checks
- freshness uses recorded input metadata such as `mtime_ns` and file size for:
  - `.too` files in the agent home
  - `toolang.toml`
  - local cap directories under `.toolang/`
- `toolang run` may skip parse and sync when the current inputs still match
  the stored freshness metadata in `${AGENT_HOME}/.toolang/.sync/<agent>.state.json`
- if any recorded input changed, if generated sync artifacts are missing, or if
  any expected state file is stale, `toolang run` triggers sync before execution
- source-defined caps are materialized during sync so unchanged agents do not
  need to be reparsed just to recover inline definitions
- if an entry exists in an agent's `refs`, the matching managed artifact must
  exist in `${AGENT_HOME}/.toolang/.sync/<kind>s/`
- if an artifact exists in `${AGENT_HOME}/.toolang/.sync/` but not in the
  current synced cap set, sync removes it
- sync may reuse or refresh data under `TOOLANG_ROOT`
- sync does not make all of `TOOLANG_ROOT` equal to the current agent home

Reason:

- `${AGENT_HOME}/.toolang/.sync/` is the synced cap projection for one agent
  home
- `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` holds agent-specific synced
  state and provenance
- `TOOLANG_ROOT` is shared local system state

Minimal sync state shape:

```json
{
  "version": 1,
  "synced_at": "2026-03-18T21:00:00Z",
  "source_file": "reviewer.too",
  "sync_state": ".toolang/.sync/reviewer.state.json",
  "synced_caps": ".toolang/.sync/",
  "inputs": {
    "toolang.toml": { "mtime_ns": 1742302800000000000, "size": 812 },
    "reviewer.too": { "mtime_ns": 1742302805000000000, "size": 2412 },
    ".toolang/skills/": { "mtime_ns": 1742302799000000000 }
  },
  "refs": {
    "skills": {
      "pdf-processing": {
        "ref": "by3gus/pdf-processing",
        "repo": "by3gus/agent-skills",
        "path": "skills/pdf-processing",
        "rev": "8f3c2d4a5b6c7d8e9f00112233445566778899aa"
      }
    }
  }
}
```


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


## 10. CLI

### 10.1 Execution

- `toolang run <agent>`
- `toolang serve <agent>`
- `toolang start <agent>`
- `toolang list`

Rules:

- all execution commands accept an `agent selector`
- `run` is one-shot foreground execution
- `run` checks sync freshness before execution and triggers sync when required
- `run` updates the known-agent registry but does not create a running-agent
  record
- `serve` runs in the foreground and registers one active served process for
  its `agent_uri`
- `start` launches `serve` in the background and returns the selected
  `agent_id` and local endpoint
- v1 allows at most one active served process per `agent_uri`

Grammar inspection and AST-oriented tooling belong in the sibling grammar
package rather than the Toolang runtime CLI.

### 10.2 Capability Management

- `toolang skill add <cap_ref>`
- `toolang skill new <name>`
- `toolang skill remove <name>`
- `toolang service add <cap_ref>`
- `toolang service new <name>`
- `toolang service remove <name>`
- `toolang prompt add <cap_ref>`
- `toolang prompt new <name>`
- `toolang prompt remove <name>`
- `toolang psyche add <cap_ref>`
- `toolang psyche new <name>`
- `toolang psyche remove <name>`
- `toolang use <kind> <cap_ref> --agent <agent_name>`
- `toolang sync`

Command intent:

- `add`
  - add a managed home-level ref to `toolang.toml`
- `use`
  - add a source-level `use` declaration to `<agent>.too`
- `new`
  - create a local authored cap
- `sync`
  - rebuild `${AGENT_HOME}/.toolang/.sync/` and all
    `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` files for the agent home

### 10.4 Running-Agent Commands

- `toolang ps`
- `toolang list`
- `toolang inspect <agent>`
- `toolang logs <agent>`
- `toolang stop <agent>`


## 11. Agent State

Each agent has a private room under `.toolang/agent/{AGENT}/` inside its agent
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
  - active served agents
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
- `pid`
- `status`
- `started_at`
- `heartbeat_at`
- `endpoint` when applicable

Rules:

- `run` may add or refresh an `agents` record
- only `serve` and `start` create `running_agents` records
- `agent.run` in the agent room mirrors the current running state for one agent
- `agent.log` stores the managed server log for one agent


## 12. Runtime Flow

Primary runtime flow:

1. `parse`
2. `sync`
3. `run`

Fast path:

- if `${AGENT_HOME}/.toolang/.sync/<agent>.state.json` is still valid, runtime
  skips parse and sync and runs from the existing synced artifacts

Definitions:

- `parse`
  - use Tree-sitter to read `.too` source into structured syntax data
- `sync`
  - build durable generated state for one agent home
- `run`
  - load synced state from `${AGENT_HOME}/.toolang/.sync/`, assemble runtime
    inputs, and execute the model and tool loop

Internal sync steps:

- `analyze`
  - semantic checks
- `resolve`
  - dependency and capability resolution
- `compile`
  - convert source and config inputs into per-agent synced records
- `materialize`
  - update `${AGENT_HOME}/.toolang/.sync/` and
    `${AGENT_HOME}/.toolang/.sync/<agent>.state.json`

Foreground and background execution build on the same prepared agent:

- `run`
  - prepare one synced agent and execute a thunk once
- `serve`
  - prepare one synced agent and expose a local HTTP API
- `start`
  - spawn `serve` as a background process and wait for registration
