# Toolang Implementation Notes

This document records the selected implementation direction for Toolang v1.

Design semantics live in:

- [index.md](./index.md)
- [layout.md](./layout.md)
- [caps.md](./caps.md)
- [execution.md](./execution.md)
- [api.md](./api.md)
- [tools.md](./tools.md)
- [plugins.md](./plugins.md)
- [python.md](./python.md)


## 1. Language And Packaging

- implementation language: Python
- supported package/runtime range: `>=3.11`
- default local development version: `3.13`
- build backend: `hatchling`
- test runner: `pytest`

Reason:

- Python keeps iteration fast and covers CLI, HTTP, SQLite, scheduling, and
  model APIs well
- the package should stay broadly installable while local development can use a
  newer pinned interpreter


## 2. Selected Stack

- syntax
  - Tree-sitter via the published `tree-sitter-toolang` Python extension
- CLI
  - Typer
- validation and serialization
  - Pydantic v2
- model execution
  - official OpenAI Python SDK
- HTTP server
  - FastAPI
  - Uvicorn
- HTTP client
  - `httpx`
- local storage
  - SQLite
- recurrence parsing
  - `python-dateutil`


## 3. Package Boundaries

Stable package map:

- `toolang.concepts`
  - shared concepts and persisted models
- `toolang.program`
  - `.too` parsing and authored source semantics
- `toolang.agent`
  - agent resolution, managed local-agent operations, preparation, and registry
- `toolang.caps`
  - cap refs, sync, materialization, and prepared/live cap views
- `toolang.runtime`
  - durable, prepared, and live runtime behavior
- `toolang.memory`
  - memory family contract and first-party memory plugins
- `toolang.channels`
  - inbound and outbound channel bindings
- `toolang.tools`
  - built-in tool families, provider loading, and tool runtime
- `toolang.sandbox`
  - sandbox lifecycle and execution environment helpers
- `toolang.plugins`
  - plugin loading and plugin capability wiring
- `toolang.bus`
  - shared event projection and bus API
- `toolang.cli`
  - CLI orchestration and environment resolution

Rules:

- shared concepts live in `toolang.concepts`
- authored source semantics stay with the package that owns the source format
- runtime state and execution stay in `toolang.runtime`
- `memory`, `tools`, `channels`, and `sandbox` are plugin families
- `toolang.plugins` owns generic plugin discovery and loading
- family-specific contracts stay with their family packages
- plugin, channel, tool, sandbox, and caps concerns stay separate from runtime


## 4. Runtime Modules

Recommended runtime modules:

- `runtime/process.py`
  - `RuntimeProcess`
  - the process-level owning object
- `runtime/state.py`
  - live runtime state
- `runtime/prepare.py`
  - snapshot build and dirty checks
- `runtime/watcher.py`
  - durable-definition watching and live refresh
- `runtime/control.py`
  - durable and operational writes
- `runtime/inspect.py`
  - read-only runtime inspection
- `runtime/runner.py`
  - one admitted run
- `runtime/assembly.py`
  - prompt assembly

Rules:

- use `process`, not `host`, for the process-level runtime object
- use `runner` for one run execution pipeline
- use `assembly` for prompt assembly
- do not use sandbox `host/guest` terms for runtime module names


## 5. Storage Choices

SQLite is the primary local storage layer.

Current or expected uses:

- execution truth
  - `${AGENT_ROOM}/execution.db`
  - activation, thread, run, step, and transcript-message records
- known-agent and running-agent registry
  - `${TOOLANG_ROOT}/agents.db`
- shared bus projection
  - `${TOOLANG_ROOT}/bus/events.db`

Other durable formats:

- Markdown
  - task, chore, will, skill, service, prompt, and psyche source artifacts
- JSON
  - sync state, prompt traces, API output, runtime metadata, and task mirrors
- TOML
  - authored runtime and plugin configuration when needed


## 6. Parser And Sync Direction

Implementation rules:

- Tree-sitter grammar is the syntax source of truth
- runtime parsing uses Tree-sitter output, not a second handwritten parser
- sync writes durable generated artifacts that can be reused without reparsing
  unchanged sources
- synced state under `.toolang/sync/` is the execution boundary, not raw source


## 7. Repository Boundaries

This repository owns:

- runtime code
- CLI code
- sync integration
- the internal `toolang.caps` package until it is split out if needed
- tests for runtime behavior

Sibling repositories own:

- the Tree-sitter grammar source
- editor packages
- the docs site


## 8. Simplicity Rules

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
