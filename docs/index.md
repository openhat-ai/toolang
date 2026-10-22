# Documentation

These guides cover Toolang authoring and current implementation design. Source
and tests are authoritative. Repository standards live at the root; start with
[CONTRIBUTING.md](../CONTRIBUTING.md). User tutorials live on the
[documentation website](https://toolang.ai/docs).

This is the single TOC. Design guides stay flat under `design/`, with chapters
organized by responsibility. Read the system overview before the relevant chapter.

## Authoring

- [Authoring conventions](authoring.md): Toolang source style, type annotations, comments, and formatting conventions.

## System overview

- [Architecture](design/architecture.md): concepts, package boundaries, source identity, placement, and the path from authored input to execution.

## Programming model

- [Program](design/program.md): semantic representation, validation, modules, declarations, signatures, types, and shared directives.
- [Content](design/content.md): authored text, prompt/include expansion, templates, and message Parts.
- [Call Input](design/call-input.md): invocation envelopes, capture boundaries, one-run overrides, and coercion.
- [Flow](design/flow.md): statements, bindings, collections, async work, spawn, and exec.
- [Agic](design/agic.md): the model/tool loop, instruction assembly, and guidance visibility.

## Preparation and resources

- [Setup](design/setup.md): installed runtime generations, configuration, lazy resource views, and publication.
- [State](design/state.md): authored layers, acceptance, immutable revisions, and publication.
- [Caps](design/caps.md): capability kinds, sources, scope, precedence, and materialization.
- [Workspaces](design/workspaces.md): directory grants, logical paths, workdir, capture, and guest authority.
- [Models](design/models.md): catalogs, connection routes, selection, model-call budgets, and accounting.
- [Tools](design/tools.md): tool invocation and built-in operation contracts.
- [Queries](design/queries.md): resource projections, matching, and availability/allow tags.

## Execution and scheduling

- [Execution](design/execution.md): Run/Thread lifecycle, admission, controls, limits, State capture, and history compaction.
- [Executor](design/executor.md): engine structure, task ownership, terminalization, and LiveTree.
- [Records](design/records.md): durable facts, values, references, and historical inspection.
- [Events](design/events.md): post-commit notifications, subscribers, SSE, replay/replacement, and Hub delivery.
- [Drivers](design/drivers.md): Chat/Talk/Chore activation, handler selection, scheduling, and recovery.
- [Chores](design/chores.md): named work definitions, identity, checkpoints, and manual occurrences.
- [IDs](design/ids.md): allocation and deterministic identity codecs.

## Hosting and storage

- [Hosting](design/hosting.md): sandbox processes, lifecycle/leases, installation, environment transport, and recovery.
- [Layout](design/layout.md): physical paths, discovery, locks, and storage locations.

## Clients and interaction

- [CLI](design/cli.md): command routing, inspection, server acquisition, and terminal metadata.
- [CLI command recognition](design/cli-command-recognition.md): agent parameter contracts, unified script selection, and dynamic signatures.
- [Scripts](design/scripts.md): source-local projects, invocation/help, and file-input origins.
- [Chat](design/chat.md): session commands, queued request snapshots, steering, recovery, and control/status UI.
- [Terminal UI](design/terminal-ui.md): shared terminal ownership, geometry, Markdown, palettes, and scrollback.

## Collaboration and integration

- [Messaging](design/messaging.md): Hub, conversations/rules, Talk consumption, and backend coordination.
- [HTTP API](design/api.md): application composition, request/response boundaries, and driver-gated routes.
- [Plugins](design/plugins.md): extension families, factories, loading, and plugin-facing contracts.

## Observation and presentation

- [Execution statistics](design/execution-statistics.md): derived usage cache, time windows, queries, and archival.
- [Top](design/top.md): current/recent activity, coverage, totals, and dashboard interaction.
- [Execution presentation](design/execution-presentation.md): progress projection and Run/Step rendering semantics.

## Historical records and reference

[Plans](plans/) retain discussion and implementation records that may become
outdated. They do not define current behavior and are not referenced by
individual current-design guides.
[Generated reference](../reference/README.md) provides module-level details.
