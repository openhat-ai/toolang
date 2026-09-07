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
_toolang/honor({paths: [{workspace: "<name>", path: "/<relative access path>"}, ...]})
```

Pick resolves one exact ref allowed by the effective binding. Keep skill and
service catalogs separate; their protocol guidance directs the model to pick
applicable, missing guidance. Pick grants no capability and performs no MCP
connection/auth/discovery: those remain service/* operations. Reading me/get is
authored-data inspection, not recall. Online resolution may use the effective
State; persist the original text so replay never needs that State.

Honor receives nonempty normalized workspace-relative access paths, not AGENTS.md
paths. A leading `/` denotes the workspace root, not a host absolute path. Runtime
discovers applicable AGENTS.md files within that workspace. A rules target is its
workspace name plus scope directory (`/`, `/src`). Recall keeps the existing
targets and wrappers:

```text
rules:   {workspace, path} → <rules workspace="..." path="..." revision="...">...</rules>
skill:   {ref}             → <skill ref="..." revision="...">...</skill>
service: {ref}             → <service ref="..." revision="...">...</service>
```

Store `{target, revision, content}` in an applied recall control; escape wrapper
attributes, not stored content. Content enters a separate user message through
the existing delta reference to that control.

### Revisions and removal

Revision is a hex-encoded uint256: SHA-256 of the exact UTF-8 recalled text for
present content, and zero for removal. Normalize once at the boundary:

- Accept 1–64 hexadecimal digits, without `0x`; short forms are allowed.
- Store and compare zero as `"0"`; store nonzero values as 64 lowercase digits,
  preserving leading zeroes.
- A removal has `revision="0"` and `content=""`. An empty existing file has its
  ordinary nonzero content hash, so it is not removal.

The protocol defines revision zero as retracting earlier content for the same
target, using the same wrapper/control and visibility rules. For applicable
previously recalled rules, discovery checks confirmed removal as well as existing
files; retry must present the retraction before the operation proceeds. An absent
file never recalled needs no retraction. Authorization/read errors are failures,
not removal. These rules do not change State revisions or ContentRef encoding.

### Workspace identity

Workspace name plus relative path is the logical identity on both host and
sandbox. Preserve that anchor through path preparation, honor, and execution;
resolve the physical path without changing the logical anchor.

Overlapping workspaces remain independent. For example, `repo:/sdk/AGENTS.md`
and `sdk:/AGENTS.md` may be the same file, but produce distinct targets
`{workspace: "repo", path: "/sdk"}` and `{workspace: "sdk", path: "/"}`.
Allow this redundancy; deduplicate by target, not physical file. Rules inherit
from ancestors within the selected workspace only, not from host-only nesting.
Bare paths matching multiple workspaces require an explicit workspace anchor;
do not choose the deepest physical root. This adds no filesystem authorization.

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

Plugins declare concrete access paths; runtime owns rule discovery, revision checks,
recalls, and retry. Preparation and execution share path resolution, defaults,
authorization, and workspace anchors. Normalize/authorize paths before loading
rules, and perform no requested read/write/shell action before preflight passes.
Plugins do not implement their own message-history or recall algorithms.

Start with explicit fs targets and shell cwd, including reads. Discover only
ancestor/scoped rules applicable to those paths, ancestor before descendant within
each workspace and deduplicated by target. Preserve access order across workspace
anchors; physical nesting gives no cross-workspace priority. Do not load every
workspace or rules from untouched descendants. This does not intercept paths
hidden in shell commands and is not a sandbox guarantee.

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

## Compaction and model call preflight

```text
_toolang/compact({thread: ThreadRef, begin: RunRef | null = null, end: RunRef})
  → {controls: ControlRef[]}
