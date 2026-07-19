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
├── agent/                  # runtime materialization, processes, tools, and assembly
├── api/                    # FastAPI application and resource routes
├── base/                   # plugin-facing protocols and shared value types
├── catalog/                # authored agent, cap, and job CRUD plus templates
├── cli/                    # CLI entry points and user interfaces
├── common/                 # small package-neutral primitives
├── config/                 # runtime configuration and logging
├── execution/              # requests, records, events, stores, replies, executor
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
`catalog.cap.AuthoredCaps`, `catalog.cap.WiredCaps`, and
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
materialization belong to `toolang.agent` and the CLI. Process start, stop, and
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
HomeState  = agent config + Program + agent-home caps
RootState  = root config + root caps
AgentState = merge(HomeState, RootState)
```

`AgentState` contains the effective config, the exact `Program`, and effective
caps used by execution. It does not contain a separate jobs collection;
program-declared jobs remain available as `AgentState.program.jobs`.

`StateWatcher` monitors the relevant files and publishes new immutable
`AgentState` versions. It owns invalidation and reuse of unchanged parsed
sources. It does not know about `Executor` or `Scheduler`.

`AgentSetup` is separate from source state and contains installed runtime
implementations:

```python
@dataclass(frozen=True, slots=True)
class AgentSetup:
    tools: Mapping[str, ToolPlugin]
    model_providers: Mapping[str, ModelProvider]
    model_adapters: Mapping[str, ModelAdapter]
```

Process-level component selection is startup configuration and is not part of
`AgentSetup` or `AgentState`.


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
`RunRequest` values to `Executor`. `Scheduler` persists job state through
`JobStore`; `Executor` persists run state through `RunStore`.

Runs link to their originating job through run origin metadata. `RunStore`
does not duplicate job records.

File inbox requests use the same `toolang.work` ownership boundary but retain
their independent `files.db` store. `jobs.db` and `files.db` are not merged by
this refactor; a unified work record and store require a separate persistence
design and migration.


## Execution

The public execution concepts are:

- `RunRequest`: an external request to execute an agic or flow
- `Executor`: agic, flow, and step execution
- `TraceEvent`: the complete ordered execution event stream
- `RunStore`: threads, commands, runs, steps, parts, and transcript messages
- `PersistSink`: trace events projected into `RunStore`
- `ReplySink`: trace events projected into a caller-facing reply

`Executor` receives `AgentSetup` when constructed and an explicit immutable
`AgentState` for each top-level run. It does not know `StateWatcher`, jobs, CLI,
or HTTP.

At run entry, the caller captures the current `AgentState`. All child runs use
that same state version. File changes produce a newer state only for later
top-level runs.

`Executor.run()` is the external entry point. It combines the persistent event
sink with an optional per-run reply handler before execution begins. Internal
child runs reuse private executor logic and never call the public entry point.
Records are derived from trace events; execution locals do not hold references
to durable command records.

The target execution package is:

```text
execution/
├── setup.py                # AgentSetup
├── request.py              # RunRequest
├── records.py              # StepPath and durable record types
├── events.py               # trace event dataclasses and TraceEvent
├── store.py                # RunStore and PersistSink
├── reply.py                # ReplySink implementations
├── agic.py                 # internal model/tool loop support
└── executor.py             # Executor
```

A process may own one `Executor` and one `AgentSetup`, but multiple processes
may execute against the same agent. Toolang-owned ID allocation and SQLite
transactions must therefore be process-safe.

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
`Executor`, `AgentProcess`, or `RuntimeClient`, and format the returned values.
They must not own source-file formats, SQL, state merging, or execution rules.

The entry points become:

```toml
toolang = "toolang.cli.toolang.cli:main"
too = "toolang.cli.toolang.cli:main"
caps = "toolang.cli.caps.cli:main"
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

`toolang.agent.runtime` resolves startup configuration and assembles concrete
objects. It may construct watchers, setup, stores, executor, scheduler, and API
state, but it must not wrap them in an `AgentRuntime` aggregate.

`toolang.api.app` owns FastAPI application and route assembly. Resource modules
such as `chat`, `runs`, `jobs`, and `caps` own HTTP request mapping;
`_streaming` and `_views` are internal API implementation modules.

Direct TUI and script execution may assemble the same objects without a daemon.
The HTTP server is another caller of the executor, not the owner of execution.
A central runtime event bus is not required by the target architecture.


## Dependency Direction

The intended package dependency direction is:

```text
base/common
    -> lang/config/catalog/plugin
    -> state and work.state
    -> execution foundations
    -> Executor
    -> Scheduler
    -> api, agent runtime, and CLI
```

Additional constraints:

- `execution` does not import `work`
- `work.scheduler` may import `execution`
- watchers do not import their consumers
- stores do not import CLI, HTTP, or UI modules
- `Executor` does not import `StateWatcher`
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
2. Replace prepared/live state layers with `HomeState`, `RootState`,
   `AgentState`, and their watcher.
3. Extract `RunStore`, persistence, reply sinks, and the final executor entry
   point from the current execution stack.
4. Introduce `HomeJobs`, `AgentJobs`, `JobWatcher`, `JobStore`, and the
   self-driven `Scheduler`.
5. Move CLI behavior into the five first-level CLI areas and reduce the root
   app to explicit assembly.
6. Move process assembly to `agent.runtime` and HTTP assembly to `api.app`.
7. Move plugin families under `plugin`, authored CRUD under `catalog`, and
   durable job/file processing under `work`.
8. Delete superseded modules and compatibility paths, then update the stable
   design documents and generated reference.

Each step should leave the repository fully tested and should remove old code
as soon as all callers have moved.


## Completion Criteria

The refactor is complete when:

- each core concept has one owner and one canonical type
- `Executor` runs against an explicit immutable `AgentState`
- child runs cannot observe later source changes
- `Scheduler` depends only on stores, executor, and state accessors
- authored CRUD is implemented through catalogs rather than CLI file editing
- run and job persistence are separate and safe across processes
- TUI, script, HTTP, and scheduled execution share the same executor behavior
- the main CLI file shows the complete command surface without implementing
  command behavior
- no target package depends on CLI or process assembly
- superseded prepared/live/queue/event-bus abstractions have been removed
