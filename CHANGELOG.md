# Changelog

All notable changes to Toolang are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic versioning.


## [Unreleased]

### Changed

- Script runnable commands no longer copy their persisted Run result to stdout
  by default. Use `--save -` for stdout or `--save PATH` for atomic file output.


## [0.3.0] - 2026-08-03

### Added

- Added static `agic` and `flow` executables with typed signatures, shared
  content input, directives, and AST-driven execution.
- Added durable thread, run, step, control, and normalized model-call storage.
- Added direct script execution, local terminal chat, run inspection, and
  thread steering, cancellation, rewind, and fork operations.
- Added a versioned local agent HTTP API for agent state, caps, jobs, runs, and
  threads, including native run-event SSE streams.
- Added RRULE-based chore scheduling and file-inbox execution.
- Added explicit tool, channel, sandbox, model-provider, and model-adapter
  plugin families.

### Changed

- Replaced the legacy top-level `use` and `thunk` language with `with`, `agic`,
  and `flow` declarations.
- Rebuilt execution around immutable setup and agent-state snapshots, explicit
  ceilings, native run events, and canonical percept/message values.
- Reorganized runtime ownership into focused `catalog`, `execution`, `lang`,
  `plugin`, `setup`, `state`, `up`, and `work` packages.
- Replaced the legacy invocation surface with direct runnable commands such as
  `toolang SCRIPT RUNNABLE`.
- Made model adapters, tools, sandboxes, channels, run tracers, and executor
  contracts asynchronous at their runtime boundaries.

### Fixed

- Kept chat submission validation failures separate from accepted durable runs.
- Improved multi-process identity allocation and serialized authored catalog
  writes.


## [0.2.7] - 2026-06-16

- Final published release of the legacy thunk-based runtime before the current
  language and execution architecture revision.


[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/openhat-ai/toolang/compare/v0.2.7...v0.3.0
[0.2.7]: https://github.com/openhat-ai/toolang/compare/v0.2.6...v0.2.7
