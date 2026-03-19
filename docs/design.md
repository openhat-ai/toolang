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
- `resident agent`
  - an agent hosted under `${TOOLANG_ROOT}/agents/`
- `roaming agent`
  - an agent hosted at an external local path
- `visiting agent`
  - an agent materialized from a remote URL under `${TOOLANG_ROOT}/guests/`
- `house_no`
  - a short persistent identifier for an agent instance
- `cap_ref`
  - a capability reference used by `skill`, `service`, `prompt`, and `psyche`
    operations
- `agent_ref`
  - an input that resolves to agent source
- `run_ref`
  - an input that resolves to a running agent


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
  - global table of running agents
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


## 7. Lock File

`toolang.lock` stores the managed resolved set for one agent home.

It does not store:

- inline cap bodies from `.too`
- local authored caps
- purely local path-based authored content

Minimal shape:

```toml
version = 1

[skills]
"pdf-processing" = { ref = "briceyan/pdf-processing", resolved = "github.com/briceyan/agent-skills@8f3c2d4a5b6c7d8e9f00112233445566778899aa:skills/pdf-processing" }

[services]
"github" = { ref = "github/github", resolved = "github.com/github/agent-services@1a2b3c4d5e6f70819293949596979899aabbccdd:services/github.md" }
```

Rules:

- `version = 1` is a top-level key
- each kind has its own top-level table
- each key is the effective managed name for that kind
- each entry stores:
  - `ref`
  - `resolved`
- if multiple authored sources produce the same kind and name:
  - identical refs merge into one lock entry
  - different refs fail with a name collision

Reason:

- the lock file is the durable resolved index
- keeping it keyed by managed name makes it easy to inspect and diff


## 8. Sync

`toolang sync` builds generated resolution state from authored inputs.

Inputs:

- top-level `.too` files
- `toolang.toml`
- local cap directories

Outputs:

- `toolang.lock`
- `${AGENT_HOME}/.toolang/.sync/`
- `${AGENT_ROOM}/`

Contract:

1. parse top-level `.too` files with Tree-sitter
2. analyze declarations, `use` statements, and source-defined caps
3. read managed refs from `toolang.toml`
4. compute the managed set keyed by kind and managed name
5. resolve each managed ref to an exact immutable target
6. compile per-agent synced records into the agent room
7. write synced capabilities under `${AGENT_HOME}/.toolang/.sync/`
8. rewrite `toolang.lock`
9. make `${AGENT_HOME}/.toolang/.sync/` exactly match the current synced cap set
10. write freshness metadata into the agent room
11. report local shadowing without rewriting local authored caps

Rules:

- the agent room stores the last sync time and input fingerprints used for
  freshness checks
- freshness uses recorded input metadata such as `mtime_ns` and file size for:
  - `.too` files in the agent home
  - `toolang.toml`
  - local cap directories under `.toolang/`
- `toolang run` may skip parse and sync when the current inputs still match
  the stored freshness metadata in the agent room
- if any recorded input changed, if generated sync artifacts are missing, or if
  `toolang.lock` is stale, `toolang run` triggers sync before execution
- source-defined caps are materialized during sync so unchanged agents do not
  need to be reparsed just to recover inline definitions
- if an entry exists in `toolang.lock`, the matching managed artifact must
  exist in `${AGENT_HOME}/.toolang/.sync/<kind>/`
- if an artifact exists in `${AGENT_HOME}/.toolang/.sync/` but not in the
  current synced cap set, sync removes it
- sync may reuse or refresh data under `TOOLANG_ROOT`
- sync does not make all of `TOOLANG_ROOT` equal to the current agent home

Reason:

- `${AGENT_HOME}/.toolang/.sync/` is the synced cap projection for one agent
  home
- `${AGENT_ROOM}/` holds agent-specific sync metadata and compiled program
  state
- `TOOLANG_ROOT` is shared local system state

Minimal sync state shape:

