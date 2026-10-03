# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This record begins at the [v0.3.4](https://github.com/openhat-ai/toolang/releases/tag/v0.3.4)
release tag; earlier history is not backfilled.

## [Unreleased]

### Added

- Flows can use the new `exec` statement to replace the current runnable within
  the same Run, binding the target's declared inputs from the current locals
  ([#675](https://github.com/openhat-ai/toolang/pull/675)).

### Changed

- Cap resource selectors, recall, and hands/handoffs are now resolved against the
  latest published State before each model call, scoped to their declaring
  modules, instead of binding resources when the Run is accepted
  ([#675](https://github.com/openhat-ai/toolang/pull/675)).
- Hands/handoffs snapshots omit only the current runnable and its ancestors on the
  calling branch, so earlier handoffs, completed children, and siblings remain
  callable ([#675](https://github.com/openhat-ai/toolang/pull/675)).
- **Breaking:** Same-Run transfers are exposed to models as `_toolang/exec`, renamed
  from `_toolang/execute`, and the public runtime method `ToolRuntime.execute(...)`
  is renamed to `ToolRuntime.exec(...)`. Update any psyche, prompt, documentation,
  or custom tool plugin that references or implements the old names; they are
  removed without an alias
  ([#675](https://github.com/openhat-ai/toolang/pull/675)).
- `too inspect` execution trees show Step ordinals, `exec → target` boundaries,
  and a `handed off` marker for replaced runnables
  ([#675](https://github.com/openhat-ai/toolang/pull/675)).

### Fixed

- Execution progress renders run, exec, handoff, and iteration boundaries with one
  thin-rule style instead of inconsistent hyphen dividers
  ([#672](https://github.com/openhat-ai/toolang/pull/672)).
- Flow sources containing `exec` statements format consistently with other flow
  statements ([#676](https://github.com/openhat-ai/toolang/pull/676)).

[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.3.4...HEAD
