# Refactor Target

This document defines the target package structure and ownership boundaries for
the next Toolang refactor. It is an implementation target, not a description of
the current source tree. Stable runtime behavior remains defined by the other
design documents in this directory.


## Goals

- make the core runtime visible through a small set of concrete classes
- keep orchestration classes small by moving invariants onto the concepts that
  own them
- separate authored files, immutable state, durable runtime truth, and live
  execution
- let each run use one immutable agent-state version, including all child runs
- let TUI, scripts, the HTTP API, and the scheduler use the same executor
- support multiple executor processes without requiring a central daemon or
  event bus
- reduce the CLI root to command assembly and global argument handling
- remove compatibility layers once their callers have moved


## Naming Rules

The following suffixes have specific meanings:

- `Catalog`: CRUD over authored filesystem entries
- `Store`: durable SQLite truth or a durable runtime projection
- `State`: an immutable in-memory snapshot
- `Watcher`: source changes projected into immutable state
- `Sink`: values projected into one destination
- `Layout`: filesystem placement and path construction

Avoid generic `Service`, `Factory`, and `Utils` types. Use `Manager` only for a
concept such as `ThreadManager` that owns state transitions and orchestration
invariants. A class should exist only when it owns state, invariants, or a
meaningful protocol.


## Target Packages

```text
toolang/
├── api/                    # FastAPI application and resource routes
├── base/                   # plugin-facing protocols and shared value types
├── catalog/                # authored agent, cap, and job CRUD plus templates
├── cli/                    # CLI entry points and user interfaces
├── common/                 # small package-neutral primitives
├── execution/              # requests, records, events, stores, and executor
├── up/                     # runtime setup, agent targets, processes, sandboxes, API assembly
├── lang/                   # .too AST, parsing, lowering, validation, formatting
├── plugin/                 # plugin loading, config, and built-in implementations
├── state/                  # durable/prepared files and immutable agent state
└── work/                   # effective jobs, file requests, watchers, stores, scheduling
```

Packages should contain concept families rather than one file per class. Most
modules should remain small enough to understand in isolation, but line count
alone is not a reason to add another forwarding layer.


## Program

`toolang.lang` owns the `.too` source format and its authored-source semantics.
The separate `toolang.program` facade is removed.

```text
lang/
├── ast.py
├── input.py
├── lower.py
├── validate.py
└── format.py
```

The primary value is `Program`. It contains cap and job declarations plus
agics, flows, contexts, instructs, and structs. AST declaration names should
match the language vocabulary, including `JobDecl`, `AgicDecl`, `FlowDecl`, and
the concrete flow statement nodes.

AST construction owns the single parser entry point. Lowering, validation,
input expansion, and formatting remain separate operations.
General program source-editing APIs are deferred; source files may continue to
be edited directly until a concrete editing API is needed.


## Authored Catalogs

Catalogs modify source files only. A catalog mutation does not directly mutate
a watcher snapshot or a runtime store.

Bundled authored-file templates live under `catalog.templates` because they
seed catalog-owned agent, cap, and job files. They are not a separate runtime
domain.

The canonical collections are `catalog.agent.LocalAgents`,
`catalog.cap.AuthoredCaps`, `catalog.config.ConfiguredCaps`, and
`catalog.job.AuthoredJobs`.

### LocalAgents

`LocalAgents` manages resident agent homes under one explicitly supplied
directory:

- `list()`
- `get()`
- `create()`
- `rename()`
- `remove()`

It works directly with agent names and home paths; there is no `LocalAgent`
model. Remote source resolution, cloning orchestration, and visiting or roaming
materialization belong to `toolang.up` and the CLI. Process start, stop, and
status belong to `AgentProcess`.

`create()` always receives explicit authored content. Callers that want a
bundled starting point load or render it through `catalog.templates`; catalog
CRUD does not keep a second inline default template or rewrite the supplied
content to match the catalog key.

### AuthoredCaps And ConfiguredCaps

`AuthoredCaps` receives one cap directory and manages `CapFile` values.
`ConfiguredCaps` receives one config-file path and manages `CapRef` values. Both
provide:

- `list()`
- `get()`
- `create()`
- `update()`
- `remove()`

`remove()` returns the removed value. Remote reference resolution, content
fetching, caching, and effective-cap materialization belong to `toolang.state`.
`ConfiguredCaps` edits its TOML tables with a round-trip parser so unrelated config,
formatting, and comments remain authored source rather than being regenerated.
All three file-backed collections expose `write_lock()` and use that same
reentrant inter-process lock internally for mutations. Callers may hold the lock
around a larger multi-operation transaction.

