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
  `__init__.py`; import internal inspection or helpers from concrete modules
  at the call site.
- Put behavior on the concept or persisted entity when it is part of that
  concept's meaning. Do not duplicate the same mapping, overlay, sorting, or
  serialization logic across multiple modules.
- Scheduled work definitions should use RRULE-based scheduling in persisted
  entities and APIs. Do not introduce new `interval_sec`-style schedule fields
  for chores.
- Persisted Pydantic entities or records should own their `load()` / `save()`
  methods. Do not add serializer wrapper modules unless they add real meaning.
- Let the package that owns a source format own its parsing and source-editing
  semantics. For example, `.too` parsing and authored source edits belong to
  `toolang.lang`, not to adjacent packages that merely consume programs.
- Prefer APIs built around concept objects over loose primitive bundles. If a
  runtime operation naturally works on `SandboxSpec`, `SandboxState`,
  `AgentRef`, or similar constructs, expose that object directly instead of
  passing separate strings, ids, and paths.
- When a split package no longer clarifies ownership, merge it back into the
  owning package instead of preserving an empty abstraction boundary.


## Module Naming

- Use `types.py` for shared business vocabulary, scalar aliases, and enums.
- Use `records.py` for storage-entry representations.
- Use `events.py` for event types.
- Use `errors.py` for package-owned exception types. Do not use the singular
  `error.py`, and do not place non-exception message constants in `errors.py`.
- Use `schemas.py` for protocol-boundary types, including HTTP request and
  response schemas. Pure construction from package-owned types or records
  should be class methods on the resulting schema. Schema modules must not
  depend on stores, watchers, runtime services, or event emitters.
- Do not create `projection.py`. Put pure record-to-schema and type-to-schema
  conversion on the schema type itself. Put aggregate loading or store-backed
  read access in a focused concept module owned by the same package, such as
  `execution/history.py`.
- Reserve `model.py`, `models.py`, and `models/` for LLM model concepts so
  business data types cannot be confused with language-model integrations.
- Use `config.py` for the configuration-file types and parsing owned by one
  package, especially sections read from root or home `config.toml` files.
  Keeping this name consistent makes supported configuration discoverable
  across packages. Keep environment resolution and process-level defaults at
  the call site unless the owning package explicitly owns those semantics.


## Current Design Boundaries

- `docs/index.md` is the entry point for the public design docs in `docs/`.
- `docs/concepts.md` defines the shared runtime vocabulary.
- `docs/ids.md` defines Toolang-owned id families, reversible encoding, and
  durable allocator state.
- `docs/program.md` defines `.too` declarations, executable signatures,
  agics, flows, directives, and surface rules.
- `docs/flow-syntax.md` defines flow statements, bindings, and clauses.
- `docs/input-syntax.md` defines `ContentBody` syntax, input perceiving, input
  coercion, and output coercion.
- `docs/layout.md` defines Toolang root, agent home, and agent room layout.
- `docs/caps.md` defines cap kinds, scopes, sources, and effective-cap rules.
- `docs/tasks.md` defines task and chore documents and their runtime mapping.
- `docs/execution.md` defines execution boundaries, lifecycle, locals,
  persistence, tracing, and caller-facing projection.
- `docs/executor.md` defines agic and flow executor behavior.
- `docs/run-step-records.md` defines durable execution records and their source
  run events.
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
  helper utilities used across tool, channel, sandbox, model provider,
  and model adapter plugins, including canonical `PerceptPart`, `Percept`,
  `MessagePart`, and `Message` values and the shared Toolang error type.
- `toolang.common` owns package-neutral filesystem, immutable-container, and
  text-template helpers, progress events, selectors, Toolang-owned id allocation,
  and shared GitHub source-reference parsing and rendering.
  `toolang.common.errors` is a compatibility export of the error type owned by
  `toolang.base`.
- `toolang.lang` owns `.too` parsing, authored source semantics, source
  editing, pure `ContentBody` parsing, input perceiving, input coercion, and
  output coercion. The language and its AST retain the concise `Part` and `Part[]`
  type names; they map to package-level `PerceptPart` and `Percept` values
  respectively.
- `toolang.up` owns remote agent target resolution, runtime-state files,
  managed processes, visiting and roaming materialization, sandbox filesystem
  assembly, resolved `AgentHosting`, API process assembly, and channel
  execution orchestration.
- `toolang.catalog` owns local agent-home CRUD, authored cap and job CRUD,
  wired cap references, and bundled authored-file templates. Its collections
  receive explicit directories or config-file paths and do not resolve remote
  sources or infer the Toolang layout. `toolang.catalog.config` owns wired cap
  references and their round-trip TOML mutation; `toolang.catalog.types` owns
  shared authored-job vocabulary and defaults.
- `toolang.work` owns effective job scheduling state, file inbox requests,
  runtime stores, watchers, and scheduling loops.
- `toolang.work.schemas` owns caller-facing job protocol types;
  `toolang.work.types` owns scheduler status vocabulary;
  `toolang.work.records` owns durable scheduler entries; and
  `toolang.work.inspection` combines authored jobs with scheduler and execution
  state before constructing schemas.
- `toolang.state` owns remote cap source resolution, durable/prepared source
  snapshots, effective cap projection and materialization, immutable
  root/home/agent state, and source-state watching.
- `toolang.setup` owns immutable installed runtime setup snapshots, provider
  model discovery, and the multi-process-safe model-list cache under
  `${TOOLANG_ROOT}/.runtime/models/`. `AgentSetup` carries `providers`,
  `adapters`, `models`, `tools`, and `envs`; execution consumes this snapshot
  without provider discovery or cache access. `SetupWatcher` receives
  environment values through an explicit caller callback and publishes new
  snapshots for later runs.
