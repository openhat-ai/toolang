# Toolang Agent Notes

## Repository Rules

- Use semantic commit messages.
- Use semantic commit messages for PR titles as well.
- Do not add `[codex]` or similar automation prefixes to PR titles unless the
  user explicitly requests them.
- Write all code and documentation in English.
- Keep changes PR-sized and composable.
- When creating PRs with `gh`, avoid inline shell-quoted multiline bodies.
  Use `--body-file` or a single-quoted heredoc so Markdown formatting is not
  corrupted by shell interpolation.
- Open ready-for-review PRs by default. Use draft PRs only when the user
  explicitly asks for a draft.
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
- Scheduled work definitions should use RRULE-based scheduling in persisted
  models and APIs. Do not introduce new `interval_sec`-style schedule fields
  for chores or will.
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

- `docs/index.md` is the entry point for the public design docs in `docs/`.
- `docs/concepts.md` defines the shared runtime vocabulary.
- `docs/ids.md` defines Toolang-owned id families, reversible encoding, and
  durable allocator state.
- `docs/program.md` defines `.too` program syntax, including `struct`,
  `slash`, thunk signatures, thunk directives, and surface rules.
- `docs/layout.md` defines Toolang root, agent home, and agent room layout.
- `docs/caps.md` defines cap kinds, scopes, sources, and effective-cap rules.
- `docs/tasks.md` defines task and chore documents and their runtime mapping.
- `docs/execution.md` defines durable records, trace events, and response
  events.
- `docs/chat.md` defines thread, run, and message projections.
- `docs/models.md` defines model selectors, profiles, and built-in model
  integrations.
- `docs/tools.md` defines built-in tool families.
- `docs/plugins.md` defines shared plugin boundaries and loading.
- `docs/api.md` defines the CLI and local agent HTTP API.
- `reference/README.md` is the entry point for generated implementation
  reference derived from code. `reference/` is not a design-doc directory.
- The Tree-sitter grammar source of truth lives in the sibling
  `tree-sitter-toolang` repository. Toolang consumes the Python extension
  package exposed by that repository rather than compiling grammar sources at
  runtime.
- `toolang.base` owns the shared plugin-facing protocols, value types, and
  helper utilities used across tool, loop, channel, sandbox, model provider,
  and model adapter plugins.
- `toolang.program` owns `.too` parsing, authored source semantics, and source
  editing.
- `toolang.agents` owns local agent home layout, runtime-state files, and
  managed agent process helpers.
- `toolang.caps` owns cap refs, authored cap files, local cap config, and
  prepared cap views.
- `toolang.work` owns task and chore document semantics.
- `toolang.state` owns durable, prepared, live, and pulse state models.
- `toolang.execution` owns run binding, execution trace, durable run truth,
  response projection, and execution storage.
- `toolang.tools`, `toolang.loops`, `toolang.channels`, `toolang.sandboxes`,
  `toolang.models.providers`, and `toolang.models.adapters` own the built-in
  plugin-family implementations.
- `toolang.plugin` owns generic entry point discovery and plugin
  loading.
- `toolang.config` owns runtime config resolution helpers.
- `toolang.up` owns agent startup and FastAPI app assembly.
- `toolang.cli` owns CLI orchestration and environment resolution.


## Agent Identity

- Canonical resident agent URI: `agent://<home>/agent.too`
- Canonical roaming agent URI: `file:///absolute/path/to/<agent>.too`
- Canonical visiting agent URI: `https://<host>/<path>`
- `alice` and `agent:alice` are resident selectors, not canonical URIs.
- `guest:<name>` and `roaming:<name>` are CLI selectors for already known
  non-resident agents, not canonical URIs.
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
- `runs.db` owns runtime transcript messages as well as activation,
  thread, run, and step truth. Do not add a separate durable chat-store layer.
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
