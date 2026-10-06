# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This record starts at the v0.3.4 baseline; earlier history is not backfilled.

## [Unreleased]

### Added

- Flow `spawn RUNNABLE` and `spawn [-> T]: BODY` start an independent root in a
  new empty thread and return once it is admitted, without waiting. Each root
  gets a new run and thread identity with a null parent, inherits the source's
  captured State, settings, limits, cwd, and workspaces, and continues under the
  same executor when its source finishes, fails, is canceled, or hands off.
  Each root completes, fails, or is explicitly canceled on its own, and a root
  still unfinished when the executor stops is canceled then (a script host
  cancels unfinished roots on exit). No completion message is injected.

- `let job = spawn RUNNABLE` binds a Run handle readable through templates as
  ordinary data: `{{job.id}}`, `{{job.thread}}`, and `{{job.status}}`, with one
  status snapshot per statement and no waiting. Unknown fields fail, an authored
  struct named `Run` remains ordinary data, handles cannot be passed as runnable
  inputs, and `async`/`await` are not implemented in this release.

- The `_toolang/spawn` tool (wire name `_toolang__spawn`) starts an independent
  root through `run`'s input decoder and hands policy, and returns the committed
  admission snapshot `{id, thread, status: "pending"}`. It waits for no result
  and injects no completion message; inspect progress and results by ID with the
  existing tools.

- Root Runs with no active descendants may `exec` their own entry runnable,
  replacing the run binding with the latest published implementation whose
  normalized contract matches, while preserving the Run, captured Setup,
  authority ceilings, limits, and entry output contract. `run self`, child
  self-exec, and ancestor calls remain rejected.

### Changed

- **Breaking:** the runtime call tool `_toolang/run` (exposed as
  `_toolang__run`) now runs an authorized hand synchronously, matching flow
  `run`: its Tool Step stays open until the child ends, and its one tool reply
  returns the child's `{type, value}` on success or a tool error on failure or
  child-only cancellation. The scheduling receipt `{run_id, controls}` and the
  separate `run-result` completion message are removed. Integrations that read
  run outcomes from the receipt or the `run-result` context must read the tool
  reply, find the child Run whose parent names the Tool Step, and inspect its
  applied entry control by run ID. A child-only failure or cancellation leaves
  the caller running with a failed Tool Step; caller cancellation or immediate
  steer unwinds the child and cancels the Tool Step.

- Run controls are applied when admission commits, independent of Run
  execution status; runtime-created controls are never left pending. `spawn`
  and `exec` commit their successful source Step together with their controls,
  so later dispatch, execution failure, cancellation, or interrupted delivery
  cannot undo them. Only external `steer` and `cancel` requests stay pending,
  closing as `wontapply` if the Run ends first. A `StepEnd` cut short by a lock
  wait keeps its persisted finish time and is still delivered.

- **Breaking:** Run Control kinds `execute` and `cwd` are renamed to `exec` and
  `chdir`, and their payload types to `ExecControlPayload` and
  `ChdirControlPayload`; `RunStore.accept_exec_control` replaces
  `accept_execute_control`. Update integrations and control-kind filters to the
  new names; the `cwd` location field is unchanged. RunStore schema 52 rejects
  older stores unchanged, with no migration or old-name aliases, so inspect old
  records with their matching runtime and start new runs on a fresh store.

- Flow `spawn` Step outputs use the same `{"type", "value", "binding"}` envelope
  and `output/value` references as ordinary outputs. The native handle's runtime
  type tag is `_Run<T>` when the target's result type `T` is known and `_Run`
  otherwise, and its value holds only `id` and `thread`; user-authored struct
  names cannot begin with `_`. The complete accepted result contract stays on
  the spawned root entry; historical ordinary stored outputs without an explicit
  `type` remain readable. Flow `spawn` records a spawn-kind Step, while agic
  `_toolang/spawn` remains an ordinary tool Step. Once admission commits, the
  Step succeeds and keeps its receipt even if delivery is interrupted or the
  caller is canceled, and a dispatch failure is recorded on the new root.

- Hands now authorizes both `run` and `spawn`, while handoffs still authorizes
  `exec`. The public `ToolRuntime` protocol adds `spawn(runnable, input)`, so
  custom tool-runtime implementations must add it, and the model-facing runtime
  protocol documents spawn and Run handles.

- Bumped the pinned `tree-sitter-toolang` grammar to 0.4.0a2, which provides the
  `spawn` syntax.

- `reduce` with a `from:` initializer accepts an empty outer array and returns
  that initializer coerced to the reducer output type without any child calls;
  without an initializer it still rejects an empty array.

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
- **Breaking:** the `Local` wrapper is removed. Stored and HTTP/event outputs
  share the `type`, `value`, and `binding` envelope; update output references
  from `output/local/value` to `output/value`. RunStore schema 52 rejects older
  stores before mutation with no compatibility reader or migration, so keep the
  matching runtime to inspect old records and start new runs on a fresh store.

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
