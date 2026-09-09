# Changelog

All notable changes to Toolang are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic versioning.


## [Unreleased]


## [0.3.0]

First public release. Earlier version numbers tracked internal development.

### Added

- Static `agic` and `flow` runnables with typed signatures, structured output,
  shared input forms, and direct `.too` script execution.
- Durable threads, runs, steps, controls, model calls, and execution inspection;
  steering, cancellation, retry, rerun, fork, rewind, and history compaction.
- Terminal Chat with slash commands, queued submissions, session settings,
  optional thread selection, and local or sandboxed execution.
- Agent workspaces, workspace URI addressing, and workspace instructions applied
  before tool calls.
- Typed collection queries for models, tools, psyches, skills, services, and
  prompts, with CLI field discovery.
- Agent state snapshots, bounded history tools, and runtime tool calls for
  loading guidance and invoking runnables.
- RRULE-based chore scheduling and a versioned agent HTTP API with run-event SSE.
- Explicit toolset, model adapter, model catalog, channel, and sandbox plugins;
  host and Docker execution, layered model catalogs, and usage/cost accounting.
- Compact CLI help with aligned groups, explicit operand notation, `-h`/`-V`,
  and optional-value options; live execution progress and parallel summaries.


[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/openhat-ai/toolang/releases/tag/v0.3.0
