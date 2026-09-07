# Runtime tool progress

Status: proposed feature definition; implementation requires approval of this plan.

## Goal and verified baseline

Make runtime operations recognizable in Script and Chat run progress, with clear
activity, outcomes, and failures. Readers must distinguish preparing context from
performing a user operation without interpreting tool names or control receipts.

Verified against `origin/main` at `7c1add70` (compact preflight, #494, workspace
URIs, #495, and AgentState snapshots, #496):

- `_toolang` owns run, execute, reload, pick, honor, and compact. Pick/run/execute/
  reload are model-triggered; honor/compact are runtime-triggered.
- `ToolStepGiven` already supplies the call, plugin, summary, and `trigger`.
  `StepEnd` supplies status, result, error, and a terminal summary.
- The shared `ProgressProjector` specializes run and execute. Other runtime calls
  use ordinary tool rows and result surfaces. Compact already has human-readable
  executor summaries, but its receipt still appears as tool output.
- Honor ends before the blocked original Tool Step. That original call returns
  `operation not executed; retry required`, even when honor succeeds. A rules
  preflight that needs no recall creates no honor Step.
- Honor arguments identify accessed paths; `load_rules` discovers the actual
  `AGENTS.md` scopes. Its receipt contains only control references. Current events
  expose neither those rule effects nor an explicit original-call/preflight link.
- Preflight-required retries use the same failed Tool Step/error channel as real
  tool failures. A summary alone cannot safely distinguish the two for rendering.
- `prompts/instruct.default.md` and `prompts/filesystem.md` already tell the model
  to follow recalled rules and retry, but do not constrain retry narration.
  Authored instructions can replace the default; filesystem guidance is injected
  only when fs tools are selected, leaving shell-only custom instructions uncovered.
- Compact waits and runs an independent program without forwarding its internal
  events to the caller's progress. Receipts identify controls, not their adoption.
- Script's Live area currently refreshes only on updates (`auto_refresh=False`).
  Chat has a Run-level elapsed ticker, but no compact-specific elapsed display.
- Progress metrics currently count every Tool Step as a tool call.

Related contracts: [runtime tools](runtime-tool-calls.md),
[runtime results](runtime-tool-results.md), and
[execution progress](execution-progress-state-machine.md).

## Scope and decisions

Include all six runtime operations in the shared Script/Chat presentation,
including parallel lanes, failures, cancellation, and footer counts. Keep the
existing run/execute structural grammar. Treat compact as a long-running operation
with immediate visibility and elapsed-time refresh independent of event arrival.
Add typed preflight provenance and honor result facts to existing Step payloads,
so routine protocol retries can be hidden without hiding real failures. This
includes event/record codecs and the Store schema-version update required for the
changed encoding. Preserve execution behavior, Step kinds/statuses, tool result
contracts, controls, message ordering, CLI flags, and operational startup/shutdown
progress. Extend model-facing runtime instructions so routine preflight recovery
proceeds without narration. Inspection retains complete results and can expose
the new facts.

### Classification

Use one presentation-owned classifier for the exact reserved `_toolang__`
namespace and its known leaves. Responsibility and provenance are independent:
model-triggered pick is a runtime operation; history/fs/me/service calls are user
tools. Read `given.trigger` as provenance; never infer it from the namespace or
summary text. Do not load plugins or query the Store to classify/render an event.

Unknown reserved leaves get a generic runtime label and retain ordinary result
details; similarly named user tools remain ordinary tools. Missing or invalid
arguments use a bounded operation label and preserve the actual error.

### Presentation grammar

- Use `✧` (U+2727 WHITE FOUR POINTED STAR) for runtime activity/result rows.
  Keep `∎` (U+220E END OF PROOF) for the existing root Run footer and its current
  status styling; ordinary tool/model traces keep `•`. Normal preflight retry
  replies produce no progress row; failed preflights use `✧` for their blocked
  operation notice. Use the same runtime
  marker while active and after success/failure/cancellation; wording and tone
  express status. Natural-language labels identify the operation, so runtime
  rows remain distinct without color. Honor and compact inherently describe
  automatic work; do not add a redundant origin badge to every row.
- Runtime rows have a dedicated semantic surface, a normal active tone, and a dim
  successful tone. They have no tool-output background, JSON receipt panel, or
  separate heading/footer. Failed/canceled rows use existing error/warning tones
  and include an indented reason. Do not render resource content as Markdown.
- StepBegin starts replaceable activity; StepEnd commits one terminal outcome
  (run/execute retain their structural rules; normal preflight retry replies are
  silent as defined below).
  Preserve the existing append-only/live behavior and event ordering. Non-TTY
  output retains terminal outcomes; compact additionally gets the bounded
  start/heartbeat feedback defined below.
- Use existing width-aware wrapping. Keep the marker and operation visible;
  abbreviate live target previews, wrap committed targets/errors, and preserve
  complete failure reasons. Honor begins with `Loading workspace rules...`;
  its result lists actual rule files, not the paths accessed by the original tool.
- A successful, receipt-shaped `{controls: [...]}` result is summarized, with no
  control IDs/counts or injected content. Unexpected result shapes retain details
  through the generic fallback. Never claim controls were newly created, adopted,
  or rolled back from receipt presence or Step status alone.

| Operation | Active label | Successful outcome |
| --- | --- | --- |
| pick | `✧ Loading skill guidance: <ref>...` (or service guidance) | `✧ Loaded skill guidance: <ref>`; empty controls: `✧ Skill guidance already loaded: <ref>` (likewise service). |
| honor | `✧ Loading workspace rules...` | `✧ Loaded workspace rules: <rule files>`; empty controls: `✧ No workspace rules update needed`. |
| compact | `✧ Compacting thread history for 1m08s` | `✧ Compacted thread history in 1m12s`; empty controls: `✧ No thread history update needed (1m12s)`. Durations are illustrative. |
| reload | `✧ Reloading agent state...` | `✧ Agent state reload ready`; empty controls: `✧ No agent state update needed`. |
| run | `✧ Running <runnable>...` while awaiting child start | Keep the existing `---  run <runnable>` header, child trace, and child-ID footer; add no duplicate success row or result JSON. |
| execute | `✧ Executing <runnable>...` | Keep the existing confirmed `---  handoff to <runnable>` boundary at target StepBegin; add no receipt/success row. |

Failures use operation-specific wording (`✧ Failed to load skill guidance: <ref>`,
`✧ Failed to compact thread history after 1m12s`, etc.); cancellation explicitly says
`Canceled`. Preserve run failure ownership/error-reference deduplication and
execute's existing committed-transfer handling, including failure before target
start. Runtime activity/error markers also apply inside parallel lanes; structural
run/handoff boundaries remain governed by existing lane projection.

Keep every blocked Tool Step and its model-facing error in execution records.
Its typed preflight outcome controls visibility as defined below. `Loaded`
describes runtime recall preparation, not model adoption.
Loading service guidance does not mean connecting to the service.

For honor, capture each discovered target/revision whose recall returns a control
(including a reused pending recall). Build logical file labels from that verified
workspace/scope plus `AGENTS.md`, e.g. `repo:/src/AGENTS.md`; never derive them from
the original access path or resolve host paths in the renderer. Deduplicate by
logical target and retain discovery order, ancestor first within each workspace.
List multiple files separated by commas in one terminal outcome and wrap as needed.
Revision-zero removals use `Removed workspace rules: <files>`; a mixed outcome
uses `Loaded workspace rules: <files>; removed: <files>`. Never label a missing
file as loaded. Retain partial facts on failure/cancellation without presenting
the overall operation as successful or implying earlier recalls were rolled back.

### Preflight facts and protocol visibility

Keep honor's `{controls: [...]}` receipt as the execution contract. Enrich the
execution facts needed by progress instead of adding UI data to model messages:

```text
ToolStepGiven.preflight: WorkspaceRulesPreflight | None = None
WorkspaceRulesPreflight:
  honor: StepRef
  outcome: "retry_required" | "failed"

ToolStepNoted.rules: tuple[WorkspaceRuleRecall, ...] = ()
WorkspaceRuleRecall:
  target: RulesRecallTarget  # existing workspace + logical scope directory
  action: "loaded" | "removed"
```

- The executor supplies `preflight` on the blocked original call before its
  StepBegin. It references the exact earlier honor Step in the same Run; the
  original retains its model ToolCall identity/source. Honor success selects
  `retry_required`; a rule-loading failure selects `failed`. These facts come
  from execution, never tool arguments, receipt presence, summary/error matching,
  or adjacency. Do not reuse `preceded_by`, which records control adoption.
- Honor records ordered rule facts as each recall succeeds or reuses a pending
  control. Derive `removed` from revision zero at the owner. Persist facts with
  the Tool Step and include them in StepEnd, retaining committed partial facts
  through cancellation/result-delivery interruption. No content, revision hash,
  host path, or full control payload enters presentation metadata.
- Build honor rows from typed rule facts, not a parsed human summary or Store
  lookup. Keep `summary` as the existing readable fallback. Call variants with
  no facts remain understandable; never invent file names. The original error
  and empty output remain the exact model-facing preflight retry protocol.

| Condition | Original call presentation |
| --- | --- |
| `retry_required`, terminal failed status with the recorded blocked ToolResultPart | Hidden in progress: no activity, result, error panel, or replacement deferral notice. Honor's rule-loading outcome remains visible. |
| `failed`, terminal failed status | `✧ Blocked write: <target> — workspace rules unavailable`, error tone; honor retains the actual loading failure and full reason. |
| Terminal canceled status | Explicit canceled/not-executed wording and the existing cancellation reason; it takes precedence over the earlier preflight verdict. |
| No preflight metadata | Ordinary tool lifecycle and failure rendering, even if its error text happens to match the retry-protocol string. |

At StepBegin, `retry_required` suppresses the original call's activity immediately,
so no `Writing...`, failure flash, or fake spinner appears. Track its events,
result, error references, and metrics normally; only presentation is silent.
If StepEnd confirms the normal protocol reply, commit nothing. If it instead
reports cancellation, lacks the expected result, or reports an unexpected executor
failure, show the appropriate terminal diagnostic. Never hide a real failure
solely because the Step began behind a preflight gate.

Apply this in Script/Chat, TTY/plain output, and parallel lanes. Silent Steps
create no empty blocks, gaps, structural boundaries, or lane-activity updates.
Leave deferred boundaries for the next visible operation. Do not mark suppressed
errors as already displayed: if a Run/Flow terminal failure references one, show
the diagnostic at that terminal owner rather than losing its explanation.

Deferral is a completed protocol reply, not an open queued operation. The model
may retry after receiving the rules; the UI neither retries nor asks the human to
retry. Show the later real Tool Step only if it occurs; invent no queued task,
retry spinner, or success. The original protocol result remains available to
model assembly, persistence/replay, and inspection. Failures in honor and actual
tool execution remain visible. For blocked/error wording, use the executor's
existing tool identity/argument summary context without parsing prose in the UI.

Illustrative committed trace (synthetic resource names; timing omitted):

```text
✧ Loaded skill guidance: code-review
✧ Loaded workspace rules: repo:/src/AGENTS.md
• read_file /src/main.py
  <ordinary tool output>
✧ Compacted thread history
• <next assistant response>
```

### Model-facing preflight protocol

Hiding the protocol result does not prevent the model from narrating its retry.
The model must understand preflight because it still issues the subsequent call;
making recovery invisible to the model would require a different execution design.
Require direct continuation through runtime instructions, without filtering or
rewriting model-authored text in progress.

Add one bundled `executor/prompts/preflight.md` block with this contract:

```text
<workspace-preflight>
- Runtime owns workspace-rule discovery and recall. Its normal preflight reply,
  "operation not executed; retry required", means the requested action has not
  run. Consume the accompanying rules before continuing.
- Rules apply to their named workspace and relative directory; more specific
  scopes refine ancestor rules. Later revisions replace earlier ones for the
  same target; revision "0" retracts that target's earlier content.
- Apply the rules, adjust the intended call when needed, and issue the next
  appropriate tool call directly. Do not repeat an action the rules prohibit.
- Treat successful preflight recovery as internal execution bookkeeping. Do not
  narrate rule loading or retrying, ask the user to retry, or restate internal
  rules in commentary or the final answer merely because preflight occurred.
- Explain a rule or preflight detail when the user explicitly asks, when it
  materially changes the requested outcome, or when a real blocker or required
  user decision prevents continuation. Report actual failures accurately.
- This recovery applies only to the normal runtime preflight reply. Rule-loading
  failures, cancellation, and ordinary tool errors are not this signal. Never
  infer that an uncertain operation had no effects or blindly repeat a write.
</workspace-preflight>
```

Append this block once through `prepare.py`'s `runtime_instructions` whenever
tools are selected, independently of fs selection, runnable routes, or authored
`instruct` templates (including `instruct: none`). Retain `_model_instructions`'s
existing effective-tool/repair gate. Remove the duplicated preflight directions
from `instruct.default.md` and `filesystem.md`; keep their unrelated guidance.
Honor remains runtime-only and absent from model tool definitions. Preserve the
blocked ToolResult payload, original exchange before recalled rule messages, and
control adoption; add no model-facing execution metadata or automatic tool replay.

For the reported write, the intended trace has the runtime rule-loading row,
then the actual write and its result. The model silently applies the directory's
logging exemption; it does not insert an explanation of that exemption or a
"retrying the write" message. Ordinary task commentary remains allowed. This is
an instruction-level behavior requirement, not a guarantee that every provider
will comply; scripted adapters cannot demonstrate real-model adherence.

### Long-running compaction

Compact must remain observable throughout a long event-free interval:

- Show its active row immediately on StepBegin, even before the first normal
  Model Step. Keep it visible until compact's own StepEnd. Display one row in
  TTY/Chat, replacing it in place rather than appending each timer update.
- Refresh elapsed time once per visible second without waiting for another
  execution event. Time belongs to this compact Step, including permit waiting,
  reuse checks, and execution; do not substitute the root Run's elapsed time.
  Keep each parallel lane's timer keyed to its owning Step, without resets when
  other lanes update. Abbreviate target details before losing the operation/time.
- Anchor live timers to local monotonic time when the begin event is received.
  Live elapsed measures observed waiting, not evidence of worker activity.
  Terminal success/failure/cancel rows retain total duration from the existing
  Step timestamps using `elapsed_fact`; transport delay can make it differ from
  the live clock. Omit unavailable terminal duration instead of inventing it.
  Use `for <duration>` while active, `in <duration>` on success, and
  `after <duration>` on failure/cancellation. The canceled label is
  `✧ Thread history compaction canceled after 1m12s`.
- Keep existing cancellation controls responsive. Stop and discard timers on
  StepEnd, presenter close/disconnect, or a terminal presentation diagnostic.
  A canceled/failed operation must never leave a running timer or get rewritten
  as successful. No new timeout, automatic retry, or warning threshold is added.
- In non-TTY Script output, print one compact start line immediately, at most
  one elapsed heartbeat per active Step every 30 seconds, and one terminal line.
  Heartbeats are renderer-owned feedback, not committed progress blocks or
  execution events; they change neither metrics nor stored history.

The active label covers waiting, reuse, and execution. Current events cannot
distinguish these stages: show no phase estimate, percentage, ETA, token savings,
or claim that the worker is making progress. The next real Model Step shows
resumed work. Actual phase/activity reporting requires a separately approved
event contract; it is not a prerequisite for this elapsed-time improvement.
`Compacted` describes completion of this operation, which may reuse a validated
summary; it does not claim that new model work occurred. Receipts cannot distinguish
reuse from generation, so do not select a separate reuse label from them.

Illustrative live row and its replacement on completion:

```text
✧ Compacting thread history for 1m08s
✧ Compacted thread history in 1m12s
```

The two lines above are successive states of one TTY/Chat row, not two concurrent
rows. The completed row is retained in scrollback.

### Accounting and architecture

- Split terminal Tool Step counts into `tool` and `runtime` in existing Run/Flow
  facts, for example `2 models 3 tools 2 runtime`. Omit zero groups. Count each
  ended Step once, including failed/canceled/no-op calls, using its saved begin
  classification. Preserve child aggregation and existing root-run count rules.
  This changes presentation counts only, not execution limits or provider usage.
- Count run/execute once as runtime calls and their children according to existing
  Run metrics. Honor and its blocked user tool count separately. Independent
  compact-program Runs/models/history tools/costs stay outside caller metrics;
  only the outer compact Tool Step is counted. Do not imply total compaction cost.
  A deferred/blocked original remains one user-tool call, even when its protocol
  reply is hidden. These are call counts, not counts of completed side effects.
  Visibility changes add no runtime calls; count honor separately.
- Put runtime classification and pure label/result projection in
  `cli/common/execution_progress/runtime.py`. Keep typed presentation vocabulary
  in `types.py`; integrate through `step_projection.py`, `projector.py`, and
  `state.py`. Both sequential traces and lane activity use the same classifier.
- Define the preflight/rule vocabulary in `execution/types.py`; extend existing
  given/noted codecs in `execution/records.py` and their event serialization.
  Validate same-Run, earlier-Step preflight references at execution boundaries.
  Keep projection independent of Store reads and resource loading. Events without
  the optional facts use ordinary/fallback presentation, not string heuristics.
- Bump the Store schema version for the changed durable encoding under the
  existing policy (42 at this baseline). Reject incompatible databases without
  modifying them; provide no automatic migration or historical backfill. CLI and
  remote runtime versions must support the new payloads. Preserve tool results,
  model assembly, and existing control relations. Replay reproduces committed
  rows; renderer-owned elapsed snapshots/heartbeats remain ephemeral.
- Carry optional compact timing metadata (owning Step and start timestamp) on
  projected live rows, including lane rows. Keep clock sampling and refresh in
  the presenters, not `ProgressProjector`; timer ticks never manufacture events.
  Script owns one refresh task for its active compact rows. Chat reuses its
  existing UI ticker, ensuring active compact rows repaint even when the status
  bar's own text does not change. Use injected clocks for deterministic tests.

## Implementation checklist and touchpoints

Unless qualified, source filenames below belong to
`src/toolang/cli/common/execution_progress/`.

- [ ] Confirm this definition and use its acceptance criteria as implementation
  scope; ship no implementation as part of this definition.
- [ ] Add the shared classifier and runtime projection in `runtime.py` and
  `types.py`, with safe fallbacks for unknown calls and malformed previews.
- [ ] Add typed preflight/rule facts and codecs in
  `src/toolang/execution/types.py`, `records.py`, and `events.py`; update the
  encoding version in `src/toolang/execution/store.py` under its existing policy.
- [ ] Capture honor's rule facts in `src/toolang/execution/executor/tool_runtime.py`;
  carry the exact honor reference/verdict into the original call and preserve
  facts through finish/cancel in `src/toolang/execution/executor/steps/tool.py`.
  Reuse `rules.py` discovery without changing recalls, retries, or adoption.
- [ ] Add the preflight protocol in `src/toolang/execution/executor/prompts/preflight.md`,
  inject it through `prepare.py`, and remove its duplicated instructions from
  `prompts/instruct.default.md` and `prompts/filesystem.md`. Preserve the existing
  `steps/model.py` gate and tool/result/recall assembly contracts.
- [ ] Integrate lifecycle and lane rendering in `step_projection.py` and
  `projector.py`; retain run/execute ownership and ordering. Update marker-aware
  hanging-prefix handling in `formatting.py` for `✧`, including parallel lanes.
  Suppress normal protocol retry rows without skipping lifecycle/error bookkeeping
  or creating blank fragments; expose non-protocol terminal failures normally.
- [ ] Render the runtime surface consistently in `rich_rendering.py`; verify
  `src/toolang/cli/common/script_progress/console.py` and
  `src/toolang/cli/toolang/commands/chat/blocks.py` consumers.
  Change consumer code only if required for the shared surface.
- [ ] Add compact timing metadata and lifecycle cleanup. Integrate Script timing
  in `src/toolang/cli/common/script_progress/presenter.py` and `console.py`, and
  Chat timing in `src/toolang/cli/toolang/commands/chat/presenter.py`, `blocks.py`,
  and `tui.py`. Keep one refresh loop per surface; preserve existing Run timing.
- [ ] Separate `runtime` metrics in `state.py` and `facts.py`, passing begin
  classification from `projector.py` and preserving aggregation.
- [ ] Add focused projection/rendering tests and extend actual-event integration
  coverage below; update execution-progress examples to match the approved grammar.
- [ ] Run default verification before every implementation commit:
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pytest`. Keep live-provider tests opt-in. Use the ordinary release path;
  no feature flag is required. Document the changed event payloads and incompatible
  Store version in the implementation release notes; do not migrate old data.

Tests belong in
`tests/unit/cli/test_execution_progress_projector.py`,
`test_agic_run_progress.py`, `test_execution_progress_facts.py`, and
`tests/integration/cli/test_runtime_progress.py`; a focused
`tests/unit/cli/test_runtime_tool_progress.py` may own the new table-driven cases.
Extend `tests/unit/cli/test_chat_tui.py` for event-independent refresh and cleanup.
Cover codecs/schema policy in `tests/unit/execution/test_events.py`,
`test_store_schema.py`, and `test_tool_step_summary.py`.
Reuse scenarios/harnesses from `tests/integration/execution/test_pick_guidance.py`,
`test_honor_rules.py`, and `test_compact_scenarios.py` without changing behavior.
Cover protocol loading/preparation in `tests/unit/execution/test_executor_prompts.py`
and `test_executor_prepare.py`; verify effective requests and message ordering in
`tests/integration/execution/test_model_assembly.py` and `test_honor_rules.py`.

## Acceptance criteria

1. Every known runtime leaf has distinct live/terminal presentation in Script and
   Chat. Pick skill/service, reload, honor, compact, run, and execute all have
   success/failure/cancel coverage; calls without preflight metadata retain their
   existing output styling.
2. Classification is independent of trigger and summary wording. Unknown reserved
   leaves, look-alike user names, invalid arguments, missing outputs, empty
   receipts, and unexpected outputs remain understandable without projector failure.
3. Actual honor events precede the separately failed original tool; retry succeeds
   normally. Cover changed/removed rules, no-op preflight, recall failure, and
   cancellation/steer. An access to `repo:/src/main.py` reports the recalled
   `repo:/src/AGENTS.md`, with root/nested rules in discovery order. Cover multiple
   workspaces, duplicate access paths, mixed removals, and old/missing summaries.
   No nonexistent rule paths, access-path substitutions, or synthetic success appears.
   Successful preflight keeps honor visible and hides the original retry reply
   entirely: no `Failed write`, `Deferred write`, `Writing...`, result panel, or
   empty gaps. Rule-loading failure produces a blocked notice with real errors.
   Identical error text from an ordinary tool remains a failure. Assert explicit
   correlation for batches, multiple paths, and repeated honors sharing controls.
   No operation runs before model retry; protocol results and recall messages
   stay unchanged.
   A model that never retries produces no phantom tool activity. Cancellation,
   missing results, and unexpected failures after a silent StepBegin remain
   visible, including Run/Flow errors pointing to a previously suppressed result.
4. Actual compact events before the first and between later Model Steps produce
   one outer runtime operation. Cover permit waiting, reuse/no-op, failure, and
   cancellation with deterministic gates. No independent-program events, fabricated
   phases, or receipt JSON appear; the next Model Step starts only when emitted.
   With StepBegin followed by a simulated multi-minute event gap, assert immediate
   visibility, advancing whole-second elapsed time, unchanged scrollback/metrics,
   independent lane clocks, and responsive cancellation. Verify non-TTY start,
   30-second heartbeat cadence, final duration, and cleanup on every termination
   path. Use fake clocks/gates, not real minute-long sleeps or live providers.
5. Run headers/child footers and confirmed execute handoffs remain correctly
   ordered, including nested/parallel work, rejected targets, committed controls
   followed by interruption, and deduplicated errors. Terminal steps clear live rows.
6. Counts are exact for mixed user/runtime calls, failures, canceled calls, no-ops,
   child aggregation, and parallel lanes; compact internals remain excluded.
   Suppression preserves call counts and adds no phantom runtime calls or executed
   user operations; other parallel lanes and structural boundaries remain intact.
7. Shared Script/Chat rendering passes TTY/non-TTY, uncolored output, narrow and
   wide widths, Unicode/long refs and workspace paths, and multiline errors.
   Assert exact marker code points, aligned hanging indents, and unchanged `∎`
   root footers; use exactly `✧`, without emoji variation selectors.
   Recorded events survive existing serialization round trips and project the same
   committed results without plugin loading or Store reads. Default offline
   verification passes.
8. Typed facts survive event round trips, persistence/restart, and interrupted
   delivery. Canceled Steps preserve captured facts while retaining canceled
   presentation. Invalid cross-Run/forward preflight references are rejected;
   absent optional facts fall back safely. Incompatible Store versions are
   rejected without modification under the existing schema policy.
9. Effective model requests contain one preflight protocol block for default,
   custom, and `none` instructions, including fs-only and shell-only selections
   and agents without runnable routes. Disabled-tool/output-repair requests retain
   their existing runtime-instruction gate. Assert the direct-continuation and
   disclosure exceptions, unchanged ToolResult/rule-message order, and honor's
   absence from advertised tools. A scripted rules-exemption/write sequence
   performs no first-attempt side effect and executes the appropriate subsequent
   call once. Preserve any actual model text; do not enforce silence with a text
   filter. Keep provider adherence checks optional and opt-in, separate from
   deterministic protocol/assembly acceptance tests.

## Risks and open questions

The main risks are hiding actual failures with protocol retries, losing
preflight correlation/partial rule facts, claiming adoption too early, duplicating
run output, and double-counting nested work. Typed execution-owned facts, unchanged
protocol replies, and interruption/batch acceptance cases address these. The Store
version change also requires a coordinated runtime/client upgrade; this definition
does not authorize migrating or deleting existing user data.
Prompt instructions reduce routine narration but cannot guarantee model silence;
they must not conceal material constraints or real failures.

No blocking design question remains. Human confirmation is required for this
proposed display grammar, model protocol instructions, execution metadata,
Store-version impact, and count split before implementation.
