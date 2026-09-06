# Runtime tool calls

## Goal and scope

Provide observable runtime operations for history inspection, resource recall,
and compaction. Reuse Tool Steps, controls, and the
[ModelCall assembly contract](control-driven-model-assembly.md); assembly remains
a separate implementation. This PR defines the work and ships no product code.

In scope: tool classification, shared plugin registration, `_too` to `_toolang`,
`_me` to `me`, the four new calls below, both preflights, and their
persistence/progress integration.
Out of scope: runspace selection, filesystem permission expansion, memory plugins,
new Step kinds, new retry restrictions, and redesigning MCP transport or assembly.

## Terms and catalog

- **User tools** are user-facing tools: they provide capabilities for user tasks.
- **Runtime tools** are runtime-facing tools: they assist runtime operations.
- **Toolset** is a named group of tools. All tools are provided by toolset plugins.
- **Model-triggered / runtime-triggered** describes who initiates a call,
  independently of the tool's responsibility.

Neither plugin provenance nor user configuration determines this classification.
Do not use internal, normal, or plugin tools as additional tool categories.

```text
Tool calls
├── Runtime tool calls: _toolang/*
│   ├── Model-triggered: run, execute, reload, inspect, pick
│   └── Runtime-triggered: honor, compact
└── User tool calls: me/*, fs/*, shell/*, service/*, ...
```

Preserve existing run/execute/reload behavior. All four additions use ordinary
Tool Steps; add no generic recall or assembly tool.

| New call | Trigger | Input and responsibility | Durable result |
| --- | --- | --- | --- |
| `_toolang/inspect` | Model | Read bounded Thread/Run history or a Run's output. | Tool result; no control. |
| `_toolang/pick` | Model | Read skill/service guidance selected by catalog `ref`. | Tool result and recall control. |
| `_toolang/honor` | Tool call preflight | Read applicable workspace rules for resolved access paths. | Tool result and recall control(s). |
| `_toolang/compact` | Model call preflight | Obtain a complete-prefix summary using the assembly budget and boundaries. | Tool result and compact control. |

The runtime owns both checks: **model call preflight** runs before Model Step
begin and may compact, then prepare again; **tool call preflight** runs before
the requested operation and may honor rules, then require a model retry.

## Registration and execution

Register `_toolang` as a built-in toolset plugin through the existing factory,
entry-point, and duplicate-checking path; replace the separate runtime-tool
definition registry. `_toolang` is the runtime toolset, not a collection of all
built-in tools. Leading underscores reserve names for Toolang, but do not imply
runtime responsibility or model visibility. Keep `<toolset>__<leaf>` encoding.

Registration, model exposure, and invocation are separate. Register honor/compact
without exposing them to the model; reject model attempts to invoke them. Apply
user-tool selectors to user tools, preserving existing runtime availability and
hand/handoff authorization. Selection must not disable required runtime preflights.

Plugins implement operations; the executor owns Tool Step begin/end, cancellation,
persistence, and control adoption. Provide trusted runtime tools with a narrow
per-call runtime interface, declared in shared protocols and implemented by the
executor; do not expose the executor/store to plugins or bind mutable Run state
into shared plugin instances. Finish the execute Tool Step before the executor
performs control transfer; do not swallow that transfer as a tool error.

Rename `_too` to `_toolang` and `_me` to `me`. Preserve me's leaves, current-agent
scope, and authorization. Update entry points, selectors, defaults, protocol text,
docs, and tests together. Add no aliases or history rewrites; unavailable old
references follow the existing unavailable-resource behavior. These names and
terms replace those in the [earlier naming plan](internal-toolsets.md); preserve
its namespace reservation and registration authority rules.

## Inspect

Use RunHistory, RunView, and ThreadView. Accept a Thread/Run `target` and `view`
(`history` or Run-only `output`). History accepts half-open begin/end and a page
limit; continuation uses only the cursor. Both views have a runtime-owned budget.

Return natural order and fully qualified references: Thread pages identify
logical roots; Run pages contain Steps and required controls, not child internals.
Preserve fixed-range pagination and mutation behavior. Never silently split tool
exchanges or label incomplete output as complete. Output-only reads do not rebuild
calls. Reads stay within the current agent's execution store and inject no
instructions. Compact Runs use the same tool for target history and earlier
compact outputs; add no special recall directive or "latest compact" tool.

## Honor and pick

Both produce the existing applied recall control:

```yaml
target: rules(workspace, path) | skill(ref) | service(ref)
revision: ...
content: ...
```

