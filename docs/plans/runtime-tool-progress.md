# Runtime tool progress

Status: proposed feature definition; implementation requires approval.

## Goal and call ownership

Show runtime work clearly without changing which ToolResults the model receives.
Only workspace-rules preflight needs a synthetic result and special visibility.

| Call | Initiator | ModelCall assembly | Progress |
| --- | --- | --- | --- |
| pick / reload | Model | Normal ToolCall and ToolResult; controls keep their existing effects | Normal tool presentation, customized wording/marker |
| compact | Runtime, before a Model Call | No compact ToolCall/ToolResult; compact control updates horizon | Normal tool presentation, customized wording/marker and elapsed time |
| honor | Runtime, before a workspace operation | No honor ToolCall/ToolResult; recall controls supply rules | Show honor with customized wording/marker |
| Blocked workspace call | Model; result supplied by preflight | Preserve the original ToolCall and supply a non-error retry notice | Hide this synthetic exchange |
| A later workspace retry | Model, if it chooses to retry | Normal ToolCall and ToolResult | Normal tool presentation |

Keep pick/reload/compact result contracts unchanged. Do not introduce a common
runtime-result envelope. Keep run/execute presentation, Steps, events, Store
schema, and ModelCall assembly mechanisms. No visibility settings, metric
redesign, general plugin summary API, or fs wording sweep in this implementation.
This PR defines the feature only.

## Honor preflight result

Keep the current order: path check, honor, then answer the blocked workspace call.
Runtime must persist or reuse the applicable recall controls before returning the
notice. The workspace operation has not executed, and runtime does not retry it.

Define only this special output in the toolset/runtime protocol in `toolang/base`:

```json
{
  "type": "_toolang/preflight",
  "tool_call_id": "<blocked-workspace-call-id>",
  "message": "Workspace rules reloaded; retry required",
  "controls": [
    {
      "ref": "<recall-control-ref>",
      "workspace": "repo",
      "path": "/src",
      "revision": "<content-hash>"
    }
  ]
}
```

The internal honor invocation receives the original workspace tool_call_id and
returns this receipt. Runtime uses it as the original workspace ToolResult's
output, preserving that call's name and identity. Honor's own result remains
internal. The extra ID lets progress identify the blocked call before its begin
event; it is not a new Step/control relationship field.

Use `error=None`: successful preflight is not a tool failure. Under the existing
result lifecycle, delivery of the synthetic result ends its Step successfully;
this means a protocol reply was delivered, not that the workspace operation ran.
The qualified type identifies the protocol; do not infer it from error text.

Control refs must already exist. Workspace/path identify actual recalled rules
scopes, not the attempted access path; revision `0` denotes removal. Content stays
in recall controls rather than being copied into the notice. Controls become
visible through existing adoption and message assembly, not through progress.

```text
Execution                              Model messages / binding
honor -> durable recall controls       (no honor tool exchange)
workspace call -> synthetic result     tool: Workspace rules reloaded; retry required + refs
next Model Call adopts recalls         user: <rules ...>...</rules>
model may issue another workspace call assistant: workspace tool call
workspace operation executes           tool: actual result
```

Honor failure/cancellation is not this successful-preflight receipt. Keep the
real failure visible and preserve existing interruption behavior. Do not create
a success notice when recall or result delivery fails.

## Presentation

Use existing tool lifecycle rendering in Script and Chat, including normal
results for pick/reload/compact. No new receipt format or special result
projection for those calls. Internal runtime results being visible to humans
does not make them model messages.

Runtime tools own their wording; put it in the existing given/noted summaries.
Keep this customization local to runtime tools and the existing executor summary
path, without extending every plugin/factory. Progress owns markers, tones,
wrapping, and elapsed-time decoration.

| Tool | Active wording | Successful wording |
| --- | --- | --- |
| honor | `Reloading workspace rules...` | `Reloaded workspace rules` |
| pick | `Loading skill guidance: <ref>...` | `Loaded skill guidance: <ref>` |
| reload | `Reloading agent state...` | `Reloaded agent state` |
| compact | `Compacting thread history` | `Compacted thread history` |

Pick uses service guidance where appropriate. Failed/canceled operations keep
their actual reasons and status wording. Use `✧` for these runtime rows, retaining
ordinary tool `•`, root footer `∎`, and existing run/execute boundaries.

### Hide only the synthetic workspace exchange

On successful honor StepEnd, read its receipt and remember the blocked call ID
within that Run. This event precedes the blocked workspace StepBegin. Suppress
that call's live activity, terminal rows, result panel, and layout gaps; show the
honor row instead. No second notice or failed-workspace label appears.

Keep normal event bookkeeping, counts, and error ownership. Runtime still records
the synthetic Step/result for assembly, replay, and inspection. Clear the one-call
match when the exchange ends, or when the Run ends. A real
failure/cancellation overrides suppression. Unknown/mismatched results retain
ordinary diagnostics; never hide a real call merely because its text matches.
A later model-issued retry is a separate call and displays normally. No retry
means no workspace-operation row.

```text
✧ Reloaded workspace rules
• Executed write workspace://repo/hello.txt   # only if the model later retries
```

No new event field, begin flag, adjacency inference, or Store lookup is needed.
Recorded honor results provide the same correlation during progress replay.

### Compaction elapsed time

Show compact immediately at StepBegin, even before the first Model Step.
Refresh its own elapsed time once per second in TTY/Chat, including permit
waiting, until StepEnd or presenter close. Reuse timestamps, duration formatting,
Chat's ticker, and one Script refresh loop. Non-TTY prints start and end only.
No heartbeat, phase estimate, or compact-program events in the caller's progress.

Keep runtime instructions concise: apply recalled rules and continue directly;
do not narrate routine preflight recovery. Report real blockers and material
constraints. Preserve rule scope/revision semantics, tool/repair gating, and
model-authored text.

## Implementation and acceptance

1. Define the single preflight output contract in base. Update only honor's
   internal arguments/result and the blocked-result path in
   `execution/tools/runtime.py`, `executor/tool_runtime.py`, and
   `executor/steps/tool.py`. Preserve independent compact execution.
2. Supply runtime-owned summaries through the existing summary path; customize
   markers and honor suppression in `cli/common/execution_progress/`.
   Integrate compact refresh in the existing Script/Chat presenters.
3. Update the bundled preflight directions for a non-error notice. Test effective
   model requests: pick/reload results present; honor/compact exchanges absent;
   workspace notice present; recalls and horizon applied through existing controls.
4. Test one successful honor with durable refs before the synthetic reply, correct
   rule scopes/revisions, and no first-attempt side effect. Cover model retry,
   changed retry arguments, no retry, multiple workspaces, and parallel Runs.
5. Test complete suppression from StepBegin without a flash, unchanged later tool
   rendering, and visible recall/delivery failure or cancellation. Round-trip
   receipts through existing codecs and reproduce progress without resource reads.
   Verify normal runtime outputs, counts, run/execute boundaries, and fake-clock
   refresh/cleanup. Run ruff check/format, ty, and the full offline pytest suite.

Approval is required before implementation. The important boundary is that
presentation never determines model history or whether an operation executes.