```json
{
  "version": 1,
  "synced_at": "2026-03-18T21:00:00Z",
  "source_file": "reviewer.too",
  "agent_room": ".toolang/agent/reviewer/",
  "synced_caps": ".toolang/.sync/",
  "inputs": {
    "toolang.toml": { "mtime_ns": 1742302800000000000, "size": 812 },
    "reviewer.too": { "mtime_ns": 1742302805000000000, "size": 2412 },
    ".toolang/skills/": { "mtime_ns": 1742302799000000000 }
  }
}
```


## 9. References

### 9.1 Agent References

`agent_ref` may be:

- a resident shorthand
- a guest shorthand
- a local `.too` path
- a canonical `agent://...` URI
- a canonical `file://...` URI
- a canonical `https://...` URI

Examples:

- `alice`
- `alice/alice`
- `alice/bob`
- `guest:alice`
- `bob.too`
- `./bob.too`
- `/path/to/some/dir/bob.too`
- `agent://alice/alice.too`
- `file:///path/to/some/dir/bob.too`
- `https://example.com/reviewer.too`

Resolution rules:

- `alice` resolves to `agent://alice/alice.too`
- `<home>/<agent>` resolves to `agent://<home>/<agent>.too`
- `guest:<name>` resolves through a guest resolver to a real `https://...` URI
- relative local paths are allowed as input and normalize to absolute
  `file://...` URIs
- canonical `file://...` URIs always use absolute normalized paths
- `agent_uri` is used for stable hashing
- `agent_uri` must not include `TOOLANG_ROOT`
- an `agent_ref` resolves to both:
  - `agent_uri`
  - `agent_home`

### 9.2 URL Materialization

Visiting agents materialize under `TOOLANG_ROOT/guests/`.

Rules:

- a visiting `https://...` URI creates or reuses a visiting agent home
- visiting agent placement is local and separate from canonical identity
- repeated runs may reuse the visiting agent home unless a refresh is requested
- `guest://...` is not a canonical URI in v1


## 10. CLI

### 10.1 Analysis and Inspection

- `toolang check <agent>`
- `toolang dump ast <agent>`
- `toolang dump ir <agent>`
- `toolang dump messages <agent>`

### 10.2 Execution

- `toolang run <agent>`
- `toolang serve <agent>`
- `toolang start <agent>`

Rules:

- `run` is one-shot foreground execution
- `run` checks sync freshness before execution and triggers sync when required
- `serve` runs in the foreground
- `start` runs in the background

### 10.3 Capability Management

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
  - rebuild `toolang.lock`, `${AGENT_HOME}/.toolang/.sync/`, and agent-local
    synced state in `${AGENT_ROOM}/`

### 10.4 Running-Agent Commands

- `toolang ps`
- `toolang inspect <run_ref>`
- `toolang logs <run_ref>`
- `toolang stop <run_ref>`


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
- the agent room also stores sync metadata and compiled program state
- the agent room lives under `.toolang/` because it is machine-managed

`will.md` stores durable agent-local intent and working state.

Running agents are tracked in `~/.toolang/agents.db`.

Each active process records:

- `house_no`
- `agent_name`
- `source_uri`
- `agent_home`
- `source_file`
- `pid`
- `mode`
- `status`
- `started_at`
- `heartbeat_at`
- `endpoint` when applicable

Mode values:

- `run`
- `serve`


## 12. Runtime Flow

Primary runtime flow:

1. `parse`
2. `sync`
3. `run`

Fast path:

- if the freshness metadata in `${AGENT_ROOM}/` is still valid, runtime skips
  parse and sync and runs from the existing synced artifacts

Definitions:

- `parse`
  - use Tree-sitter to read `.too` source into structured syntax data
- `sync`
  - build durable generated state for one agent home
- `run`
  - load synced state from `${AGENT_HOME}/.toolang/.sync/` and `${AGENT_ROOM}/`,
    assemble runtime inputs, and execute the model and tool loop

Internal sync steps:

- `analyze`
  - semantic checks
- `resolve`
  - dependency and capability resolution
- `compile`
  - convert source and config inputs into per-agent synced records
- `materialize`
  - update `toolang.lock`, `${AGENT_HOME}/.toolang/.sync/`, and synced state in
    `${AGENT_ROOM}/`
