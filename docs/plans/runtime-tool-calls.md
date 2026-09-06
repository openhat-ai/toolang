# History and runtime tool calls

## Goal and scope

Provide bounded history reads and observable runtime operations for resource
recall and compaction. Reuse Tool Steps, controls, and the
[ModelCall assembly contract](control-driven-model-assembly.md); assembly remains
a separate implementation. This PR defines the work and ships no product code.

In scope: tool classification, shared plugin registration, `_too` to `_toolang`,
`_me` to `me`, the history toolset, three new runtime calls, both preflights,
and persistence/progress integration.
Out of scope: runspace selection, filesystem permission expansion, memory plugins,
new Step kinds, new retry restrictions, message grouping in history readers, and
redesigning MCP transport or assembly.

## Terms and catalog

- **User tools** are user-facing tools: they provide capabilities for user tasks.
- **Runtime tools** are runtime-facing tools: they assist runtime operations.
- **Toolset** is a named group of tools. All tools are provided by toolset plugins.
- **Model-triggered / runtime-triggered** describes who initiates a call,
  independently of the tool's responsibility.

```text
Tool calls
├── Runtime tool calls: _toolang/*
│   ├── Model-triggered: run, execute, reload, pick
│   └── Runtime-triggered: honor, compact
└── User tool calls: history/*, me/*, fs/*, shell/*, service/*, ...
```

Preserve existing run/execute/reload behavior. All additions use ordinary Tool
Steps. History serves both user investigations and compact Runs; being called by
a compact Run does not make it runtime-facing.

| New call | Trigger | Input and responsibility | Durable result |
| --- | --- | --- | --- |
| `history/read_threads` | Model | List the current agent's Threads. | Records and cursor; no control. |
| `history/read_runs` | Model | List a Thread's logical root Runs. | Records and cursor; no control. |
| `history/read_steps` | Model | Read a Run's Steps and Controls. | Records and cursor; no control. |
| `history/read_output` | Model | Read a Run's stored output and status. | Typed output; no control. |
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

## Call contracts

Arguments and results are JSON objects; references use existing fully qualified
pointer strings. In the signatures below, `?` means optional. Results describe
ToolResultPart.output; failures use its existing error field, not another envelope.
Reject unknown arguments and invalid reference types at the tool boundary.

The per-call context supplies the current agent, Thread, Run, Tool Step, effective
binding, and visible recall revisions. Callers cannot supply authority, recalled
content, revisions, or the control's triggered_by. History gets a read-only
interface; runtime tools get only the operations they require.

## History

Register `history` as a user-facing toolset. Reuse RunHistory, ThreadView, and
RunView; do not assemble messages, compute far/near, or calculate message groups.
Pages may separate a ToolCall from its ToolResult: preserve both records and their
references, without making the exchange an indivisible pagination unit.

### Parameters

```python
# First page
history/read_threads(limit=20)
history/read_runs(thread?, begin?, end?, limit=20, from_end=false)
history/read_steps(run, begin?, end?, limit=20, from_end=false)
history/read_output(run)

# Subsequent pages: cursor is the only argument
history/read_threads(cursor)
history/read_runs(cursor)
history/read_steps(cursor)
```

- `thread`: ThreadRef, defaulting to the current Thread. `run`: required RunRef.
- `begin/end`: RunRefs belonging to the selected logical Thread for read_runs;
  StepRefs belonging to the selected Run for read_steps. The range is `[begin,
  end)`; omitted bounds are open. Reversed or foreign bounds fail; equal bounds
  return an empty page.
- `limit`: positive integer, counting primary records, not dependency controls.
  It is the read budget; history adds no message/token sizing or content truncation.
- `from_end`: boolean; take pages from the range's tail, but keep each page in
  natural order. Runs use logical Thread order; Steps use numeric Step order.
  read_threads uses the existing newest-updated-first order, with id as a tie-break.
- `cursor`: opaque, scoped to this agent and the same tool. Freeze the target,
  range, ordering, and limit on the first call; reject mixed cursor/query arguments.
  An omitted or null cursor starts a new read; a returned null cursor means done.

### Results

```python
read_threads -> {threads: ThreadRecord[], cursor: str | null}
read_runs    -> {thread: ThreadRef, head: ControlRef,
                 runs: RunRecord[], cursor: str | null}
read_steps   -> {run: RunRecord, entries: (StepRecord | ControlRecord)[],
                 dependencies: ControlRecord[], cursor: str | null}
read_output  -> {run: RunRef, status: RunStatus, output: Local | null}
```

Use canonical record serialization. Run records retain physical ownership;
read_runs identifies the selected logical Thread. read_steps preserves RunView's
entries and dependency distinction: unbounded reads include owned raw controls, even unused
ones; bounded reads include the selected Steps and required controls. Dependencies
may recur between pages and are not additional timeline entries. Child Runs are
not expanded; their references allow explicit reads.

Resolve Local values in selected Step outputs and Control payload.input for the
response, so compact Runs can read content rather than only pointers. Keep structural
references and stored ModelCall references; do not rebuild ModelCalls. read_output
uses get_output and returns its resolved Local as `{type, value, name, dim}`;
null means the Run exists but has no stored output, not that it succeeded. Preserve
the recorded status and partial data. Unknown targets and unresolved values fail.

