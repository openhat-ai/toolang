# Runtime tool results and progress

Status: feature definition; no product changes in this PR.

## Contract and boundaries

Give progress the facts it needs through ordinary runtime ToolResults, while
preserving which calls and responses belong in model messages. Currently these
tools return control refs only, and honor interception creates a failed workspace
Tool Step. Replace those behaviors with the following contract:

| Call | Initiator | Own Tool Step and progress | Own ToolResult in model messages | Effect of its controls |
| --- | --- | --- | --- | --- |
| pick | Model | Yes | Yes | Recall skill/service guidance |
| reload | Model | Yes | Yes | Adopt State |
| compact | Runtime, before a Model Call | Yes | No | Update horizon |
| honor | Runtime, before a workspace operation | Yes | No | Recall workspace rules |
| Intercepted workspace call | Model | No | Yes: executor supplies its response | None of its own |

All runtime calls are shown in this phase. Keep existing control adoption,
record/event schemas, run/execute behavior, and independent compact execution.
No visibility settings, general plugin API redesign, or fs wording changes.

## Runtime results

All four tools return `controls` containing summaries of the controls they have
durably created or reused. Each item includes the control `ref` and the necessary
payload fields. Representative output values, keyed here by tool name:

```json
{
  "pick": {
    "controls": [{
      "ref": "<recall-control-ref>",
      "target": {"kind": "skill", "ref": "<skill-ref>"},
      "revision": "<content-hash>"
    }]
  },
  "reload": {
    "controls": [{"ref": "<reload-control-ref>", "state": "<state-revision>"}]
  },
  "compact": {
    "controls": [{"ref": "<compact-control-ref>", "horizon": "<compact-run-output-ref>"}]
  },
  "honor": {
    "controls": [{
      "ref": "<recall-control-ref>",
      "target": {"kind": "rules", "workspace": "repo", "path": "/src"},
      "revision": "<content-hash>"
    }]
  }
}
```

Pick also supports `target.kind = "service"`. Reuse the existing recall target
shape. Honor reports actual rules scopes, not attempted access paths; revision
`0` means removal. Content remains in controls or compact output, not summaries.

These are ordinary results of the named tools. Honor does not return a workspace
preflight response, another call's identity, or a retry instruction.

Keep control ordering, reuse, and `controls: []` when no control is needed.
Reported controls must exist before return; this does not imply ModelCall
adoption. Reload still waits for application, while recall/compact keep their
existing adoption boundary. Real failures remain errors.

Define the wire shapes in `toolang/base`; plugins must not import execution
records. Existing ToolResult/StepEnd delivers the summaries to UI without Store
or State queries. For example, `horizon` identifies the compact Run, allowing a
future progress change to display its ID without changing the result contract.

## Workspace preflight response

The executor checks rules before creating the workspace Tool Step. When recall
is needed, it runs honor, waits for its result, then answers the original
workspace call without executing the operation or creating its Step/events.

Only this executor-generated workspace response uses the special preflight
protocol. Its output contains the notice and honor's control summaries; the
original call's identity belongs in the normal ToolResultPart envelope:

```json
{
  "type": "tool_result",
  "tool_call_id": "<original-workspace-call-id>",
  "call_id": "<original-provider-call-id>",
  "tool_name": "<workspace-tool-name>",
  "tool_family": "<workspace-tool-name>",
  "output": {
    "type": "_toolang/preflight",
    "message": "Workspace rules reloaded; retry required",
    "controls": [{
      "ref": "<recall-control-ref>",
      "target": {"kind": "rules", "workspace": "repo", "path": "/src"},
      "revision": "<content-hash>"
    }]
  }
}
```

Set `error=None` (omitted by serialization). Recognize the qualified protocol
type, not message text. This is a non-error response, not a successful workspace
operation. Runtime never retries automatically. The model may retry, change its
request, or move on; a later call executes and displays normally.