### AuthoredJobs

`AuthoredJobs` receives one agent-home directory and manages `JobFile` values:

- `list()`
- `get()`
- `create()`
- `update()`
- `move()`
- `remove()`

`JobFile.stage` records folder placement as `draft`, `ready`, or `archived`.
Stable identity remains in `JobFile.meta["id"]`; `title` is the only optional
display label. The catalog enforces id uniqueness across kinds and stages but
does not allocate ids or write scheduler state. `toolang.work` allocates and
persists missing ids before publishing ready-job state.


## Agent State

Agent source state is represented by three immutable values:

```text
HomePrepared = home source + resolution + config + Program + home caps
RootPrepared = root source + resolution + config + shared caps
AgentState   = one exact RootPrepared + HomePrepared pair
```

`AgentState` contains the effective config, authored program source path, exact
`Program`, and effective caps used by execution. It does not contain a separate
jobs collection; program-declared jobs remain available as
`AgentState.program.jobs`.

`StateWatcher` monitors the relevant files and publishes new immutable
`AgentState` versions. It owns invalidation and reuse of unchanged parsed
sources. It does not know about `RunExecutor` or `JobScheduler`. Every API process
that can accept runs starts its watcher as process infrastructure; watching is
not an optional runtime component.

`toolang.setup.AgentSetup` is separate from source state and contains
process-local identity, placement, installed implementations, the available
model snapshot, and resolved environment values:

```python
@dataclass(frozen=True, slots=True)
class AgentSetup:
    name: str
    home: Path
    providers: Mapping[str, Provider]
    adapters: Mapping[str, ModelAdapter]
    models: tuple[ModelInfo, ...]
    tools: Mapping[str, AgentTool]
    envs: Mapping[str, str]
```

Effective service definitions remain in the `AgentState` captured for each
run. Run assembly passes those definitions and their explicitly resolved
environment values from `AgentSetup` through `ToolContext`.

There is no general `toolang.config` package. Prepared root and home config are
part of `AgentState`. The CLI resolves environment variables and process
settings at the call site; plugin families parse their own sections from
explicit config mappings.


## Durable Jobs

Job definitions and mutable scheduling state remain separate:

```text
Job          = immutable effective task or chore definition
JobRecord    = mutable scheduler checkpoint in jobs.db
Run          = durable execution fact in runs.db
```

`JobWatcher` publishes immutable ready-file snapshots. `JobScheduler` merges
them with current program jobs into a map keyed only by globally unique job id.
Duplicate definitions are invalid; sources do not silently shadow one another.

`JobStore` owns current revision state, RRULE cursors, active dispatch
checkpoints, and atomic claims. Run counters, results, and history remain in
`runs.db`.

`JobScheduler` owns a dedicated thread and event loop after `start()`. The API
loop remains the sole owner of `RunExecutor` and every run task. The scheduler
submits one coroutine through a thread-safe future; that coroutine invokes and
awaits `RunExecutor.start()` only on the execution loop. Execution has no
scheduler callback or dependency.

The scheduler uses one in-memory due heap and point-updates `jobs.db`; it does
not scan SQLite to discover ready work. It preallocates a run id, persists an
active checkpoint, and posts an immutable `RunSpec` to the execution loop.
Startup repairs only checkpointed active run ids from `runs.db`.


## Execution

The public execution concepts are:

- `RunSpec`: immutable setup, state, thread, effective bindings and limits,
  ceiling restrictions, primary `Percept`, and named inputs
- `LocalRunHandle`: an awaitable locally started run with control conveniences
- `RunExecutor`: run acceptance, control, and agic/flow execution
- `ThreadManager`: synchronous thread creation, rewind, and fork orchestration
- `RunEvent`: the complete ordered execution event stream
- `RunStore`: thread controls, run controls, runs, steps, and transcript messages
- private run-event projection: mandatory run/step persistence into `RunStore`
- `RunTracer`: optional per-start observation of live run events

`RunExecutor.start()` receives one `RunSpec` carrying explicit immutable
`AgentSetup` and `AgentState` values for each top-level run. It does not know
`StateWatcher`, jobs, CLI, or HTTP.

`ThreadManager` owns chat-thread creation and branching rules. It never writes
run controls or depends on the executor, and thread operations never spawn
follow-up runs. Create and fork return a thread id; rewind mutates an idle
thread in place. `RunExecutor` does not import or expose thread operations.