Keep RunHistory's fixed-range cursor behavior: later appends are excluded, Thread
rewinds do not change captured membership, and replacement of captured Run/Step
facts invalidates continuation. Add paged Thread-record listing: capture its ID
order, exclude new Threads, and read metadata as of each page, not a historical
snapshot. Cursors survive restart; an invalidated cursor requires a fresh read.
No MVCC or new mutation restrictions are needed.

All reads stay within the current agent's store, create no controls, and inject
no messages. Compact Runs use these same calls for target records and earlier
compact outputs; add no special recall directive or latest-compact tool.

## Honor and pick

### Parameters and results

```python
_toolang/pick({kind: "skill" | "service", ref: str})
  -> {target: {kind, ref}, revision: str, control: ControlRef | null}

_toolang/honor({paths: str[]})
  -> {recalls: [{target: {kind: "rules", workspace: str, path: str},
                 revision: str, control: ControlRef}]}
```

Pick takes one exact ref from the corresponding catalog. Honor takes a nonempty
list of normalized absolute access paths from tool call preflight; these are not
rule-file paths. Runtime maps them to authorized workspaces and applicable rules.
Each rules target identifies a workspace name and its scope directory relative
to the workspace root: `/` or, for example, `/src`.

Revision fingerprints the exact recalled text. A non-null control identifies a
new or reused, unadopted recall; pick returns null when that revision is already
visible. Honor returns only the missing/outdated revisions, deduplicated by
target; an empty result means none remain after rechecking. Loading failure is
a tool error and never permits the original operation. Neither result repeats
content or claims the consuming Model Step has adopted it.

Persist recalled content in the existing applied recall control payload
`{target, revision, content}`. Control.triggered_by identifies the creating Tool
Step and is unchanged on reuse; the consuming Model Step records preceded_by.
Independent user messages use existing rules/skill/service wrappers and delta
references to that content.

Pick resolves only resources allowed by the effective binding. It grants no new
capabilities; MCP connection/auth/discovery/operations remain in service/*.
Reading authored data through me/get does not constitute recall.

Both recall paths match target/revision in actual visible near/now; far and
invisible old controls do not count. Honor collects ancestor/nested rules for
resolved paths, not all configured workspaces, and never changes access or roots.

```text
Model path request → tool call preflight → honor Tool Step → recall control(s)
  → original Tool result: operation not executed; retry required
Next ModelCall → rules user message(s) → Model retries the path operation
```

No-op tool call preflight creates no honor Step. Blocked calls may share an
unadopted recall, but none proceeds before the model receives the rules. Recheck
on retry, including after compact/reload. The model picks missing guidance;
assembly does not restore it.

After successful honor, the blocked original call returns
`error: "operation not executed; retry required"` with `output: {}`. This is the
original call's result, not honor's result; recall content arrives separately.

Tools declare the normalized paths used for execution; runtime owns rule discovery
and recall. Start with explicit fs targets and shell cwd, without claiming coverage
of arbitrary shell commands/indirect paths or a sandbox guarantee.

## Compact

### Parameters and results

```python
_toolang/compact({thread: ThreadRef, begin: RunRef | null = null, end: RunRef})
  -> {horizon: FieldRef | null, control: ControlRef | null}

# Input and output of the invoked compact.too Run
input  = {thread, begin, end}
output = {thread, begin, end, summary: str}
```

Model call preflight chooses the range. Thread must be the calling Run's Thread;
begin is null or its first logical root. End is exclusive and must leave at least
one historical root before the active root. This version accepts root boundaries
only. Pass the range unchanged into compact.too; never accept summary as an argument.

On success, horizon references the validated compact Run output, not a copied
summary; control identifies the new or reused, unadopted compact control for the
calling Run. Reuse requires the same Thread and range. If the permit recheck finds
no work or adoption needed, return the effective horizon (possibly null) and a
null control. If the requested range became invalid, return an error and let
preflight reprepare instead of silently changing the recorded arguments.

Output must echo the input range with a complete-prefix summary. History tools
must be available to compact.too through its explicit authorized tool selection,
not a hidden history-reading bypass.

```text
Model call preflight → prepare request → budget check → compact Tool Step
  → compact.too root Run in compact_<thread>
  → output {thread, begin, end, summary}
  → compact control {horizon: compact Run output reference}
  → prepare again → commit Model Step → dispatch
```

The compact Run is a root in its own Thread, not a cross-Thread child. The outer
Tool Step links its result and shows activity; its internal Steps stay outside
the target conversation/progress. Compaction Runs never recursively trigger compact;
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
- [ ] Add the history toolset through execution/tools/history.py and RunHistory;
  add cursor paging for Thread listing without changing existing CLI readers.
  Cover all four input/result schemas, cursor-only continuation, wrong-tool cursors,
  fixed ranges, raw pages crossing tool exchanges, unused/dependency controls,
  resolved output, missing targets, record limits, fork/rewind, child isolation,
  and restart. No message-group computation or ModelCall rebuilding.
- [ ] Add pick and common recall handling in the executor and control messages.
  Cover allowed refs, exact revisions/content, visible/null and pending/reused
  receipts, deduplication, and live/replay equality.
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
