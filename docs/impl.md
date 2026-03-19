# Toolang Impl

This document records the selected implementation direction for Toolang v1.

It focuses on stack choices and module boundaries. Source layout, capability
semantics, and runtime behavior live in
[design.md](/Users/bryan/openhat-ai/toolang/docs/design.md).


## 1. Language And Packaging

- implementation language: Python
- supported package/runtime range: `>=3.10`
- default local development version: `3.13`
- build backend: `hatchling`
- test runner: `pytest`

Reason:

- Python keeps iteration fast and covers CLI, HTTP, SQLite, and model APIs well
- the package should stay broadly installable while local development can use a
  newer pinned interpreter


## 2. Runtime Structure

The main runtime flow is:

1. `parse`
2. `sync`
3. `run`

Responsibilities:

- `parse`
  - use Tree-sitter to read `.too` source into structured syntax data
- `sync`
  - build durable generated state for one agent home
- `run`
  - load synced state from `${AGENT_HOME}/.toolang/.sync/`, then execute the
    model and tool loop

Expected runtime behavior:

- most invocations should spend their time in `run`
- `parse` and `sync` are rebuild steps and should be skipped when existing sync
  state is still fresh
- the synced state in `${AGENT_HOME}/.toolang/.sync/` is the execution
  boundary, not the raw source text

Internal sync steps:

- `analyze`
  - semantic validation
- `resolve`
  - capability and reference resolution
- `compile`
  - convert source and config inputs into per-agent synced records
- `materialize`
  - update `${AGENT_HOME}/.toolang/.sync/` and
    `${AGENT_HOME}/.toolang/.sync/<agent>.state.json`


## 3. Parsing And Sync State

Tree-sitter is the parsing foundation for Toolang.

Implementation strategy:

- Tree-sitter grammar is the syntax source of truth
- runtime parsing uses Tree-sitter output rather than a separate handwritten
  syntax parser
- sync writes durable generated artifacts that can be reused without reparsing
  unchanged source files
- synced caps are written under `${AGENT_HOME}/.toolang/.sync/`
- per-agent sync state lives in `${AGENT_HOME}/.toolang/.sync/<agent>.state.json`

Reason:

- one grammar should drive runtime parsing, editor tooling, and syntax-aware
  utilities
- durable sync artifacts make steady-state execution cheap
- this reduces grammar drift between the runtime and editor integrations


## 4. Data Model

Pydantic v2 is the chosen model layer for structured runtime data.

Expected use:

- AST and synced agent records
- home configuration and resolved-cap records
- registry and resolved-cap records
- structured CLI and API output

Reason:

- explicit validation is useful at parser, config, and API boundaries
- JSON serialization is built-in and predictable


## 5. CLI

Typer is the chosen CLI framework.

Command groups:

- analysis
- execution
- capability management
- running-agent management

Representative commands:

- `toolang run`
- `toolang serve`
- `toolang start`
- `toolang sync`

Reason:

- the command tree is large enough that typed subcommands and reusable option
  groups are worth the extra dependency


## 6. Runtime And Model Integration

The runtime core uses the official OpenAI Python SDK for model execution.

Expected responsibilities:

- message assembly
- tool registration and execution loop
- model fallback handling
- output coercion and structured-output enforcement

Reason:

- the runtime maps directly to developer messages, user messages, tool calls,
  and structured responses
- a direct SDK integration keeps the core runtime easier to reason about than a
  heavier orchestration framework


## 7. HTTP And Networking

Chosen server stack:

- FastAPI
- Uvicorn

Chosen client stack:

- `httpx`

Expected use:

- `toolang serve`
- registry access
- remote home fetches
- remote capability downloads

Reason:

- these libraries are mature, simple, and fit both local-first and API-driven
  runtime modes


## 8. Storage

SQLite is the primary local storage layer.

Expected use:

- agent memory
- running-agent table
- local metadata

Reason:

- SQLite is built into Python
- it has low operational overhead
- it is sufficient for the local-first runtime model

Other durable formats:

- TOML
  - `toolang.toml`
- Markdown
  - skill, service, prompt, and psyche source artifacts
- JSON
  - per-agent sync state, command output, API output, and runtime metadata blobs


## 9. Capability Resolution

Capability materialization uses two storage layers:

- `${AGENT_HOME}/.toolang/.sync/`
  - synced cap artifacts for one agent home
- `TOOLANG_ROOT`
  - reusable local system state for resident and visiting agents

Reason:

- per-agent-home reproducibility and cross-home reuse should stay separate
- `${AGENT_HOME}/.toolang/.sync/` needs exact sync semantics
- `TOOLANG_ROOT` should stay reusable across resident and visiting agents


## 10. Repository Boundaries

This repository owns:

- runtime code
- CLI code
- runtime parser and sync integration
- the colocated `toolang_caps` package until it is split out
- tests for runtime behavior

Sibling repositories own:

- Tree-sitter grammar source
- editor packages
- docs site

Reason:

- runtime behavior and editor packaging evolve at different speeds
- caps storage and synchronization logic already has a cleaner extraction
  boundary than the rest of the runtime
- keeping editor packages separate avoids polluting the runtime package


## 11. Simplicity Rules

The v1 implementation should stay intentionally small:

- local-first runtime
- no required framework layer above the model SDK
- no separate package-manager binary
- simple background supervision
- shared grammar for runtime and editor parsing
- synced execution state as the main runtime boundary

Reason:

- the runtime surface is still evolving
- fewer moving parts make it easier to stabilize the language and execution
  model first
