# Runtime tool progress

Status: proposed feature definition; implementation requires approval.

## Goal and ownership

Make runtime work visible through ordinary Tool Steps and self-contained results.
Unify control summaries, not call ownership or ModelCall assembly semantics.

| Call | Initiator | ModelCall assembly | Progress |
| --- | --- | --- | --- |
| pick / reload | Model | Normal ToolCall and ToolResult; controls keep their existing effects | Normal tool presentation, customized wording/marker |
| compact | Runtime, before a Model Call | No compact tool exchange; compact control updates horizon | Show compact and elapsed time |
| honor | Runtime, before a workspace operation | No honor tool exchange; recall controls supply rules | Show honor |
| Blocked workspace call | Model; runtime supplies its response | Preserve the original ToolCall and supply a non-error retry notice | No Tool Step or execution events |
| Later workspace retry | Model, if it chooses to retry | Normal ToolCall and ToolResult | Normal tool presentation |

Show all runtime calls in this phase. Keep run/execute behavior, existing record
and event schemas, and independent compact execution. No visibility settings,
metrics redesign, general plugin summary API, or fs wording sweep. This PR changes
only the definition, not product code.

## Control summaries in results

For pick, honor, reload, and compact, change `controls` from reference strings to
summary objects. Each summary contains its control `ref` and these payload facts:

| Tool | Additional fields |
| --- | --- |
| pick | `target: {kind: "skill" or "service", ref}`, `revision` |
| honor | `target: {kind: "rules", workspace, path}`, `revision` |
| reload | `state` (State revision) |
| compact | `horizon` (compact Run output reference) |

Reuse the existing recall target shape. References remain pointer strings; the
wire contract belongs in `toolang/base`, with no execution-record dependency in
the toolset plugin. Do not copy guidance, rules content, or compact summary text
into these results.

Return only after the reported controls have been durably created or reused.
Preserve deduplication and ordering; `controls: []` means no control was needed.
A receipt does not imply adoption by a Model Call: recall/compact retain their
existing adoption boundary, while reload still waits for its control to apply.
Failures retain their real errors, not a successful receipt.

The existing ToolResult and StepEnd carry these facts to progress. UI must not
query Store or State to obtain them. For example, a future compact Run ID display
can use `horizon`; elapsed time still comes from Step timestamps. That additional
display is not required in this implementation.

## Honor preflight

Check workspace rules before creating the requested Tool Step. If recall is
needed, execute honor as a runtime Tool Step, then answer the original call
without executing the workspace operation. Do not create a workspace Tool Step or
emit its execution/part events. The original Model Step's ToolCall stays intact.

Honor receives the blocked `tool_call_id` alongside its normalized paths. Its
successful, durable output is this special toolset/runtime protocol result:

```json
{
  "type": "_toolang/preflight",
  "tool_call_id": "<blocked-workspace-call-id>",
  "message": "Workspace rules reloaded; retry required",
  "controls": [
    {
      "ref": "<recall-control-ref>",
      "target": {"kind": "rules", "workspace": "repo", "path": "/src"},
      "revision": "<content-hash>"
    }
  ]
}
```

Runtime uses this output for the original workspace ToolResult, preserving its
tool name, tool_call_id, and provider call_id. Set `error=None`: preflight is not
an operation failure. Recognize the qualified protocol type, not message text.
Workspace/path identify actual recalled rule scopes, not the attempted access
path; revision `0` denotes removal. The operation has not executed. Runtime does
not retry it; the model may retry, change the request, or move on.

```text
Execution                              Model messages / binding
Model Step requests workspace call     assistant: original workspace ToolCall
honor -> durable recall controls       (no honor tool exchange)
runtime answers original call          tool: preflight notice + control summaries
next Model Call adopts recalls         user: recalled rules
model may request another call         assistant: workspace ToolCall
workspace Tool Step executes           tool: actual result
```

Honor failure/cancellation remains visible on the honor Step. Use the same
no-workspace-Step path for the original call's failed/interrupted response,
preserving interruption behavior; never manufacture a successful reload notice.

### Persistence without a workspace Step

Append the original call's response directly to the online message buffer. The
next ModelCall delta records it as a literal ToolResultPart using the existing
delta format; honor's own tool exchange remains excluded.

