# Design Documentation

These guides describe the current Toolang implementation for maintainers and
client/plugin implementers. Source and tests are authoritative. Repository,
testing, audit, and authoring standards live at the root; start with
[CONTRIBUTING.md](../CONTRIBUTING.md). User tutorials live on the
[documentation website](https://toolang.ai/docs).

## Reading paths

- System: [Architecture](architecture.md), [Layout](layout.md), [State](state.md),
  [Execution](execution.md).
- Language: [Program](program.md), [AST](ast.md), [Input syntax](input-syntax.md),
  [Call Input](call-input.md), [Flow](flow.md), [Agic](agic.md).
- Integration: [Plugins](plugins.md), [CLI](cli.md), [HTTP API](api.md),
  [Events](events.md), [Messaging](messaging.md).

## Design owners

| Guide | Owns |
| --- | --- |
| [Architecture](architecture.md) | Terms, package responsibilities, source identity, placement, and hosting boundaries |
| [Layout](layout.md) | Authored files, State revisions, runtime data, discovery, and locks |
| [State](state.md) | Parsed layers, publication, accepted sources, immutable revisions, and adoption |
| [Program](program.md) | Modules, declarations, signatures, types, and documentation binding |
| [AST](ast.md) | Semantic nodes, lowering, static validation, and serialization |
| [Input syntax](input-syntax.md) | Content evaluation, Chat settings, and one-run overrides |
| [Call Input](call-input.md) | Flat mappings, capture boundaries, prompt expansion, and coercion |
| [Flow](flow.md) | Binding, calls, arrays, loops, async work, spawn, and exec |
| [Agic](agic.md) | Model/tool loop, instruction assembly, guidance, output recovery, and compaction |
| [Caps](caps.md) | Composable agent primitives, source refs, scope, precedence, and projections |
| [Queries](queries.md) | Public resource records and native TQ matching |
| [Models](models.md) | Catalogs, route resolution, model settings, adapters, and accounting |
| [Tools](tools.md) | User/runtime tools, workspaces, services, State edits, and conversations |
| [Plugins](plugins.md) | Toolset, sandbox, catalog, and adapter contracts and loading |
| [Scripts](scripts.md) | Source-local configuration, workspace grants, and input origins |
| [Source commands](source-commands.md) | Offline parse, CST inspection, formatting, highlighting, and diagnostics |
| [Execution](execution.md) | Admission, lifecycle, limits, controls, threads, and State capture |
| [Executor](executor.md) | Execution internals, async ownership, terminalization, and resident LiveTree |
| [Records](records.md) | Durable records, values, references, and historical inspection |
| [Events](events.md) | Subscriber payloads, post-commit delivery, cursors, SSE recovery, and Hub observation |
| [Drivers](drivers.md) | Chat/Talk/Chore activation, handler selection, scheduling, and recovery |
| [Chores](chores.md) | Named definitions, deterministic identity, checkpoints, and manual occurrences |
| [Chat](chat.md) | Session state, queued submissions, steering, and remote recovery |
| [Execution presentation](execution-presentation.md) | Shared progress projection, terminal geometry, and scrollback |
| [Messaging](messaging.md) | Conversations, shared rules, Talk consumption, Hub APIs, and backend storage |
| [IDs](ids.md) | Agent-keyed allocation, deterministic chore IDs, and conversation codecs |
| [Execution statistics](execution-statistics.md) | Incremental usage cache, query windows, and archival |
| [Top](top.md) | Current/recent observation, coverage, totals, and dashboard interaction |
| [HTTP API](api.md) | Agent application composition, requests, responses, and driver-gated routes |
| [Hosting](hosting.md) | Sandbox processes, installation, captured environment, logging, discovery, and cleanup |
| [CLI](cli.md) | Command routing, inspection, server acquisition, and terminal metadata |

## Plans and reference

[Plans](plans/) record proposed or approved changes and their acceptance criteria.
Their existence does not establish that a feature is implemented. The current
design guides describe the result of implemented changes; plans are maintained
separately. [Generated reference](../reference/README.md) provides module-level
details directly from source.