At run entry, the caller captures the current `AgentState`. All child runs use
that same state version. File changes produce a newer state only for later
top-level runs.

`RunExecutor.start()` is the external execution entry point. It uses mandatory
internal persistence and an optional per-start tracer. Internal child runs
reuse private runtime logic and never call the public entry point. Runtime
owners retain or await the returned `LocalRunHandle`; `RunExecutor` does not
provide `spawn()`.

The target execution package is:

```text
execution/
├── types.py                # execution lifecycle vocabulary
├── records.py              # durable thread, run, control, and step truth
├── schemas.py              # protocol types and pure record conversion
├── history.py              # caller-facing durable run history
├── events.py               # RunEvent, RunTracer, and thread events
├── store.py                # RunStore
├── threads.py              # ThreadManager
├── tools/                  # agent-specific built-in tools
└── executor/               # RunExecutor and execution implementation helpers
    ├── __init__.py         # RunExecutor, RunSpec, and LocalRunHandle exports
    ├── executor.py         # public run contract and private per-start _Execution
    ├── common.py           # bound runs, locals, and shared execution helpers
    ├── prepare.py          # agic resolution and complete model-input preparation
    ├── diagnostics.py      # bounded model and tool diagnostics
    ├── _persist.py         # private run-event projection
    ├── runs/               # agic and flow run bodies
    ├── steps/              # event-owning run, model, tool, and value steps
    ├── stmts/              # lowered flow-statement semantics
    └── prompts/            # default execution prompt resources
```

A process owns one shared `RunStore` and `IdIssuer` for an agent and passes both
to its `RunExecutor` and `ThreadManager`. Multiple processes may execute against
the same agent, so file-backed ID issuance and SQLite transactions remain
process-safe.

A general queue limiting the number of top-level runs is not part of the
target. Future resource control belongs near model providers and should limit
concurrency, rate, token use, or cost according to the constrained resource.


## CLI

The CLI has four first-level areas with one-way dependencies:

```text
toolang/cli/
├── toolang/                # too/toolang CLI
│   ├── main.py
│   ├── __main__.py         # thin python -m adapter
│   └── commands/
│       ├── agent.py
│       ├── runtime.py
│       ├── chat.py
│       ├── thread.py
│       ├── job.py
│       ├── plugin.py
│       └── program.py
├── caps/                   # reusable cap commands and standalone caps CLI
│   ├── main.py
│   ├── __main__.py         # thin python -m adapter
│   └── commands.py
├── impl/                   # interactive implementations, never CLI entry points
│   └── chat/               # prompt-toolkit TUI
└── common/                 # infrastructure shared by two or more CLI areas
    ├── context.py
    ├── routing.py
    ├── client.py
    ├── progress.py
    └── output.py
```

Responsibilities are:

- `toolang`: root Typer setup, global options, explicit command registration,
  and final CLI error handling
- `toolang.commands`: Typer parameters, calls to concept objects, and
  presentation
- `toolang.commands.script`: local script command, executable argument
  coercion, generated Typer help, and direct `RunExecutor.start()` orchestration
- `toolang.commands.chat`: process-local chat orchestration, interactive TUI
  state, mutable blocks, widgets, and slash commands
- `caps`: one cap command implementation reused by `too` and the standalone
  `caps` entry point
- `common`: only code used by at least two first-level CLI areas

`RuntimeClient` owns semantic HTTP and SSE operations such as `start_run()`,
`stop_run()`, `steer_run()`, `list_runs()`, and `stream_events()`. Raw request
helpers must not remain scattered through command modules.

CLI command functions should resolve inputs, call `Catalog`, `Store`,
`RunExecutor`, `AgentProcess`, or `RuntimeClient`, and format the returned values.
They must not own source-file formats, SQL, state merging, or execution rules.

The entry points become:

```toml
toolang = "toolang.cli.toolang.main:main"
too = "toolang.cli.toolang.main:main"
caps = "toolang.cli.caps.main:main"
```

The CLI dependency direction is:

```text
cli.common <- cli.caps
cli.common <- cli.toolang

cli.toolang.commands.chat    -> cli.common
cli.toolang                  -> cli.caps.commands
```

There is no separate implementation package. Command-specific implementation
stays with its command package, while shared CLI behavior belongs in
`cli.common`.
The existing generic CLI `utils.py` is removed as its routing, output, context,
and owner-specific functions move to their named modules.


## Process Assembly