Control.triggered_by points to the honor/pick Step; the consuming Model Step
records preceded_by. Persist the actual content before adoption. Tool results
report status; independent user messages use existing rules/skill/service wrappers
and delta references to that content, without repeating its body in tool results.

Pick resolves only resources allowed by the effective binding. It grants no new
capabilities; MCP connection/auth/discovery/operations remain in service/*.
Reading authored data through me/get does not constitute recall.

Honor runs when applicable rules are absent/outdated. Both recall paths match
target/revision in actual visible near/now; far and invisible old controls do not
count. Honor collects ancestor/nested rules for resolved paths, not all configured
workspaces, and never grants access or changes roots.

```text
Model path request → tool call preflight → honor Tool Step → recall control(s)
  → original Tool result: operation not executed; retry required
Next ModelCall → rules user message(s) → Model retries the path operation
```

No-op tool call preflight creates no honor Step; already-visible picks add no
recall control.
Blocked calls may share an unadopted recall, but none proceeds before the model
receives the rules. Recheck on retry, including after compact/reload. The model
picks missing guidance; assembly does not restore it.

Tools declare the normalized paths used for execution; runtime owns rule discovery
and recall. Start with explicit fs targets and shell cwd, without claiming coverage
of arbitrary shell commands/indirect paths or a sandbox guarantee.

## Compact

```text
Model call preflight → prepare request → budget check → compact Tool Step
  → compact.too root Run in compact_<thread>
  → output {thread, begin, end, summary}
  → compact control {horizon: compact Run output reference}
  → prepare again → commit Model Step → dispatch
```

The compact Run is a root in its own Thread, not a cross-Thread child. The outer
Tool Step links its result and shows activity; its internal Steps stay outside
the target conversation/progress. Preserve complete-prefix coverage and at least
one historical root in near. Compaction Runs never recursively trigger compact;
compact.too reads bounded pages and splits/reduces oversized input itself.

Use one cross-process permit per target Thread. After acquiring it, recheck the
budget and reuse a suitable result. Failure/cancel leaves horizon unchanged;
canceling one waiter must not abort another's work. Reprepare after waiting with
intervening reload/steer/cancel. Discarded candidates create no Model Steps.
Do not hold a store transaction or the Model Step begin lock while waiting or
executing compaction. The compact control's triggered_by points to its Tool Step.

## Messages and progress

Runtime-triggered calls are real records, not model-emitted ToolCalls: no fabricated
assistant or orphan tool messages. Their effects reach assembly through controls;
model-triggered calls retain normal tool-exchange rules.

Use Tool Step summaries such as "Compacting history", "Loading service guidance:
github", and "Loading workspace rules: toolang /src". Distinguish runtime/user
activity in live and reconstructed views. "Loaded" is not yet Model Step adoption;
retry-required is not a completed read/write. Progress never changes execution.

## Implementation sequence and acceptance

- [ ] Register the runtime toolset as `_toolang` and rename `_me` to `me`. Touch
  execution/tools, shared tool protocols/context, toolset loading/registration,
  executor preparation/dispatch, pyproject.toml, defaults/docs/tests. Cover
  collisions, unchanged user permissions and runtime availability, rejected model
  calls to honor/compact, per-call context isolation, and execute control transfer.
- [ ] Add inspect through execution/tools/runtime.py and RunHistory. Cover fixed
  ranges, output-only reads, page budgets, fork/rewind, child isolation, and restart.
- [ ] Add pick and common recall handling in the executor and control messages.
  Cover allowed refs, exact revisions/content, deduplication, and live/replay equality.
- [ ] Add tool call preflight and honor through tool preparation and fs/shell adapters.
  Cover nested rules, batches, failed loads, zero side effects before retry, changed
  files, and rules falling out of view; avoid rereading stable message prefixes.
- [ ] Add model call preflight, the compact coordinator, and bundled compact.too
  after assembly is ready.
  Cover budgets/coverage, concurrent waiters, failure/cancel, restart, intervening
  controls, exact output references, and unchanged earlier ModelCalls.
- [ ] Extend execution_progress and its Chat/Script tests for both trigger sources.
  Verify durable begin/end and control ordering, no fabricated exchanges or duplicate
  child results, canceled/failed activity, and recovery after commit/delivery failures.

Main risks: false completion, runtime records leaking into model exchanges, stale
visibility, and namespace/authorization confusion. Run Ruff check/format, ty, and
the complete offline pytest suite for each change.

## Open questions

None for this scope. General shell path interception and finer-than-root compaction
boundaries require separate definitions; this plan does not imply their support.
