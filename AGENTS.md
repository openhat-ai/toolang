# Toolang Agent Notes

## Repository Rules

- Use semantic commit messages.
- Write all code and documentation in English.
- Keep changes PR-sized and composable.
- When creating PRs with `gh`, avoid inline shell-quoted multiline bodies.
  Use `--body-file` or a single-quoted heredoc so Markdown formatting is not
  corrupted by shell interpolation.
- Before each commit, run:
  - `uv run ty check --python-version 3.13 src tests`
  - `uv run ruff check`
  - `uv run pytest -q`
- Update this file when new project-wide conventions become stable.


## Implementation Style

- Prefer minimal designs over deep abstraction stacks.
- Do not add layers that only forward parameters without adding meaning.
- Put core data structures, file definitions, and common operations in focused
  modules.
- Toolang is still in a very early implementation stage. Do not preserve
  compatibility with earlier local designs just for the sake of continuity.
  If a change is clearly simpler, easier to maintain, or better aligned with
  the concepts confirmed in discussion, make the change directly.
- Do not read environment variables inside core modules.
- Do not infer ambiguous parameters inside core modules.
- Resolve environment variables, CLI inputs, and default values at the call
  site, then pass explicit values into lower-level functions.
- Use mature libraries when they clearly reduce code size or validation
  complexity.
- Keep package facades narrow. Export only stable entry points from
  `__init__.py`; import internal view models or helpers from concrete modules
  at the call site.
- Put behavior on the concept or persisted model when it is part of that
  concept's meaning. Do not duplicate the same mapping, overlay, sorting, or
  serialization logic across multiple modules.
- Persisted Pydantic models should own their `load()` / `save()` methods.
  Do not add serializer wrapper modules unless they add real meaning.
- Let the package that owns a source format own its parsing and source-editing
  semantics. For example, `.too` parsing and authored source edits belong to
  `toolang.program`, not to adjacent packages that merely consume programs.
- Prefer APIs built around concept objects over loose primitive bundles. If a
  runtime operation naturally works on `SandboxSpec`, `SandboxState`,
  `AgentRef`, or similar constructs, expose that object directly instead of
  passing separate strings, ids, and paths.
- When a split package no longer clarifies ownership, merge it back into the
  owning package instead of preserving an empty abstraction boundary.


## Current Design Boundaries

- `docs/index.md` is the entry point for the design docs in `docs/`.
- `docs/layout.md` is the source of truth for Toolang root, agent home, agent
  room, and canonical agent URI layout.
- `docs/capabilities.md` defines capability forms, scopes, refs, and sync
  semantics.
- `docs/execution.md` defines runtime execution semantics.
- `docs/api.md` defines CLI, registry, agent API, and bus API surfaces.
- `docs/impl.md` defines implementation direction and stack choices.
- `docs/plugins.md` defines plugin boundaries and loading.
- `docs/memory.md` defines the memory-plugin contract.
- `reference/README.md` is the entry point for generated implementation
  reference derived from code. `reference/` is not a design-doc directory.
- The Tree-sitter grammar source of truth lives in the sibling
  `tree-sitter-toolang` repository. Toolang consumes the Python extension
  package exposed by that repository rather than compiling grammar sources at
  runtime.
- `toolang.program` owns `.too` parsing, authored source semantics, and source
  editing.
- `toolang.agent` owns agent selector resolution, managed local-agent
  operations, prepared runtime inputs, and the local agent registry.
- `toolang.caps` owns cap refs, sync/materialization, local cap files, and
  runtime cap views. It should consume normalized program and concept objects
  rather than reinterpret raw `.too` syntax.
- `toolang.concepts.layout` owns layout concepts such as the Toolang root,
  agent home, and agent room. Put durable layout behavior on those concept
  objects instead of rebuilding paths ad hoc.
- `toolang.sandbox` owns sandbox lifecycle helpers. Sandbox concepts and
  persisted sandbox state live in `toolang.concepts.sandbox`.
- `toolang.web` owns the small shared web/app helpers used by FastAPI app
  modules.
- Stable shared constructs live in `toolang.concepts` so runtime, caps, and
  storage code can depend on one explicit concept source. Shared concepts may
  include small operations that eliminate repeated implementation glue.


## Agent Identity

- Canonical resident agent URI: `agent://<home>/<agent>.too`
- Canonical roaming agent URI: `file:///absolute/path/to/<agent>.too`
- Canonical visiting agent URI: `https://<host>/<path>`
- `guest:<name>` is a shorthand, not a canonical URI.
- Canonical identity must not depend on the absolute `TOOLANG_ROOT` path.


## CLI Responsibilities

- The CLI is responsible for reading `TOOLANG_ROOT` and any other environment
  variables.
- The CLI is responsible for resolving channel config environment references
  such as `token_env` before constructing long-lived runtime inputs.
- The CLI resolves `agent_ref` values into explicit runtime inputs before
  calling lower-level modules.
- The CLI should orchestrate work, but not absorb all parsing, file-shape, or
  storage logic.


## Files And Storage

- Keep file-shape logic in dedicated modules.
- Keep path/layout logic in dedicated modules.
- Keep runtime execution logic separate from file parsing and path resolution.
- Synced state should be reusable without reparsing unchanged source files.


## Principles Borrowed From Takoagent

- Keep path helpers pure and predictable.
- Separate `resolve`, `fetch`, `materialize`, and `install` style steps instead
  of hiding them behind one large function.
- Let lower-level modules return structured values; let the CLI decide when and
  where to persist them.
- Keep canonical identity separate from local placement.
- Keep generated state diff-friendly and deterministic.
- Make directory layout explicit with small helper functions instead of
  reconstructing paths ad hoc at many call sites.
