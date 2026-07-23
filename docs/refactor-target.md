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
- `Sink`: events projected into persistence or a reply
- `Layout`: filesystem placement and path construction

Avoid generic `Manager`, `Service`, `Factory`, and `Utils` types. A class should
exist only when it owns state, invariants, or a meaningful protocol.


## Target Packages

```text
toolang/
├── api/                    # FastAPI application and resource routes
├── base/                   # plugin-facing protocols and shared value types
├── catalog/                # authored agent, cap, and job CRUD plus templates
├── cli/                    # CLI entry points and user interfaces
├── common/                 # small package-neutral primitives
├── execution/              # requests, records, events, stores, and executor
├── up/                     # runtime setup, agent targets, processes, hosting, API assembly
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
`catalog.cap.AuthoredCaps`, `catalog.config.WiredCaps`, and
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

### AuthoredCaps And WiredCaps

`AuthoredCaps` receives one cap directory and manages `CapFile` values.
`WiredCaps` receives one config-file path and manages `CapRef` values. Both
provide:

- `list()`
- `get()`
- `create()`
- `update()`
- `remove()`

`remove()` returns the removed value. Remote reference resolution, content
fetching, caching, and effective-cap materialization belong to `toolang.state`.
`WiredCaps` edits its TOML tables with a round-trip parser so unrelated config,
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
Stable identity remains in `JobFile.meta["id"]`, while the authored logical name
is `JobFile.meta["name"]`. The catalog enforces id uniqueness across kinds and
stages but does not allocate ids or write scheduler state. `toolang.work`
allocates and persists missing ids before publishing home-job state.


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
sources. It does not know about `RunExecutor` or `Scheduler`. Every API process
that can accept runs starts its watcher as process infrastructure; watching is
not an optional runtime component.

`toolang.up.setup.AgentSetup` is separate from source state and contains
process-local identity, placement, installed implementations, resolved
environment values, and model activation limits:

```python
@dataclass(frozen=True, slots=True)
class AgentSetup:
    name: str
    home: Path
    tools: Mapping[str, AgentTool]
    model_providers: Mapping[str, ModelProvider]
    model_adapters: Mapping[str, ModelAdapter]
    model_environ: Mapping[str, str]
    model_selectors: tuple[str, ...] = ()
    model_cache_dir: Path | None = None
```

Effective service definitions remain in the `AgentState` captured for each
run. Run assembly passes those definitions and their explicitly resolved
environment values from `AgentSetup` through `ToolContext`.

There is no general `toolang.config` package. Prepared root and home config are
part of `AgentState`. The CLI resolves environment variables and process
settings at the call site; plugin families parse their own sections from
explicit config mappings.


## Durable Work

Job definitions and mutable scheduling state remain separate:

```text
HomeJobs  = snapshot of authored task and chore files
AgentJobs = merge(HomeJobs, AgentState.program.jobs)
JobStore  = mutable scheduler projection in jobs.db
```

`JobWatcher` publishes immutable `HomeJobs` snapshots. `AgentJobs.merge()` owns
definition precedence and duplicate validation.

`JobStore` owns durable job status, schedules, counters, last and next run
references, and atomic claims. It must be safe when multiple processes inspect
or attempt to claim jobs.

`Scheduler` is self-driven after `start()`. It receives state accessors rather
than watcher objects:

```python
Scheduler(
    job_store=job_store,
    executor=executor,
    get_home_jobs=job_watcher.current,
    get_agent_state=state_watcher.current,
)
```

For one scheduling pass it captures one `AgentState`, merges `AgentJobs` from
that state and the current `HomeJobs`, atomically claims due work, and submits
`RunRequest` values to `RunExecutor`. `Scheduler` persists job state through
`JobStore`; `RunExecutor` persists run state through `RunStore`.

Runs link to their originating job through run origin metadata. `RunStore`
does not duplicate job records.

File inbox requests use the same `toolang.work` ownership boundary but retain
their independent `files.db` store. `jobs.db` and `files.db` are not merged by
this refactor; a unified work record and store require a separate persistence
design and migration.


## Execution

The public execution concepts are:

- `RunRequest`: an external request to execute an agic or flow
- `RunExecutor`: run acceptance, control, and agic/flow execution
- `ThreadManager`: synchronous thread creation, rewind, and fork orchestration
- `RunEvent`: the complete ordered execution event stream
- `RunStore`: thread controls, run controls, runs, steps, and transcript messages
- `PersistSink`: mandatory internal run/step projection into `RunStore`
- `RunTracer`: optional per-start observation of live run events

`RunExecutor.start()` receives explicit immutable `AgentSetup` and `AgentState`
values for each top-level run. It does not know `StateWatcher`, jobs, CLI, or
HTTP.

`ThreadManager` owns chat-thread creation and branching rules. Rewind may
write durable stop controls through the shared store, but it never depends on
the executor and thread operations never spawn follow-up runs. Create and fork
return a thread id; rewind mutates in place. `RunExecutor` does not import or
expose thread operations.

At run entry, the caller captures the current `AgentState`. All child runs use
that same state version. File changes produce a newer state only for later
top-level runs.

`RunExecutor.start()` is the external execution entry point. It uses mandatory
internal persistence and an optional per-start tracer. Internal child runs
reuse private runtime logic and never call the public entry point. Runtime
owners decide whether to await `start()` or create and retain a background
task; `RunExecutor` does not provide `spawn()`.

The target execution package is:

```text
execution/
├── types.py                # execution lifecycle vocabulary
├── records.py              # durable thread, run, control, and step truth
├── schemas.py              # protocol types and pure record conversion
├── inspection.py           # store-backed aggregate inspection
├── events.py               # RunEvent, RunTracer, and thread events
├── store.py                # RunStore
├── threads.py              # ThreadManager
├── tools/                  # agent-specific built-in tools
└── executor/               # RunExecutor and execution implementation helpers
    ├── __init__.py         # stable RunExecutor entry point
    ├── request.py          # RunRequest and executable kind
    ├── executor.py         # RunExecutor and private per-start _Execution
    ├── common.py           # bound runs, locals, and shared execution helpers
    ├── prepare.py          # agic resolution and complete model-input preparation
    ├── diagnostics.py      # bounded model and tool diagnostics
    ├── persist.py          # mandatory internal PersistSink
    ├── runs/               # agic and flow run bodies
    ├── steps/              # event-owning run, model, tool, and system steps
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
│   ├── cli.py
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
│   ├── cli.py
│   ├── __main__.py         # thin python -m adapter
│   └── commands.py
├── impl/                   # command implementations, never CLI entry points
│   ├── invoke/
│   │   ├── runner.py
│   │   ├── request.py
│   │   ├── help.py
│   │   └── rendering.py
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
- `impl.invoke`: local script invocation, executable argument coercion,
  generated help, and trace progress