`toolang.up.server` resolves startup configuration and assembles concrete
objects. It may construct watchers, setup, stores, executor, scheduler, and API
state, but it must not wrap them in an `AgentRuntime` aggregate.

The CLI is the only public process origin. It first resolves one materialized
agent target, then selects a sandbox from `--sandbox` or explicit config. Sandbox
decides whether the API process runs on the host, in Docker, or in a future
remote or hybrid environment. The server core and `RunExecutor` do not inspect
that sandbox decision.

`toolang.api.app` owns `ApiContext`, its `app.state` registration and request
dependency, and FastAPI application assembly. `toolang.api.router` mounts
versioned resource routers under the shared `/api/v1` prefix. Resource modules
under `toolang.api.routers`, such as `agent`, `chat`, `caps`, `jobs`, `runs`,
and `threads`, own HTTP request mapping and export module-level `APIRouter`
instances. Route functions receive the shared `ApiContext` dependency and use
its fields directly. `common` owns shared transport helpers. Typed inspection
projections belong to the core package that owns the inspected state rather
than to an API-wide view module. Process-level routes such as `/healthz` remain
on the application itself.

Scripts with `sandbox=host` may assemble an executor for one-shot invocation.
Managed-sandbox script runs transport native run events through their
process/channel orchestration rather than an HTTP streaming endpoint. The chat
TUI runs in its own process, assembles the core objects and `RunExecutor`
directly, and observes native events through a `RunTracer`; it does not depend
on API streaming endpoints. A central runtime event bus is not required by the
target architecture.


## Dependency Direction

The intended package dependency direction is:

```text
base/common
    -> lang/catalog/plugin
    -> state and work.state
    -> execution foundations
    -> RunExecutor
    -> JobScheduler
    -> api, up, and CLI
```

Additional constraints:

- `execution` does not import `work`
- `work.scheduler` may import `execution`
- watchers do not import their consumers
- stores do not import CLI, HTTP, or UI modules
- `RunExecutor` does not import `StateWatcher`
- concrete internal modules are imported directly; package facades stay narrow


## Current Types To Retire

The refactor should remove, rather than preserve aliases for, superseded
concepts:

- `PreparedProgram`, `LiveProgram`, and `LiveState` are replaced by `Program`
  and immutable home/root/agent state
- `ExecutionStore` becomes the focused `RunStore`
- legacy queue-runner, submission-wrapper, binding, and outcome types are not
  public target concepts
- `RuntimeEventBus` is not required for local execution or persistence
- effective jobs leave general agent state and become immutable `Job` values
  maintained directly by `JobScheduler`
- `toolang.lang` becomes the canonical language package and the
  `toolang.program` facade is removed
- `toolang.cli.toolang` and `toolang.cli.caps` are the two executable CLI
  packages; interactive chat lives under
  `toolang.cli.toolang.commands.chat`
- duplicate cap command registration and forwarding modules are removed

No compatibility wrappers should remain once all in-repository callers use the
target APIs.


## Migration Order

1. Consolidate language behavior in `toolang.lang` and introduce the focused
   agent, cap, and job value types and catalogs.
2. Replace prepared/live state layers with `Source`, `HomePrepared`,
   `RootPrepared`, `AgentState`, and their watcher.
3. Extract `RunStore`, persistence, reply sinks, and the final executor entry
   point from the current execution stack.
4. Introduce `Job`, `JobWatcher`, `JobStore`, and the self-driven
   `JobScheduler`.
5. Move CLI behavior into the five first-level CLI areas and reduce the root
   app to explicit assembly.
6. Move process assembly to `up.server` and HTTP assembly to `api.app`.
7. Move plugin families under `plugin`, authored CRUD under `catalog`, and
   durable job scheduling under `work`.
8. Delete superseded modules and compatibility paths, then update the stable
   design documents and generated reference.

Each step should leave the repository fully tested and should remove old code
as soon as all callers have moved.


## Completion Criteria

The refactor is complete when:

- each core concept has one owner and one canonical type
- `RunExecutor` runs against an explicit immutable `AgentState`
- child runs cannot observe later source changes
- `JobScheduler` depends only on its watcher, store, executor, and immutable
  setup/state accessors
- authored CRUD is implemented through catalogs rather than CLI file editing
- run and job persistence are separate and safe across processes
- TUI, script, HTTP, and scheduled execution share the same executor behavior
- the main CLI file shows the complete command surface without implementing
  command behavior
- no target package depends on CLI or process assembly
- superseded prepared/live/queue/event-bus abstractions have been removed
