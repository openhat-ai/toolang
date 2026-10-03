# Changelog

All notable changes to Toolang are documented in this file.

This file follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Tracking begins after v0.3.4; earlier releases predate this file.

## [Unreleased]

### Added

- Add the `exec` Flow statement, which replaces the current runnable within the
  same Run and never returns on success, including from nested repeats. Inline
  agics keep their containing code, and the Run keeps its identity and output
  contract. (#675)
- Add `aide.too` at the repository root, a tracked repository helper exposing the
  `changelog` runnable, which generates `CHANGELOG.md` following Keep a Changelog
  1.1.0. It supports repository maintenance and is not an installed CLI command.

### Changed

- Resolve resource selectors against the latest published State before each
  model call, so active Runs pick up published agent, flow, and cap updates while
  accepted code stays fixed. Children inherit the active path's rules instead of
  an earlier selected resource list. (#675)
- Callability follows the calling branch's active path: the current runnable and
  its ancestors are rejected, while an earlier handoff target can be called
  again. (#675)
- **Breaking:** Rename the same-Run transfer tool from `_toolang/execute` to
  `_toolang/exec`, and the `ToolRuntime.execute(...)` protocol method to
  `ToolRuntime.exec(...)`. Update integrations, explicit tool references, and
  custom tool plugins to the new names; the old names are removed without an
  alias. (#675)

### Fixed

- Format `exec` Flow statements consistently in `too fmt`. (#676)
- Unify Script and Chat execution progress dividers so Run, Execute, and
  iteration boundaries share one rendering. (#672)

[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.3.4...HEAD