compact.too input:  {thread, begin, end}
compact.too output: {thread, begin, end, summary: Text}
```

Model call preflight prepares a candidate and checks its input budget before
allocating a Model Step. At compaction, choose the boundary using the near retention
budget and select a complete prefix of the calling Run's Thread: begin is null or
its first logical root; exclusive end retains at least one historical root before
the active root. Between compactions, keep the history boundary fixed while new
messages append; do not slide or trim near on each ModelCall. Never drop now to
make history fit.
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
Rerun also prefers the latest applicable result, falling back to the source Run's
explicit horizon when no newer result is available.

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

### Budget policy

Resolve the output budget first from the request configuration or default, within
the model's output limit. Pass this same budget to the adapter as the actual output
limit and reserve it when calculating input capacity. Do not automatically reserve
the model's maximum supported output. Account for reasoning according to adapter
semantics, without counting it twice.

```python
input_budget = min(context_window - output_budget, input_limit) - safety_margin
```

Omit the `input_limit` term when no independent input limit exists. Normalize model
metadata: `context_window` is the combined input/output capacity, not an input-only
limit. Reserving the full output budget is runtime policy, even if the provider
accepts a request that could run out of context during generation.

- Estimate the complete candidate: instructions, selected far/near/now, tools,
  output contract, and newly included honor/pick content. Proceed when the estimate
  is at most `input_budget`; otherwise compact before committing the Model Step.
- Use the near retention budget only to choose a compaction boundary. Root-only
  boundaries and the mandatory retained historical root take precedence over that
  target; Step-level splitting is deferred. No fixed post-compaction percentage
  is required. Reject already-oversized mandatory content before starting compact.
  This lower-bound check excludes newly staged historical tails, which may shrink
  when the horizon advances; committed now never shrinks.
- Give compact's model calls their own output budgets. After adopting the result,
  reprepare and recheck the complete candidate, not just the summary. If it still
  exceeds budget and no valid boundary can advance, fail explicitly. This includes
  oversized fixed content, mandatory near, or now; never silently truncate or
  repeat compaction of an ineffective range.
- Maintain estimates in memory using recent valid provider usage plus estimates
  of appended content while the request prefix is unchanged. Rebuild the baseline
  after compaction or relevant binding changes. Never use cumulative Run usage as
  context size or reread stable records on every call.

PR5 policy:

- Default output: 4096 tokens, capped at the known model output limit. Honor
  native adapter configuration; Messages thinking must fit inside this budget.
  Persist `ModelCall.max_output_tokens` (Store schema 42) and replay it unchanged.
  OpenAI's output limit includes reasoning tokens; see its
  [token-counting guide](https://developers.openai.com/api/docs/guides/token-counting).
- Responses continuation retains a fingerprint of the request prefix. A changed
  prefix starts a fresh provider context and resends the selected tool exchanges;
  unchanged prefixes continue using the previous response. Other adapters keep
  their own continuation semantics. Preserve the Responses reasoning items that
  precede retained tool calls, following its
  [context-management guidance](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context).
- Safety margin: 5% of the limiting input capacity, at least 1024 tokens. Near
  target: half the input budget, rounded down; retain the last historical root
  regardless of its size. Each compact advances at least one root.
- Initial estimate: UTF-8 bytes / 3, rounded up, with serialized roles, tools and
  output schema, 8 tokens per message and 32 request overhead. Add 4096 per image,
  audio or document part. This is conservative accounting, not a tokenizer or a
  guarantee for arbitrary media. Positive inclusive provider input usage replaces
  the estimate for an unchanged prefix; otherwise reuse the estimated prefix.
  Horizon, State, model or recall changes invalidate calibration.
- Unknown limits disable automatic capacity checks, not output limits. A known
  independent input limit still applies. Never infer a missing context window.
- `compact.too` first inspects earlier compact outputs, then uses fresh child
  Runs for bounded history pages and rolling reduction. At most 1024 page
  iterations; incomplete coverage produces no usable summary. An individually
  oversized history record fails explicitly: field slicing and recursive compact
  are not implemented. Only read-only history tools are available to this program,
  loaded through the normal factory/registration path independently of the human
  Run's tool selectors. Loading compact does not initialize unrelated plugins.
  Compact model selection is independent of the normal Run (see below).
- Admission uses a cancellable OS file lock beside the Store, one per target
  Thread. No Store transaction or Step-begin lock spans the wait. Canceling an
  admitted caller cancels its own compact Run; canceling a waiter affects no owner.

## Implementation PRs and acceptance

Each PR includes progress, cancellation, recovery, and replay checks; these are
not a final cleanup phase. Use ordinary Tool Step summaries such as "Loading
workspace rules: toolang /src" and "Compacting history". Distinguish loaded from
adopted and retry-required from a completed filesystem operation.

| PR | Scope and likely touchpoints | Acceptance |
| --- | --- | --- |
| 1 — Shared tool execution | execution/tools/runtime, base tool protocols/context, ToolStepGiven/codec, plugin loading, executor dispatch, entry points/defaults | Migrate run/execute/reload and names/receipts; preserve authorization, trigger provenance, per-call isolation, execute transfer, and model exchanges. No new tool bodies. |
| 2 — Pick and recall visibility | Runtime plugin, executor recall handling, existing delta selection, protocol catalogs | Allowed refs; exact text and canonical revision, including short/zero forms; last-visible/pending reuse and revision reversions; batches, changed/excluded history, State-free replay. No context deduplication. |
| 3 — Honor and tool preflight | Shared path preparation, fs/shell, executor recall handling | Scoped/nested rules; deletion versus empty files and failed reads; restoration; overlapping workspace anchors unchanged across host/sandbox, no physical deduplication; reads/writes, batch reuse, zero side effects before retry, no orphan runtime tool messages. |
| 4 — History toolset | execution/tools/history, RunHistory/cursors, record serialization | Four contracts; cursor-only/wrong-tool checks, fixed ranges, dependencies/unused controls, resolved/partial output, fork/rewind, children, restart; no ModelCall rebuild. |
| 5 — Compact and model preflight | Budget policy, model metadata/adapter limits, executor coordinator, bundled compact.too | Matching output limit/reservation, independent input limits, exact-budget threshold, estimator reset; full-prefix output, stable retained near, concurrent waiters, no recursive compact, cancel/failure/restart, intervening controls, unchanged prior calls, irreducible input/no-progress errors. |

Prioritize PRs 1–3 for honor/pick. PR4 is independent after PR1 and must precede
PR5. Register each new tool with
its implementation, not a placeholder operation in PR1.

Across all PRs verify durable Step/control order, no duplicate adoption or child
results, commit-before-delivery failures, and equality of online requests and
State-free replay. Run Ruff check/format, ty, and the default offline pytest suite
before every commit. Validate links and keep changes within the PR's scope.

## Independent compact model selection

```toml
[allow]
models = ["provider-a/*", "provider-b/*", "*"]
# Equivalent collection query: models = "provider-a/*, provider-b/*, *"
[default]
model = "provider-a/model effort=medium"
[compact]
model = "provider-b/model effort=low"
```

- `allow.models` defines authorization and ordering. Without a query (including
  `all`), rank exact provider IDs: alibaba, anthropic, deepseek, google, meta,
  minimax, mistral, moonshotai, openai, openrouter, xai, zai, zhipuai. Append other
  providers in catalog order; retain catalog order within each provider. This
  default excludes no models. Explicit `*` retains catalog order.
- Omitted `compact.model` selects the first available, allowed model supporting
  both tool calls and structured output. Apply session/request model ceilings too,
  but not the normal runnable's model directive. Unknown capabilities do not
  qualify. No eligible model produces a clear error when compaction is needed.
- Explicit `compact.model` requires an exact model plus supported parameters,
  using the existing model-body syntax. It must pass the same authorization and
  capability checks. `unset` disables compaction. Never inherit the normal model
  or parameters; never silently fall back to another model after failure.
- Precedence: CLI `--compact 'model=MODEL effort=high'`, environment
  `TOOLANG_COMPACT_MODEL`, agent config, root config, automatic selection. These
  are runtime startup settings, not per-run model overrides. Existing runtimes
  must be configured at their own startup. `allow` and `default` keep their
  existing environment/CLI options. There is no `compact.models` setting.
- Setup parses configuration once; the executor selects from the effective
  authorized collection when compact starts. Persist the selected request using
  existing Run records, without another schema or replay dependency on Setup.

Touchpoints: setup configuration/publication and model-cache invalidation,
CLI/runtime startup forwarding, executor compact selection, and offline tests.
Acceptance: default/explicit ordering, string/list queries, unavailable or
unauthorized models, unknown capabilities, independent effort, disabled compact,
layer precedence, host/guest CLI propagation, and online/replay equivalence.

## Risks and exclusions

Main risks are stale visibility after removal, loss of workspace identity across
environments, side effects before honor succeeds, stale token estimates, runtime
results leaking into model exchanges, and lost/duplicated effects at commit
boundaries. External workspace permissions, general shell interception, and
Step-level compaction remain separate definitions.