- `toolang.state.schemas` owns caller-facing capability protocol types;
  its schema types construct themselves from prepared capability state;
  `toolang.state.types` owns capability-state vocabulary.
- `toolang.execution` owns run binding, execution trace, durable run truth,
  response projection, execution storage, and agent-specific built-in tools.
- `RunExecutor` owns run acceptance, control, and execution. It receives the
  process-owned `RunStore` and `IdIssuer`, constructs its mandatory internal
  `PersistSink`, and may receive one optional `RunTracer` per `start()`.
  `ThreadManager` shares the same store and issuer without depending on the
  executor. Persistence and control-status updates complete before the tracer
  observes an event. `RunTracer.on_event()` is async, serialized by the
  executor, and always awaited on the owner event loop.
- `toolang.execution.executor` contains `RunExecutor`, `RunSpec`, `RunHandle`,
  its mandatory `PersistSink`, shared run values, model-input preparation, and
  bounded diagnostics. Durable records, events, storage, inspection, schemas,
  and thread management remain at the `toolang.execution` package level.
- `RunSpec` carries the captured `AgentSetup` and `AgentState`, existing thread
  id, unique runnable name, canonical primary `Percept` input, optional model
  choice, and optional runnable `args`. Runnable declarations call their formal
  arguments `params`; runtime calls provide `args`. After runnable resolution,
  the executor uses language-owned input coercion to initialize typed `_` while
  preserving the original `Percept` in the start control. Run and request
  identities are `start()` arguments rather than executable input.
- Within `toolang.execution.executor`, `runs` owns complete agic and flow run
  bodies, `stmts` owns lowered flow-statement semantics, and `steps` owns step
  execution and event emission. Top-level runs have no synthetic containing
  step; recursive calls are wrapped by the invoking run step.
- Agic model-tool sequencing is fixed executor behavior. Do not add a loop
  plugin family or a plugin-facing run-context protocol.
- Agic and flow execution, model adapters, agent tools, and run tracers use
  async contracts. Sync Python tool callables may be isolated with
  `asyncio.to_thread()` at the function-tool boundary; do not move the agic
  loop itself into a worker thread.
- Prompt caps, agic bodies, and applicable flow statement bodies are
  `ContentBody` sources perceived as canonical `Percept` values. Language
  `Part` and `Part[]` variables remain percept parts rather than becoming text.
  Instruct and context declarations remain text templates and see only flat
  runtime variables. Body variables stay flat; do not add wrapper namespaces
  such as `args.*` or `runtime.*`.
- Templates are authored source concepts. Execution persistence stores their
  effective data in normalized model calls, with instructions, messages, and
  toolsets content-addressed; do not label model request data as template source.
- `toolang.execution.threads` owns synchronous thread creation, rewind, and
  fork semantics through `ThreadManager`. Create and fork return a new thread
  id; rewind mutates in place and returns nothing. Fork retains its anchor in
  inherited history, while rewind discards its anchor and the suffix after it.
  Anchors must be terminal. Fork may select an earlier terminal anchor while
  the source thread has a later active run; rewind requires an idle thread and
  never stops runs on the caller's behalf.
- `toolang.execution.schemas` owns caller-facing run, thread, step, and failure
  protocol types and reuses canonical message values from `toolang.base`;
  its schema types construct themselves from durable records, while
  `toolang.execution.history.RunHistory` provides the four store-backed read
  operations `list_threads()`, `get_thread()`, `list_runs()`, and `get_run()`. API
  code only serializes these schemas; CLI code reads them through the shared
  remote-or-local execution adapter and only renders them.
  `toolang.execution.types` owns shared execution lifecycle and run-control
  vocabulary.
- `toolang.plugin` owns generic entry point discovery, pure plugin configuration
  parsing, and independently reusable built-in tool, channel, sandbox,
  model-provider, and model-adapter implementations. It does not locate or read
  runtime config files and may depend only on `toolang.base` and
  `toolang.common` among internal packages.
- `toolang.api` owns FastAPI application assembly and HTTP route mapping.
  Route modules are grouped by public resource (`agent`, `chat`, `caps`,
  `jobs`, `runs`, and `threads`), not by read/write mode or OpenAPI tag. API
  routes call owning catalog, execution, and inspection objects instead of
  implementing persistence or projection algorithms.
- `toolang.cli` owns CLI orchestration, process environment resolution, dotenv
  loading, and process-level Web and logging call sites. Immutable root and home
  config layers are carried by `AgentState`; plugin packages own pure parsing of
  their explicit config sections.


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
- Persist run controls directly through `RunExecutor` and `RunStore`; runtime
  owns their application status. Persist run and step facts by sending
  `RunEvent` values through the executor's mandatory internal `PersistSink`.
- Run events are the complete source of durable run and step facts.
  `PersistSink` stores their references and output edges directly and never
  synthesizes steps or reconstructs runtime locals.
- Model steps persist a non-secret effective target snapshot and references
  that rebuild the normalized `ModelCall` accepted by `ModelAdapter`.
  Instructions, canonical messages, and toolsets are content-addressed in
  `runs.db`; provider-specific HTTP payloads, API keys, and request headers are
  not stored.
- Run and thread IDs use the shared file-backed allocator. Run-control and
  thread-control indexes are allocated and inserted in one SQLite transaction
  so all durable identities remain safe across local processes.
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

## Authored Catalog Writes

- `WiredCaps` must preserve unrelated TOML source, including comments.
- `WiredCaps`, `AuthoredCaps`, and `AuthoredJobs` serialize mutations with their
  public reentrant inter-process `write_lock()` mechanism.
- Authored job ids are globally unique across task and chore kinds and all
  stages. `toolang.work` allocates and persists ids missing from manually added
  files before publishing job state.