Honor failure/cancellation stays visible on honor. Answer the original call
with the corresponding failure/interruption, never a successful preflight notice;
the intercepted workspace operation still has no Tool Step or execution events.

```text
Model output: workspace ToolCall
  -> honor Tool Step -> recall controls -> honor ToolResult -> StepEnd / UI
  -> executor builds workspace ToolResult -> model message buffer
Next Model Call: original exchange + recalled rules
  -> optional model-issued retry -> real workspace Tool Step
```

## Persistence and reconstruction

Keep correlation inside execution: record a reference to the original Model
ToolCallPart in honor's existing `Step.input` field. This is a data dependency, not
a new control relationship or plugin argument. Honor still takes normalized paths.

The executor builds the workspace response from that original call and honor's
outcome. Append it directly to the online message buffer; the next ModelCall delta
saves the resulting literal ToolResultPart using the existing format.

If cancellation or a crash prevents that delta from being recorded, reconstruct
the unrecorded history tail from the same honor Step input and outcome. Share the
response builder between live execution and history recovery. Once the delta
exists, replay it unchanged and do not append the response again.

Preserve complete tool exchanges, batch ordering, and cancellation placement.
Raw Step inspection shows honor, while conversation history includes the original
workspace response. No synthetic Step, extra message log, resource reread, or
UI-side correlation is needed. Lost or duplicate responses are the main risk.

## Progress

Use ordinary tool lifecycle presentation for pick, reload, compact, and honor.
Tools supply wording through existing summaries; progress owns markers, layout,
and duration formatting. The result summaries provide terminal display data.

| Tool | Running | Succeeded |
| --- | --- | --- |
| pick | `Loading skill guidance: <ref>...` | `Loaded skill guidance: <ref>` |
| reload | `Reloading agent state...` | `Reloaded agent state` |
| compact | `Compacting thread history` | `Compacted thread history` |
| honor | `Reloading workspace rules...` | `Reloaded workspace rules` |

Pick uses service wording where appropriate. Keep actual failure/cancellation
wording. Use `✧` for these runtime rows, retaining ordinary tool `•` and footer
`∎`. Counts follow actual Steps. There is no workspace event to suppress, no
first-attempt activity flash, and no duplicate workspace notice or result panel.

Show compact from StepBegin, including permit waiting. Refresh elapsed time once
per second in TTY/Chat until StepEnd or presenter close; reuse Step timestamps,
Chat's ticker, and one Script refresh loop. Non-TTY prints start/end only. A final
row can read `Compacted thread history in 1m20s`. Do not forward compact-program
events or add heartbeats. Keep bundled directions concise: honor the recalled
rules and continue; report real blockers rather than narrating routine recovery.

## Implementation touchpoints and acceptance

- Base result schemas and `execution/executor/{tool_runtime,executor,compact}.py`:
  enrich all four results; test exact summaries, durability before return, reused
  controls, empty results, and real errors. Keep the runtime toolset base-only.
- `execution/executor/steps/tool.py`: preserve the original Model ToolCall, record
  honor's input dependency, and replace the blocked-Step path with a protocol
  response. Test zero workspace side effects/Steps/events; one honor Step; retry,
  changed arguments, no retry, multiple scopes/workspaces, and parallel Runs.
- `execution/assembly.py` and affected history readers: share response construction
  with the online buffer. Test cancellation/restart during honor and before/after
  delta persistence, mixed tool batches, complete exchanges, and no duplicates.
  Existing saved deltas remain unchanged. Pick/reload remain model-visible;
  honor/compact remain internal; control effects are unchanged.
- Runtime wording, bundled preflight directions, and Script/Chat progress under
  `cli/common/execution_progress/`: test StepEnd round-trips, progress replay
  without Store/State queries, ordinary later workspace calls, visible failures,
  and fake-clock compact timing/cleanup. No event suppression state is introduced.

Run ruff check/format, ty, and the default offline pytest suite. This definition
requires human approval before implementation; no further product choices are
required for its scoped behavior.
