# Runtime tool calls

## Goal and scope

Provide observable runtime operations for history inspection, resource recall,
and compaction. Reuse Tool Steps, controls, and the
[ModelCall assembly contract](control-driven-model-assembly.md); assembly remains
a separate implementation. This PR defines the work and ships no product code.

In scope: tool classification, `_me` to `me`, the four new calls below,
path-aware preflight, and their persistence/progress integration.
Out of scope: runspace selection, filesystem permission expansion, memory plugins,
new Step kinds, new retry restrictions, and redesigning MCP transport or assembly.

## Classification and catalog

Runtime describes ownership, not the caller:

```text
Tool calls
├── Runtime tool calls: _too/*
│   ├── Model-triggered: run, execute, reload, inspect, pick
│   └── Runtime-triggered: honor, compact
└── Normal tool calls: me/*, fs/*, shell/*, service/*, ...
```

Preserve existing run/execute/reload behavior. All four additions use ordinary
Tool Steps; add no generic recall or assembly tool.

| New call | Trigger | Input and responsibility | Durable result |
| --- | --- | --- | --- |
| `_too/inspect` | Model | Read bounded Thread/Run history or a Run's output. | Tool result; no control. |
| `_too/pick` | Model | Read skill/service guidance selected by catalog `ref`. | Tool result and recall control. |
| `_too/honor` | Runtime preflight | Read applicable workspace rules for resolved access paths. | Tool result and recall control(s). |
| `_too/compact` | Runtime before Model Step begin | Obtain a complete-prefix summary using the assembly budget and boundaries. | Tool result and compact control. |

Expose only model-triggered operations to the model; reject model attempts to
invoke honor/compact. Classify runtime activity by `_too/*`, not any leading
underscore. Model-facing encoding remains `<toolset>__<leaf>`.

## Normal authored-data tools: me

Rename the `_me` plugin/toolset to `me`, preserving its leaves, current-agent
scope, authorization, and factory pattern. Update entry points, selectors,
defaults, protocol text, docs, and tests together. Use public-name registration
and existing duplicate rejection; `_too` remains reserved to Toolang.

Do not add aliases or rewrite historical tool identities. Existing references to
unavailable `_me` tools follow the existing unavailable-resource behavior.
This supersedes only the `_me` naming decision in
[internal toolsets](internal-toolsets.md), not its other authority rules.

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
Model path request → preflight → honor Tool Step → recall control(s)
                    → original Tool result: operation not executed; retry required
Next ModelCall      → rules user message(s) → Model retries the path operation
```

No-op preflight creates no honor Step; already-visible picks add no recall control.
Blocked calls may share an unadopted recall, but none proceeds before the model
receives the rules. Recheck on retry, including after compact/reload. The model
picks missing guidance; assembly does not restore it.

Tools declare the normalized paths used for execution; runtime owns rule discovery
and recall. Start with explicit fs targets and shell cwd, without claiming coverage
of arbitrary shell commands/indirect paths or a sandbox guarantee.

## Compact

```text
Prepare request → budget check → runtime compact Tool Step
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
github", and "Loading workspace rules: toolang /src". Distinguish runtime/normal
activity in live and reconstructed views. "Loaded" is not yet Model Step adoption;
retry-required is not a completed read/write. Progress never changes execution.

## Implementation sequence and acceptance

- [ ] Rename me and classify runtime tools. Touch agent_state tools, plugin
  registration, pyproject.toml, defaults/docs/tests; reject collisions and model
  attempts to invoke runtime-triggered calls without changing normal permissions.
- [ ] Add inspect through execution/tools/runtime.py and RunHistory. Cover fixed
  ranges, output-only reads, page budgets, fork/rewind, child isolation, and restart.
- [ ] Add pick and common recall handling in the executor and control messages.
  Cover allowed refs, exact revisions/content, deduplication, and live/replay equality.
- [ ] Add path-awareness and honor through tool preparation and fs/shell adapters.
  Cover nested rules, batches, failed loads, zero side effects before retry, changed
  files, and rules falling out of view; avoid rereading stable message prefixes.
- [ ] Add the compact coordinator and bundled compact.too after assembly is ready.
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
