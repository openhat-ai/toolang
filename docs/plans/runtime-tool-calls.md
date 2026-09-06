# History and runtime tool calls

## Goal and baseline

Connect tool calls and preflights to the execution records and ModelCall assembly
already shipped in [#486](https://github.com/openhat-ai/toolang/pull/486).
This is a definition-only PR; implementation follows in small, reviewable PRs.

The existing [assembly contract](control-driven-model-assembly.md) already owns
far/near/now selection, immutable message deltas, horizon adoption, and exact
replay. Recall controls store target/revision/content and produce user messages.
RunHistory, ThreadView, and RunView provide bounded record reads. The missing
pieces are tool registration, real recall triggers, recall visibility, and
automatic compaction. Context continues to be sent on every ModelCall;
context deduplication is not part of this work.

In scope: `_too` to `_toolang`, `_me` to `me`, shared toolset registration,
pick/honor, history, both preflights, and their execution/progress integration.
Out of scope: new Step/control kinds, redesigned assembly, runspace selection,
expanded filesystem permissions, memory plugins, new retry restrictions, MCP
transport changes, and message grouping in history readers.

## Terms and tools

- **User tool:** a capability for user tasks, including history inspection.
- **Runtime tool:** an operation that assists runtime execution.
- **Toolset:** a named group of tools supplied by a plugin.
- **Model-triggered / runtime-triggered:** who initiates a call, independently
  of its responsibility or whether its definition is exposed to the model.

```text
Tool calls
├── Runtime tools: _toolang/*
│   ├── Model-triggered: run, execute, reload, pick
│   └── Runtime-triggered: honor, compact
└── User tools: history/*, me/*, fs/*, shell/*, service/*, ...
```

Honor is not a model command; pick is. Both use the existing `recall` control,
not separate honor/pick control kinds. History remains a user tool when called
by a compact Run.

## Shared execution boundary

Register `_toolang` through the existing toolset factory, entry-point, and
duplicate-checking path. Replace the separate runtime definition registry.
Preserve the [namespace authority rules](internal-toolsets.md) and
`<toolset>__<leaf>` encoding. Rename `_me` to `me` without changing its leaves,
current-agent scope, or authorization; update selectors, defaults, protocol text,
docs, and tests together. Add no aliases or historical record rewrites.

Keep registration, selection, and invocation separate. User selectors select
user tools; runtime availability and hand/handoff authorization retain their
existing behavior. Honor/compact are registered when implemented but are never
advertised to, or callable by, the model. Selection cannot disable preflights.

Plugins use narrow per-call interfaces declared in shared protocols. History
receives read-only operations; trusted runtime tools receive only the runtime
operations they require. The executor supplies the current agent/Thread/Run/Tool
Step and binding. Do not expose the Store/executor, accept authority in model
arguments, or retain mutable Run state in shared plugins.

The executor owns Step allocation, begin/end, cancellation, persistence, control
adoption, and transfer. Finish execute's Tool Step before applying its committed
transfer; generic error handling must not swallow that transfer.

Separate recording a Tool Step result from delivering it to model messages:

- Model-triggered calls retain their real ToolCall source and normal ToolResult.
- Runtime-triggered calls have durable Tool Steps/results but no fabricated
  assistant ToolCall or orphan tool message. Controls carry their assembly effects.
- Persist invocation provenance in `ToolStepGiven.trigger: "model" | "runtime"`,
  supplied by the executor, not tool arguments or name inference. This is a field,
  not a new Step kind. Keep `Step.input` for data dependencies.
- Keep a blocked original call distinct from honor; only the original has a
  model ToolCall to answer. Do not reuse its call identity for honor.
- Progress/inspection show both sources. Recovery preserves committed facts
  without automatically repeating an uncertain external action.

Version changed record encodings through the existing Store schema policy;
reject incompatible databases without modifying them. Do not add MVCC or a
parallel runtime-message log.

## Results and adoption

Arguments/results are JSON objects. Validate external arguments and reference
types once at the tool boundary, then use typed values internally. Failures use
ToolResultPart.error, not another error envelope. Control-only success returns:

```javascript
{controls: ["<ControlRef>", ...]}
```

Pick, compact, reload, and execute return at most one reference; honor may return
several, once each in control-index order. Pick/honor/compact return an empty list
when no adoption is needed. Receipts identify committed records, not predicted
effects, and do not themselves mean that a Model Step adopted them.

| Fact | Authoritative storage, not copied into receipts |
| --- | --- |
| Recall target, revision, original text | Recall control payload |
| Reload state | Reload control payload |
| Execute state, runnable, input | Execute control payload |
| Horizon | Compact control payload |
| Compacted range and summary | Compact Run output referenced by horizon |

Remove reload's from_state/state/applied and execute's executed-runnable echoes.
Run still delivers `{run_id: RunRef, output_type: str, output: JSONValue}`, without
the duplicate runnable field. Preserve errors, diagnostics, child output delivery,
and CLI/API control response formats.

Keep newly committed controls in memory for online adoption; do not read back
receipts to discover their effects. `triggered_by` identifies the creating Tool
Step, `preceded_by` records adoption, and `aborted_by` records interruption.
A later failure does not roll back a committed control. Reused controls retain
their original trigger. Restart recovery uses existing records and relations.

## Pick, honor, and shared recall

```javascript
_toolang/pick({kind: "skill" | "service", ref: "<catalog ref>"})
_toolang/honor({paths: ["<absolute access path>", ...]})
```

Pick resolves one exact ref allowed by the effective binding. Keep skill and
service catalogs separate; their protocol guidance directs the model to pick
applicable, missing guidance. Pick grants no capability and performs no MCP
connection/auth/discovery: those remain service/* operations. Reading me/get is
authored-data inspection, not recall. Online resolution may use the effective
State; persist the original text so replay never needs that State.

Honor receives nonempty normalized access paths, not AGENTS.md paths. Runtime
maps them to configured workspaces and applicable AGENTS.md files. A rules target
is a workspace name plus its scope directory relative to that root (`/`, `/src`).
Recall keeps the existing targets and wrappers:

```text
rules:   {workspace, path} → <rules workspace="..." path="..." revision="...">...</rules>
skill:   {ref}             → <skill ref="..." revision="...">...</skill>
service: {ref}             → <service ref="..." revision="...">...</service>
```

Revision is the SHA-256 fingerprint of the exact UTF-8 recalled text, not the
whole State revision. Store `{target, revision, content}` in an applied recall
control; escape wrapper attributes, not stored content. Content enters a separate
user message through the existing delta reference to that control.

### Visibility and reuse

Visibility means inclusion in the selected near/now of a committed ModelCall.
Derive it from saved delta references to recall controls, not XML/text matching,
raw control scans, or the fact that a Tool Step loaded a resource. Far, excluded
near, authored look-alike tags, and unadopted controls do not count. For each target,
the last visible recall determines its effective revision; an older matching
revision does not override a later different one.

| Current target/revision | Shared pick/honor decision |
| --- | --- |
| Latest pending recall for this target matches | Reuse it; it is still not visible. |
| No pending recall and the visible revision matches | Return no control. |
| Otherwise | Capture content and create a recall control. |

Pending here means an eligible applied recall not yet adopted in this Run/attempt,
not ControlRecord.status=pending. Never reuse an earlier matching
pending control across a later different revision: append the current revision
after it so adoption order remains correct. Do not rewrite earlier controls.

An adopted control whose content left the view is not pending work to reuse.
A new recall records its renewed presentation. Concurrent calls in one Run share
pending recalls; different Runs keep separate adoption. Compare against the
committed request that led to the tool batch, never discarded preparation or a
newly pending user message.

Maintain visibility alongside live resolved prefixes; recover it from the same
selected deltas/controls. Update it when a Model Step commits, including changes
from execute/reload/compact or recall selection. Do not add a second durable
visibility log or reread/rerender stable prefixes on every tool call. The model
picks missing guidance; assembly does not automatically restore it.

### Tool call preflight

Plugins declare concrete paths; runtime owns rule discovery, revision checks,
recalls, and retry. Preparation and execution share path resolution, defaults,
and authorization. Normalize/authorize paths before loading rules, and perform no
requested read/write/shell action before preflight passes. Plugins do not implement
their own message-history or recall algorithms.

Start with explicit fs targets and shell cwd, including reads. Discover only
ancestor/scoped rules applicable to those paths, ancestor before descendant and
deduplicated by target. Do not load every workspace or rules from untouched
descendants. This does not intercept paths hidden in shell commands and is not
a sandbox guarantee.

Rule-file reads must also satisfy existing read authorization; workspace
membership alone does not authorize following an AGENTS.md link outside it.

```text
Model ToolCall → path preflight
  ├── rules current: original Tool Step → execute → ordinary ToolResult
  └── missing/stale: honor Tool Step → recall control(s)
      → original Tool Step → error="operation not executed; retry required", output={}
Next ModelCall: original ToolResult → rules user message(s) → model retries
```

Honor finishes before allocating the blocked original Tool Step, keeping ordinary
Step ordering without overlapping sibling Steps. No-op preflight creates no honor
Step. Pending recalls may be shared, but no blocked operation proceeds before the
model receives its rules. Loading errors also block execution. Cancellation still
completes/skips each announced original ToolCall with a durable ToolResult.

Recheck paths/revisions on retry, including after reload/compact. Complete the
original model tool exchange before inserting recall user messages; honor receipts
do not enter that exchange. Workspace configuration does not expand current
fs/shell permissions: both still restrict access to agent home. External workspace
access requires separate authorization/filesystem work.

## History toolset

Expose the existing RunHistory/View readers through a read-only user toolset.
Read records, not assembled messages. Pages may split a model/tool exchange;
retain references/dependencies instead of adding message grouping.

```text
First page:
  history/read_threads(limit=20)
  history/read_runs(thread?, begin?, end?, limit=20, from_end=false)
  history/read_steps(run, begin?, end?, limit=20, from_end=false)
  history/read_output(run)
Continuation: the same list tool with cursor only

read_threads → {threads: ThreadRecord[], cursor: str | null}
read_runs    → {thread: ThreadRef, head: ControlRef, runs: RunRecord[], cursor: str | null}
read_steps   → {run: RunRecord, entries: (StepRecord | ControlRecord)[],
                dependencies: ControlRecord[], cursor: str | null}
read_output  → {run: RunRef, status: RunStatus, output: Local | null}
```

- Thread defaults to the caller's Thread; Run is required. Bounds are RunRefs
  within the selected logical Thread or StepRefs within the selected Run.
  Ranges are `[begin, end)`; omitted bounds are open, equal bounds are empty,
  and reversed/foreign bounds fail.
- Limit is a positive primary-record count, not a token/byte budget. from_end
  selects from the tail while each page remains naturally ordered. Threads use
  newest-updated-first order with id as tie-break; Steps use numeric Step order.
- Cursor fixes agent, tool, target, range, direction, and limit. Reject mixed
  cursor/query arguments and wrong-tool cursors. Omitted/null starts a read;
  returned null ends it. Preserve fixed membership across append/rewind and
  restart; changed captured facts invalidate continuation, without MVCC.
- Add paged Thread-record listing: capture ID order, exclude later Threads,
  and read metadata as of each page. Preserve existing CLI list semantics.
- Preserve physical ownership in records and logical Thread membership in
  read_runs. Unbounded read_steps includes unused owned controls; Step-bounded
  reads include selected Steps and required controls. Dependencies may recur
  across pages; child internals require explicit child reads.
- Use canonical serialization. Resolve selected Step outputs and Control input
  Locals, including dependencies; preserve structural and stored ModelCall refs.
  read_output uses get_output and returns `{type, value, name, dim}` or null.
  Preserve status/partial data; missing targets or unresolved values fail.

Reads stay inside the current agent's Store and create no controls or injected
user messages. Compact Runs use these tools for target records and earlier compact
outputs; add no special recall directive, latest-compact tool, or hidden read path.

## Compaction follow-up

```text
_toolang/compact({thread: ThreadRef, begin: RunRef | null = null, end: RunRef})
  → {controls: ControlRef[]}
compact.too input:  {thread, begin, end}
compact.too output: {thread, begin, end, summary: Text}
```

Model call preflight prepares a candidate and checks its input budget before
allocating a Model Step. Select a complete prefix of the calling Run's Thread:
begin is null or its first logical root; exclusive end retains at least one
historical root before the active root. Never drop now to make history fit.
Pass the range unchanged to compact.too; callers cannot supply summary.
Validate that output echoes thread/begin/end and contains a complete-prefix
summary before creating a compact control.

```text
prepare candidate → budget check → compact Tool Step
  → compact.too root Run in compact_<thread> → validated output
  → compact control {horizon: compact Run output reference}
  → prepare again → commit Model Step → dispatch
```

The discarded candidate is not a Model Step. The compact Run is a root in its
own Thread, not a cross-Thread child. Its internal Steps stay outside target
conversation/progress. Its explicit authorized tools include history; bounded
reads and reduction belong to compact.too. Compact Runs do not recursively compact.

At new root creation, reuse an applicable validated result located by
RunHistory.get_compaction and fix its output reference in the run payload's
horizon; otherwise start with no horizon. Children retain existing parent-horizon
inheritance. Later changes use compact controls, never a replay-time latest lookup.

Use one cross-process permit per target Thread, not a ban on requests while a
root Run is active. Waiters recheck budget/range after admission and reuse valid
same-range output. Each calling Run needs its own compact control unless adoption
is already satisfied. Return no control when no work/adoption is needed; reject
invalidated arguments rather than silently changing the recorded range.

Wait outside Store transactions and Model Step begin locks. Reprepare with any
intervening reload/steer/cancel. Canceling a waiter does not cancel another caller's
work. Only validated durable outputs produce compact controls; failure does not
invent a horizon. Committed facts survive interrupted delivery, and horizon
changes only through existing recorded adoption.

Before implementing compact, define the budget policy: account for actual
candidate input (instructions/messages/tools/output contract), reserve output
capacity/headroom from the model context window, and choose a near retention
budget with at least one historical root. Specify the estimator, defaults,
missing metadata, and oversized irreducible now behavior. Do not silently
truncate, repeatedly compact an ineffective range, or use the Run's cumulative
token/cost limit as its context-window budget.

## Implementation PRs and acceptance

Each PR includes progress, cancellation, recovery, and replay checks; these are
not a final cleanup phase. Use ordinary Tool Step summaries such as "Loading
workspace rules: toolang /src" and "Compacting history". Distinguish loaded from
adopted and retry-required from a completed filesystem operation.

| PR | Scope and likely touchpoints | Acceptance |
| --- | --- | --- |
| 1 — Shared tool execution | execution/tools/runtime, base tool protocols/context, ToolStepGiven/codec, plugin loading, executor dispatch, entry points/defaults | Migrate run/execute/reload and names/receipts; preserve authorization, trigger provenance, per-call isolation, execute transfer, and model exchanges. No new tool bodies. |
| 2 — Pick and recall visibility | Runtime plugin, executor recall handling, existing delta selection, protocol catalogs | Allowed refs; exact text/revision; last-visible/pending reuse and revision reversions; batches, changed/excluded history, State-free replay. No context deduplication. |
| 3 — Honor and tool preflight | Shared path preparation, fs/shell, executor recall handling | Scoped/nested rules, reads/writes, batch reuse, changed files, failed loads, zero side effects before retry, no orphan runtime tool messages. |
| 4 — History toolset | execution/tools/history, RunHistory/cursors, record serialization | Four contracts; cursor-only/wrong-tool checks, fixed ranges, dependencies/unused controls, resolved/partial output, fork/rewind, children, restart; no ModelCall rebuild. |
| 5 — Compact and model preflight | Budget policy, executor coordinator, bundled compact.too | Full-prefix output, retained near, concurrent waiters, no recursive compact, cancel/failure/restart, intervening controls, unchanged prior calls, no-progress handling. |

Prioritize PRs 1–3 for honor/pick. PR4 is independent after PR1 and must precede
PR5. Register each new tool with
its implementation, not a placeholder operation in PR1.

Across all PRs verify durable Step/control order, no duplicate adoption or child
results, commit-before-delivery failures, and equality of online requests and
State-free replay. Run Ruff check/format, ty, and the default offline pytest suite
before every commit. Validate links and keep changes within the PR's scope.

## Remaining decisions and risks

- Before PR3, settle rules deletion/retraction and overlapping-workspace scope
  ordering. An absent file must not silently make stale visible rules authoritative;
  these cases need explicit tests/protocol wording, not accidental path behavior.
- Before PR5, approve the budget policy above. Tool contracts and the existing
  horizon/assembly semantics do not determine thresholds or token sizing.
- Main risks: stale visibility, side effects before honor succeeds, runtime
  results leaking into model exchanges, and lost/duplicated effects at commit
  boundaries. External workspace permissions, general shell interception, and
  Step-level compaction remain separate definitions.
