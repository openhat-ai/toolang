# Runtime tool progress

Status: proposed feature definition; implementation requires approval.

## Goal and scope

Make runtime preparation visible in Script and Chat without showing control
receipts or presenting a successful rules preflight as a failed user operation.
Keep compaction visibly active while it waits or runs.

Current execution already emits ordinary Tool Steps for pick, honor, reload, and
compact. Their calls, summaries, results, and timestamps support presentation.
Run/execute already have child-run and handoff presentation; leave it unchanged.
The one missing fact is whether a blocked call is a normal preflight retry.

This PR defines the feature only. Implementation preserves tool behavior,
controls, message ordering, cancellation, ModelCall assembly, and inspection.
No new commands, configuration, Step kinds, or runtime-count category.

## Presentation

Use the existing shared progress projection for Script and Chat, including
parallel lanes. Match the four exact `_toolang__` tool names below, independently
of `given.trigger`. Other calls retain their current presentation.

Runtime rows use `✧`, plain text, and the existing tones: active while running,
dim after success, error/warning on failure/cancellation. Use the existing
unboxed surface; no new styling system. Ordinary tools keep `•`, root footers
keep `∎`, and run/execute keep their existing structural boundaries.

| Tool | Active | Successful outcome |
| --- | --- | --- |
| pick | `✧ Loading skill guidance: <ref>...` | `✧ Loaded skill guidance: <ref>` |
| honor | `✧ Updating workspace rules...` | `✧ Updated workspace rules` |
| reload | `✧ Reloading agent state...` | `✧ Reloaded agent state` |
| compact | `✧ Compacting thread history for 1m08s` | `✧ Compacted thread history in 1m12s` |

Pick uses `service guidance` when appropriate; this does not imply connecting to
the service. Honor deliberately omits individual rule files: updating includes
loading, replacing, and removing rules. Reload already waits for its control to
be applied before returning success.

For a successful `{controls: [...]}` receipt, show only the outcome, not JSON,
control IDs, or resource content. For empty pick/honor/compact receipts, use
`No guidance update needed`, `No workspace rules update needed`, or
`No thread history update needed`; retain pick's kind/ref and compact's duration.
Compaction may reuse a valid result; do not claim new work.

Failures and cancellations show the operation and its actual reason. Unknown
calls or unexpected results use ordinary tool presentation rather than losing
information. Keep existing wrapping and error deduplication. Counts remain Tool
Step counts, including runtime calls and hidden retries, not executed side effects.

## Preflight retry

Keep the existing sequence and model-facing result:

```text
honor Tool Step        -> ✧ Updated workspace rules
blocked user Tool Step -> hidden; result remains "operation not executed; retry required"
model applies rules    -> retries or changes the intended call
actual user Tool Step  -> ordinary progress and output
```

Add only `ToolStepGiven.retry_required: bool = False`. The executor sets it on
the blocked original call after honor succeeds, before that call's StepBegin.
It means no operation will execute: this Step returns the normal retry reply.
Do not infer this fact from error text, Step adjacency, or control receipts.
No preflight reference, outcome enum, or per-rule presentation records are needed.

The projector hides that Step's live row. At StepEnd it also hides the terminal
row only when status is failed and the saved ToolResult is the normal retry
reply. Cancellation, missing results, and other failures remain visible. Failed
honor does not set the flag; both the recall failure and blocked call retain
ordinary failure reporting. Track hidden Steps normally for counts and error
references; suppression must not lose a later Run/Flow failure explanation.

Persist the flag through existing record/event codecs. Bump the Store schema
version under its existing policy; incompatible databases remain untouched and
are rejected. ToolResult content, Step status, control relations, replay, and
inspection remain unchanged. There is no automatic retry or model-text filtering.

Add one bundled runtime preflight instruction, independent of authored instruct
and fs selection, using the existing effective-tools/output-repair gate:

