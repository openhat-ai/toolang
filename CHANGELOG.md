# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This record starts at the v0.3.4 baseline; earlier history is not backfilled.

## [Unreleased]

### Added

- Root Runs with no active descendants may `exec` their own entry runnable,
  replacing the run binding with the latest published implementation whose
  normalized contract matches, while preserving the Run, captured Setup,
  authority ceilings, limits, and entry output contract. `run self`, child
  self-exec, and ancestor calls remain rejected.

### Changed

- **Breaking:** `storm` is renamed to `generate` and `settle` to `reduce`, and
  `generate`, `map`, and `reduce` now require `using` for named targets and must
  omit it for inline bodies, so `map using [-> T]: BODY` becomes
  `map [-> T]: BODY`. `reduce` keeps its optional `from:` initializer.
- **Breaking:** flow arrays use the actual outermost array and there is no
  separate item/list shape. `run` makes exactly one child call with the complete
  input, including arrays, without iterating, wrapping, or flattening; `map`,
  `keep`, `drop`, `sort`, and `reduce` operate on outer items. Arrays from
  parameters, calls, helper flows, `exec`, and restored values behave
  identically, and array-valued child results stay nested.
- **Breaking:** the `Local` wrapper is removed. Stored outputs contain only
  `value` and `binding`, and the HTTP/event protocol projection contains `type`,
  `value`, and `binding`; update output references from `output/local/value` to
  `output/value`. RunStore schema 51 rejects older stores before mutation with no
  compatibility reader or migration, so keep the matching runtime to inspect old
  records and start new runs on a fresh store.

- **Breaking:** the `me` tool `me__loaded(receipts)` is replaced by `me__sync()`,
  which waits for one State publication and returns `{revision, files}` for the
  agent's tracked root and home sources. Integrations that poll `me.loaded`
  receipts must call `me.sync()` / `me__sync({})`, handle operational error codes,
  and use the scoped `files` manifest as the receipt.

### Removed

- **Breaking:** the `scatter` and `gather` flow statements are removed. Rewrite
  `scatter using R` and `gather using R` as `run R`, and give a former `scatter`
  body an explicit `run -> Text[]` type; a former `gather` may now receive `[]`.
  Source, snapshots, retries, and reruns using them are rejected with migration
  guidance, and older records remain available only to their matching runtime.

## [0.3.6] - 2026-10-04

### Added

- `me` adds `me__loaded(receipts)`, which compares home-file `{key, digest}`
  receipts with the State bound to the calling Run and returns `loaded`, the
  loaded `revision`, and only mismatching keys. (#679)

### Changed

- **Breaking:** the `me` tools now address the current agent's home files by
  home-relative path instead of resource kind and name. `me__list()` takes no
  arguments; `me__get(key)` reads whole files; `me__create(key, content)` fails
  if the file exists; `me__update(key, content, if_digest)` replaces whole
  content; and `me__delete(key, if_digest)` deletes one file and no longer
  manages task/chore lifecycle. Integrations using the old `kind`/`key`
  arguments or field-level updates must migrate; restart older writers to
  release the retired `.authored-flows.lock` and `.project.lock`. (#679)

### Fixed

- Flow and prompt templates retain typed values (Boolean, struct, array, and
  `Part`) instead of coercing them to text, and native strings such as `"false"`
  are not re-parsed into other types. (#680)

## [0.3.5] - 2026-10-03

### Added

- Flow `exec` statements replace the current runnable within the same Run and
  never return on success. Named and inline targets use the same forms and input
  binding as `run`, and named targets resolve from the latest published State. (#675)

### Changed

- Resource selectors (`models`, `tools`, `psyches`, `skills`, `services`,
  `prompts`) now filter the latest State within their declaring modules. Children
  inherit restrictions rather than an earlier selected list, and `+=` restores
  only items still allowed by ancestors and authority ceilings. (#675)
- Hands/handoffs snapshots now omit only the current runnable and its ancestors
  on the calling branch, so earlier handoffs, completed children, and siblings are
  callable again. (#675)
- **Breaking:** the runtime call tool `_toolang/execute` (exposed as
  `_toolang__execute`) is renamed to `_toolang/exec` (`_toolang__exec`), and the
  public `ToolRuntime.execute()` method to `ToolRuntime.exec()`. Integrations that
  invoke the tool or implement the method must switch to `exec`. (#675)
- Execution progress dividers are unified: Run boundaries use `┌`/`└`, `Run` and
  `Execute` captions use sentence case, and loop iterations show centered counters
  such as `1/3`. (#672)

### Fixed

- The formatter normalizes `exec` statements consistently with other flow
  statements. (#676)

[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.3.6...HEAD
[0.3.6]: https://github.com/openhat-ai/toolang/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/openhat-ai/toolang/compare/v0.3.4...v0.3.5
