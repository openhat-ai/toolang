# Design Documentation

These guides describe the current implementation, organized by design
responsibility. Source and tests are authoritative. Repository standards live
at the root; start with [CONTRIBUTING.md](../CONTRIBUTING.md). User tutorials
live on the [documentation website](https://toolang.ai/docs).

Read the overview first, then the relevant chapter. Files stay flat; the
chapters below express design responsibilities rather than Python packages.

## System overview

- [Architecture](architecture.md): concepts, package boundaries, source identity, placement, and the path from authored input to execution.

## Programming model

- [Program](program.md): modules, declarations, signatures, types, and shared directives.
- [Content](content.md): authored text, prompt/include expansion, templates, and message Parts.
- [Call Input](call-input.md): invocation envelopes, capture boundaries, one-run overrides, and coercion.
- [Flow](flow.md): statements, bindings, collections, async work, spawn, and exec.
- [Agic](agic.md): the model/tool loop, instruction assembly, and guidance visibility.
- [AST](ast.md): semantic representation, lowering, validation, and serialization.

## Language commands

- [Language commands](language-commands.md): offline `parse`, `fmt`, and `highlight`, their shared language pipeline and output/error boundaries.
- [Diagnostic examples](source-diagnostics-examples.md): verified AST/CST/formatter evidence supporting those commands.

## Preparation and resources

- [Setup](setup.md): installed runtime generations, configuration, lazy resource views, and publication.
- [State](state.md): authored layers, acceptance, immutable revisions, and publication.
- [Caps](caps.md): capability kinds, sources, scope, precedence, and materialization.
- [Workspaces](workspaces.md): directory grants, logical paths, workdir, capture, and guest authority.
- [Models](models.md): catalogs, connection routes, selection, model-call budgets, and accounting.
- [Tools](tools.md): tool invocation and built-in operation contracts.
- [Queries](queries.md): resource projections, matching, and availability/allow tags.

## Execution and scheduling

- [Execution](execution.md): Run/Thread lifecycle, admission, controls, limits, State capture, and history compaction.
- [Executor](executor.md): engine structure, task ownership, terminalization, and LiveTree.
- [Records](records.md): durable facts, values, references, and historical inspection.
- [Events](events.md): post-commit notifications, subscribers, SSE, replay/replacement, and Hub delivery.
- [Drivers](drivers.md): Chat/Talk/Chore activation, handler selection, scheduling, and recovery.
- [Chores](chores.md): named work definitions, identity, checkpoints, and manual occurrences.
- [IDs](ids.md): allocation and deterministic identity codecs.

## Hosting and storage

- [Hosting](hosting.md): sandbox processes, lifecycle/leases, installation, environment transport, and recovery.
- [Layout](layout.md): physical paths, discovery, locks, and storage locations.

## Clients and interaction

- [CLI](cli.md): command routing, inspection, server acquisition, and terminal metadata.
- [Scripts](scripts.md): source-local projects, invocation/help, and file-input origins.
- [Chat](chat.md): session commands, queued request snapshots, steering, recovery, and control/status UI.
- [Terminal UI](terminal-ui.md): shared terminal ownership, geometry, Markdown, palettes, and scrollback.

## Collaboration and integration

- [Messaging](messaging.md): Hub, conversations/rules, Talk consumption, and backend coordination.
- [HTTP API](api.md): application composition, request/response boundaries, and driver-gated routes.
- [Plugins](plugins.md): extension families, factories, loading, and plugin-facing contracts.

## Observation and presentation

- [Execution statistics](execution-statistics.md): derived usage cache, time windows, queries, and archival.
- [Top](top.md): current/recent activity, coverage, totals, and dashboard interaction.
- [Execution presentation](execution-presentation.md): progress projection and Run/Step rendering semantics.

## Historical records and reference

[Plans](plans/) retain discussion and implementation records that may become
outdated. They do not define current behavior; these guides link current owners
and implementation evidence instead of individual plans.
[Generated reference](../reference/README.md) provides module-level details.