That next Model Call may never happen. The persisted honor invocation identifies
the original Model ToolCall, and its output/outcome supplies the response facts.
Use one response builder for live execution and unrecorded history-tail recovery.
History must retain a complete exchange after cancellation or restart, without
re-reading resource files, adding a message log, or rerunning honor.

Once a delta contains the response, replay uses that delta unchanged; tail
reconstruction must not append it again. Keep tool-batch ordering and grouping,
including other calls alongside the intercepted call. Raw Step inspection shows
honor only; reconstructed conversation history includes the workspace response.

## Presentation

Use existing tool lifecycle rendering in Script and Chat. Tools own wording,
supplied through existing given/noted summaries; progress owns markers, tones,
wrapping, and elapsed-time decoration. Keep wording customization local to
runtime tools and the existing executor summary path, without extending every
plugin/factory. Result summaries provide terminal presentation data directly.

| Tool | Active wording | Successful wording |
| --- | --- | --- |
| honor | `Reloading workspace rules...` | `Reloaded workspace rules` |
| pick | `Loading skill guidance: <ref>...` | `Loaded skill guidance: <ref>` |
| reload | `Reloading agent state...` | `Reloaded agent state` |
| compact | `Compacting thread history` | `Compacted thread history` |

Pick uses service guidance where appropriate. Failed/canceled operations retain
their actual reasons and status wording. Use `✧` for these runtime rows, keeping
ordinary tool `•`, root footer `∎`, and existing run/execute boundaries.

There is no progress suppression registry: the blocked workspace Step and its
events do not exist. Show honor once, with no workspace activity flash, duplicate
notice, result panel, or layout gap. Only a later model-issued workspace call
produces a normal workspace row. Counts follow actual Steps.

Show compact immediately at StepBegin, including before the first Model Step.
Refresh its elapsed time once per second in TTY/Chat, including permit waiting,
until StepEnd or presenter close. Reuse timestamps, duration formatting, Chat's
ticker, and one Script refresh loop. Non-TTY prints start and end only. No
heartbeat, phase estimate, or compact-program events in the caller's progress.

Keep runtime instructions concise: apply recalled rules and continue directly;
do not narrate routine preflight recovery. Report real blockers and material
constraints. Preserve rule scope/revision semantics and model-authored text.

## Implementation and acceptance

1. Define the result wire shapes in base; enrich all four operations in
   `execution/executor/tool_runtime.py`, `executor.py`, and `compact.py`.
   Update honor's arguments in `execution/tools/runtime.py` and the base protocol.
   Test durable refs, exact payload summaries, reused controls, and empty results.
2. Replace the blocked-Step path in `executor/steps/tool.py` with direct response
   generation. Share it with `execution/assembly.py` tail recovery and check the
   affected history readers. Use existing `executor/_messages.py` and
   `message_delta.py` support; no new persistence or event fields.
3. Test zero workspace side effects, Steps, or execution events on interception;
   one honor Step; and a complete original tool exchange with `error=None`.
   Cover retry, changed arguments, no retry, multiple rule scopes/workspaces,
   mixed tool batches, parallel Runs, and actual failure/cancellation.
4. Test restart/cancellation before the next delta, after its persistence, and
   during honor. Live and reconstructed responses must agree, without duplicates
   or incomplete tool exchanges. Verify pick/reload results remain model-visible,
   honor/compact exchanges remain internal, and controls still supply rules,
   guidance, State, and horizon through their existing adoption paths.
5. Customize summaries and markers in the existing runtime/progress paths under
   `cli/common/execution_progress/`; integrate compact refresh in Script/Chat.
   Round-trip enriched results through existing codecs and StepEnd. Test progress
   replay without Store/State reads, normal later workspace rendering, real
   failures, run/execute boundaries, and fake-clock refresh/cleanup. Update the
   bundled preflight directions. Run ruff check/format, ty, and offline pytest.

The main risk is losing or duplicating a model-required response after removing
its synthetic Step. Honor records and shared tail reconstruction close that gap;
presentation must never determine model history or whether an operation executes.
No open design questions remain; implementation requires human approval.
