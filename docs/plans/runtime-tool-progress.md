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
| Intercepted workspace call | Model | No | Yes: an error-only response from executor | None of its own |

All runtime calls are shown in this phase. Keep existing control adoption,
record/event schemas, run/execute behavior, and independent compact execution.
No visibility settings, general plugin API redesign, or fs wording changes.

## Runtime results

All four tools return `controls` containing summaries of the controls they have
durably created or reused. Each item includes the control `ref` and the necessary
diagnostic fields. Representative output values, keyed here by tool name:

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
      "target": {"kind": "rules", "workspace": "repo", "path": "/src/AGENTS.md"},
      "revision": "<content-hash>"
    }]
  }
}
```

Pick also supports `target.kind = "service"`. In an honor summary, `target.path`
is the exact workspace-relative rules file: `/AGENTS.md`, `/src/AGENTS.md`, etc.
It is neither a directory scope nor the requested operation's path. Existing
recall controls identify directory scopes; project each scope to its rules file
when building this result, without changing control identity or applicability.
Revision `0` means removal and still identifies the removed rules file.

Content remains in controls or compact output, not summaries. These are ordinary
results of the named tools; honor does not return another call's identity or its
retry error. Keep control ordering, reuse, and `controls: []` when none is needed.
Reported controls must exist before return; this does not imply ModelCall
adoption. Reload waits for application; recall/compact retain their adoption
boundary. Actual runtime tool failures remain errors.

Define result wire shapes in `toolang/base`; plugins must not import execution
records. Existing ToolResult/StepEnd delivers the summaries to UI without Store
or State queries. For example, `horizon` identifies the compact Run, allowing a
future progress change to display its ID without changing the result contract.

## Intercepted workspace response

The executor checks rules before creating the workspace Tool Step. When recall
is needed, it runs honor and waits for its outcome. After successful recall,
answer the original workspace call with this model-facing error content:

```json
{
  "error": "Operation not executed: workspace rules must be applied first. Follow the supplied rules and retry."
}
```

Use the existing ToolResultPart error field and preserve the original call's
name and IDs in the normal envelope. There is no output payload, special result
type, control summary, or reference to honor in this response. The recalled
rules already arrive through control-derived user messages.

Every model adapter must transmit the error text and omit an output payload for
this error-only response. Keep provider-required envelopes and error markers.
The internal Part may retain its existing empty-output default for storage;
no new Part field, discriminator, or Store schema change is needed.

The workspace operation has not executed and has no Tool Step or execution
events. Honor succeeds if recall succeeds; the original call's retry error does
not turn honor into a failed Step or fail the Run. Runtime never retries
automatically. The model may retry, change its request, or move on; a later
workspace call executes and displays normally.

If honor itself fails or is canceled, show that actual status and preserve
interruption behavior. The original call's error must reflect that failure, not
claim that rules are ready. It still has no output or workspace Tool Step.

```text
Model output: workspace ToolCall
  -> honor Tool Step -> recall controls -> honor result summaries -> StepEnd / UI
  -> executor answers original call with error only -> model message buffer
Next Model Call: original exchange + control-derived rules messages
  -> optional model-issued retry -> real workspace Tool Step
```

## Persistence and reconstruction

Record a reference to the original Model ToolCallPart in honor's existing
`Step.input` field. This is a data dependency, not a new control relationship or
plugin argument. Honor still takes normalized workspace access paths.

Build the original call's error response from that call and honor's outcome.
Append it to the online message buffer; the next ModelCall delta saves the
literal ToolResultPart using the existing format. If cancellation or a crash
prevents that delta, reconstruct the unrecorded history tail from the same honor
Step input and outcome, using the same response builder. Once the delta exists,
replay it unchanged and do not append the response again.

Preserve complete tool exchanges, batch ordering, and cancellation placement.
Raw Step inspection shows honor; conversation history includes the original
workspace error response. No synthetic Step, extra message log, resource reread,
or UI-side correlation is needed. Lost or duplicate responses are the main risk.

## Progress

Use ordinary tool lifecycle presentation for pick, reload, compact, and honor.
Tools supply wording through existing summaries; progress owns markers, layout,
and duration formatting. Result summaries provide terminal display data.

| Tool | Running | Succeeded |
| --- | --- | --- |
| pick | `Loading skill guidance: <ref>...` | `Loaded skill guidance: <ref>` |
| reload | `Reloading agent state...` | `Reloaded agent state` |
| compact | `Compacting thread history` | `Compacted thread history` |
| honor | `Reloading workspace rules: workspace://repo/src/AGENTS.md...` | `Reloaded workspace rules: workspace://repo/src/AGENTS.md` |

Honor must show both workspace and exact rules-file path while running and when
finished, including failure/cancellation. Preflight discovery supplies identified
rules-file paths for the existing begin summary; completion uses result summaries.
For multiple files, list every workspace/file pair in order under the same honor
Step, using additional detail lines as needed. Do not collapse them into a generic
label or a count. Removed files retain their paths; no UI resource reads are needed.

Pick uses service wording where appropriate. Keep actual failure/cancellation
wording. Use `✧` for these runtime rows, retaining ordinary tool `•` and footer
`∎`. Counts follow actual Steps. There is no workspace event to suppress, no
first-attempt activity flash, and no duplicate workspace error or result panel.

Show compact from StepBegin, including permit waiting. Refresh elapsed time once
per second in TTY/Chat until StepEnd or presenter close; reuse Step timestamps,
Chat's ticker, and one Script refresh loop. Non-TTY prints start/end only. A final
row can read `Compacted thread history in 1m20s`. Do not forward compact-program
events or add heartbeats. Keep bundled directions concise: apply recalled rules
and continue; report real blockers rather than narrating routine recovery.

## Implementation touchpoints and acceptance

- Base result schemas and `execution/executor/{tool_runtime,executor,compact}.py`:
  enrich all four results; test exact summaries, durability before return, reuse,
  empty results, and real errors. Honor summaries identify the actual rules file,
  including workspace-root files, nested files, and deletion revisions.
- `execution/executor/{rules.py,steps/tool.py}`: carry discovered rules-file paths
  into honor's begin summary, record the original ToolCall dependency, and replace
  the blocked-Step path with an error-only response. Test no workspace side
  effects/Steps/events; successful honor plus retry error; changed/no retry;
  multiple workspaces/files; discovery/read failures; and parallel Runs.
- Model adapters under `plugin/models/adapters/`: verify error text reaches each
  provider, with no output payload or recall metadata. Correct error-only encoding
  as needed; keep nonempty tool outputs and provider-required envelopes intact.
- `execution/assembly.py` and affected history readers: share response construction
  with the online buffer. Test cancellation/restart during honor and before/after
  delta persistence, mixed tool batches, complete exchanges, and no duplicates.
  Pick/reload remain model-visible; honor/compact remain internal; existing
  saved deltas and control effects are unchanged.
- Runtime wording, bundled directions, and Script/Chat progress: test StepEnd
  round-trips and replay without Store/State queries, all honor workspace/file
  identities at begin/end, normal later workspace calls, actual failures, and
  fake-clock compact timing/cleanup. No event suppression state is introduced.

Run ruff check/format, ty, and default offline pytest. This definition requires
human approval before implementation.
