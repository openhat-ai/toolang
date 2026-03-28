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
- supported package/runtime range: `>=3.10`
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

Current internal packages:

- `toolang.program`
  - parsing and syntax analysis
- `toolang.agent`
  - agent resolution, managed local-agent operations, preparation, and registry
- `toolang.runtime`
  - activation lifecycle, run execution, chat state, prompt build, scheduling,
    and server runtime
- `toolang.caps`
  - cap refs, sync orchestration, materialization, and runtime cap views
- `toolang.concepts`
  - shared identity, execution, layout, sandbox, caps, and persisted
    constructs
- `toolang.tools`
  - built-in runtime tool families, provider loading, and local tool execution
- `toolang.bus`
  - shared bus projection and bus API
- `toolang.sandbox`
  - sandbox runtime helpers
- `toolang.cli`
  - CLI surfaces and command registration
- `toolang.web`
  - small shared FastAPI app helpers

Reason:

- shared constructs should live in one explicit internal package instead of
  being redefined across runtime, sync, and caps modules
- caps logic should stay grouped under one package, even while it still
  spans authoring, materialization, and runtime view concerns
- the runtime package should stay focused on execution and state


## 4. Storage Choices

SQLite is the primary local storage layer.

Current or expected uses:

- chat transcripts
  - `${AGENT_ROOM}/chats/chats.db`
- execution truth
  - `${AGENT_ROOM}/execution.db`
  - activation, thread, run, and step records
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


## 5. Parser And Sync Direction

Implementation rules:

- Tree-sitter grammar is the syntax source of truth
- runtime parsing uses Tree-sitter output, not a second handwritten parser
- sync writes durable generated artifacts that can be reused without reparsing
  unchanged sources
- synced state under `.toolang/sync/` is the execution boundary, not raw source


## 6. Repository Boundaries

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


## 7. Simplicity Rules

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