> A normal preflight retry reply means the operation has not executed. Apply the
> recalled workspace rules and issue the appropriate next tool call directly,
> without narrating routine rule loading or retrying. Explain actual failures,
> material changes to the requested outcome, required user decisions, or details
> the user asks about. Cancellation and other errors are not this retry signal.

Consolidate the existing preflight directions in this block, retaining workspace
scope, more-specific-rule precedence, and revision-zero retraction semantics.
Remove duplicated preflight directions from default/filesystem prompts. Preserve
model-authored text: the instruction encourages direct continuation, not silence
at the cost of hiding a material constraint or failure.

## Compaction elapsed time

Show compact immediately on StepBegin, including before the first Model Step.
In TTY/Chat, refresh the existing live row once per second without waiting for
execution events. Use that Step's start time, not the root Run's; the elapsed
time includes permit waiting and execution, with no phase or percentage estimate.
Keep timing independent between parallel lanes.

Reuse existing timestamps and duration formatting. Presenters own refresh;
timer ticks neither create execution events nor append scrollback. Reuse Chat's
ticker and give Script one refresh loop. Stop refreshing on StepEnd or presenter
close. The terminal row includes total elapsed time for success, failure, or
cancellation. Non-TTY output prints one compact start line and one terminal line;
no heartbeat. Compact's independent internal Run remains outside caller progress.

## Implementation touchpoints

- `src/toolang/execution/types.py`, `records.py`, `events.py`, and `store.py`:
  the retry flag, codecs, and schema version only.
- `src/toolang/execution/executor/steps/tool.py`: supply the retry flag from the
  successful honor path; preserve the existing execution/result lifecycle.
- `src/toolang/execution/executor/prepare.py` and `prompts/`: the common preflight
  instruction and removal of its duplicated directions.
- `src/toolang/cli/common/execution_progress/`: shared runtime rows and retry
  suppression in the existing Step projection; retain lifecycle bookkeeping.
- `src/toolang/cli/common/script_progress/` and
  `src/toolang/cli/toolang/commands/chat/`: elapsed refresh and cleanup using the
  shared rendering path. Add only the live timing data needed for rendering.

No honor result expansion, new presentation hierarchy, per-call timer tasks,
metric split, or changes to compaction execution are required.

## Acceptance

1. Script and Chat show the four operations consistently, including empty
   receipts, failure/cancel, narrow output, and parallel lanes. Successful
   receipts disappear; ordinary tool output, run/execute boundaries, counts,
   and error ownership remain correct.
2. Real honor/retry events show rules preparation followed by the eventual tool
   execution, without a failed-write flash or empty block for the normal retry.
   The first attempt has no side effect. An ordinary tool returning identical
   error text stays visible; failed honor and interrupted delivery stay visible.
   No later retry means no invented tool activity.
3. The flag survives record/event round trips and restart; projecting recorded
   events reproduces committed rows without Store lookups. Existing ToolResult
   and recall-message order is unchanged. Schema rejection leaves old data intact.
4. Default, custom, and disabled authored instruct all receive the common protocol
   when tools are effective, including shell-only selections. Disabled tools and
   output repair retain their existing gate. Model text is never filtered.
5. With a fake clock and an event-free wait, compact is immediately visible and
   its elapsed time advances; success, failure, cancellation, and close stop the
   timer. Non-TTY output has only start/end lines. No internal compact events leak.

Extend existing progress-projector, Chat TUI, runtime-progress, honor-rules,
executor-prepare, event-codec, and Store-schema tests. Keep tests offline and
deterministic; run `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`, and `uv run pytest` before committing implementation.

## Risks and approval

The retry flag is necessary to suppress activity at begin without hiding ordinary
errors; it is the only durable addition. Its schema change requires the existing
coordinated upgrade policy. Prompt compliance is model-dependent and cannot be
guaranteed by scripted tests. No open design choice remains; human approval of
this reduced scope is required before implementation.