- `impl.chat`: interactive TUI state, mutable blocks, widgets, and slash
  commands
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
cli.common <- cli.impl.invoke
cli.common <- cli.impl.chat
cli.common <- cli.caps
cli.common <- cli.toolang

cli.toolang.commands.runtime -> cli.impl.invoke
cli.toolang.commands.chat    -> cli.impl.chat
cli.toolang                  -> cli.caps.commands
```

`impl`, `caps`, and `common` never import `toolang`. The existing generic CLI
`utils.py` is removed as its routing, output, context, and owner-specific
functions move to their named modules.


## Process Assembly

`toolang.up.server` resolves startup configuration and assembles concrete
objects. It may construct watchers, setup, stores, executor, scheduler, and API
state, but it must not wrap them in an `AgentRuntime` aggregate.

The CLI is the only public process origin. It first resolves one materialized
agent target, then selects hosting from `--sandbox` or explicit config. Hosting
decides whether the API process runs on the host, in Docker, or in a future
remote or hybrid environment. The server core and `RunExecutor` do not inspect
that hosting decision.

`toolang.api.app` owns `ApiContext`, API-owned run tasks, durable run-submission
acknowledgment, and FastAPI application assembly. `toolang.api.router` mounts
versioned resource routers under the shared `/api/v1` prefix. Resource modules
under `toolang.api.routers`, such as `agent`, `chat`, `caps`, `jobs`, `runs`,
and `threads`, own HTTP request mapping and export a router factory bound to the
application context. `common` owns shared transport helpers. Typed inspection
projections belong to the core package that owns the inspected state rather
than to an API-wide view module. Process-level routes such as `/healthz` remain
on the application itself.

Scripts with `sandbox=none` may assemble an executor for one-shot invocation.
Managed-sandbox script runs use a session-owned API and stream canonical trace
events back to the CLI. Chat reuses an existing API when present. Without one,
`sandbox=none` uses a process-local executor and managed hosting owns a temporary
API for the session. A central runtime event bus is not required by the target
architecture.


## Dependency Direction

The intended package dependency direction is:

```text
base/common
    -> lang/catalog/plugin
    -> state and work.state
    -> execution foundations
    -> RunExecutor
    -> Scheduler
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
- `QueueRunner`, `RunSubmission`, `RunBinding`, and `RunOutcome` are not public
  target concepts
- `RuntimeEventBus` is not required for local execution or persistence
- jobs leave general agent state and become `HomeJobs` plus `AgentJobs`
- `toolang.lang` becomes the canonical language package and the
  `toolang.program` facade is removed
- `toolang.cli.toolang` and `toolang.cli.caps` are the two executable CLI
  packages; chat and invoke live under `toolang.cli.impl`
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
4. Introduce `HomeJobs`, `AgentJobs`, `JobWatcher`, `JobStore`, and the
   self-driven `Scheduler`.
5. Move CLI behavior into the five first-level CLI areas and reduce the root
   app to explicit assembly.
6. Move process assembly to `up.server` and HTTP assembly to `api.app`.
7. Move plugin families under `plugin`, authored CRUD under `catalog`, and
   durable job/file processing under `work`.
8. Delete superseded modules and compatibility paths, then update the stable
   design documents and generated reference.

Each step should leave the repository fully tested and should remove old code
as soon as all callers have moved.


## Completion Criteria

The refactor is complete when:

- each core concept has one owner and one canonical type
- `RunExecutor` runs against an explicit immutable `AgentState`
- child runs cannot observe later source changes
- `Scheduler` depends only on stores, executor, and state accessors
- authored CRUD is implemented through catalogs rather than CLI file editing
- run and job persistence are separate and safe across processes
- TUI, script, HTTP, and scheduled execution share the same executor behavior
- the main CLI file shows the complete command surface without implementing
  command behavior
- no target package depends on CLI or process assembly
- superseded prepared/live/queue/event-bus abstractions have been removed
