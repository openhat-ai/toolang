# Runtime tool progress

Status: proposed feature definition; implementation requires approval.

## Goal and boundaries

Make runtime preparation observable through the toolset/runtime result protocol.
Tools supply wording and useful facts; progress owns markers and presentation.
Keep existing Steps, events, Store schema, controls, and ModelCall assembly.
This PR defines the feature; it does not implement it.

Current tool outputs already accept JSON objects, including structured
`ToolFailure.output`. Existing `given.summary` and `noted.summary` carry wording.
Use those channels; add no retry flag, event field, or separate progress log.

## Result protocol

Define the shared contract in `toolang/base`. Reserve qualified result types
`_toolang/honor`, `_toolang/pick`, `_toolang/reload`, `_toolang/compact`, and
`_toolang/preflight`. These are ordinary ToolResult output objects, distinguishable
from application data without parsing error text or correlating adjacent Steps:

```json
{
  "type": "_toolang/preflight",
  "summary": "Workspace rules reloaded; retry required",
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

All five results use `type`, `summary`, and `controls`. Each controls entry has a
serialized ControlRef in `ref`, plus the facts below. Runtime captures these facts
while performing the operation; progress needs no Store, State, or plugin reads.

| Result | Facts accompanying each control ref |
| --- | --- |
| honor / preflight | `workspace`, root-relative rules scope `path`, `revision` |
| pick | `kind` (skill/service), `resource` (exact catalog ref), `revision` |
| reload | `state` revision |
| compact | `thread`, `begin`, `end`, `output` (compact output reference) |

Only created or reused controls appear in receipts. Honor reports actual recalled
scopes, not the original file-access paths; revision `0` denotes removal. Resource
content stays in recall controls and is not copied into result metadata. Empty
controls are valid for no-ops; the tool supplies the corresponding summary.
Run/execute retain their existing result contracts and structural presentation.

### Preflight ordering

```text
path check -> honor -> persist/reuse recall controls -> honor result
           -> original call returns preflight result; operation not executed
           -> next model call receives tool result and recalled rules
           -> model applies rules and issues the appropriate next call
```

Runtime returns the tagged preflight output on the blocked original call only
after successful honor. Its recall controls must already be durable and available
for the existing next-call adoption path; receipt creation is not adoption.
Use the existing structured failure channel to keep
`ToolResult.error = "operation not executed; retry required"` alongside the
preflight output. Preserve call identity, failed Step status, and tool/user
message ordering. The original operation does not execute automatically.

Failure/cancellation is not a normal preflight result. Preserve real errors and
any receipts already produced without claiming successful recovery. The protocol
reports completed runtime work; receiving an object does not authorize new
access or trigger another recall. Neither error-text matching nor progress
presentation determines execution behavior.

## Tool-owned wording

Add an optional tool `summary(arguments, status)` hook through the existing
plugin/factory wrappers. Use it for active, failed, and canceled wording and as
the success fallback. Successful results and normal preflight use their returned
`summary`; actual failure/cancel takes precedence even when output exists.
Runtime stores these strings in the existing given/noted summaries. Tools without
the hook retain the generic fallback; projection never loads a plugin.
The hook only formats original call arguments, preserving logical workspace
paths and excluding secrets; it does not perform resource reads or operations.

| Tool | Active wording | Successful wording |
| --- | --- | --- |
| honor | `Reloading workspace rules...` | `Reloaded workspace rules` |
| pick | `Loading skill guidance: <ref>...` | `Loaded skill guidance: <ref>` |
| reload | `Reloading agent state...` | `Reloaded agent state` |
| compact | `Compacting thread history` | `Compacted thread history` |
| fs read | `Reading <path>...` | `Read <path>` |
| fs write | `Writing <path>...` | `Wrote <path>` |

Pick substitutes service where appropriate. Tools append relevant targets or
no-op wording themselves. Honor reloading includes changed and removed rules;
reload already waits for adoption. Picking service guidance does not connect it.
Provide concise wording in the runtime and fs toolsets, not an English-inflection
engine or a tool-name-to-sentence table in the executor or renderer.

Progress adds `✧` to runtime rows and retains `•` for ordinary tools and `∎`
for root footers. It owns tone, wrapping, layout, and elapsed-time decoration;
tool summaries contain no marker, ANSI styling, or elapsed clock.

## Progress and lifecycle

Show all runtime calls in Script and Chat. Keep run/execute's existing structural
presentation; do not add duplicate rows for their receipts. Add no visibility
option, filtering mode, CLI flag, or environment setting. Recognized preflight
is a visible neutral protocol outcome, not a red failure. Errors and cancellation
remain visible.

Before a result exists, classify the known runtime calls by exact tool name.
An ordinary call starts with its tool's active summary. If it returns preflight,
replace that live row with the neutral preflight summary rather than a failed
operation label.

Runtime rows are plain and unboxed, showing supplied summary and target/ref
details without dumping receipt JSON; inspection retains the complete output.
Unknown results use ordinary presentation. Preserve Step bookkeeping, counts,
error-reference ownership, and parallel lanes. A real terminal failure must not
lose its explanation.

Show compact immediately on StepBegin. In TTY/Chat, refresh its elapsed time once
per second using its own start time, including permit waiting; stop at StepEnd or
presenter close. Reuse timestamps, duration formatting, Chat's ticker, and one
Script refresh loop. Timer ticks create neither events nor scrollback. Non-TTY
prints one start and one terminal line. No heartbeat, phase estimate, or internal
compact-Run trace; terminal success/failure/cancel retains total duration.

Keep one shared runtime instruction, independent of authored instruct and fs
selection: apply recalled rules and continue directly without narrating routine
recovery; explain real blockers, material constraints, or details the user asks
about. Preserve scope/revision semantics and the effective-tools/output-repair
gate. Never filter model-authored text.

## Implementation and acceptance

1. Add the small result contract and optional summary hook in `toolang/base`;
   forward the hook through tool loading/function-tool wrappers. Plugins depend
   only on base, not execution records or progress.
2. Update `execution/tools/runtime.py`, `executor/tool_runtime.py`, and
   `executor/steps/tool.py` to return factual receipts and tagged preflight via
   the existing ToolResult path. Adjust compact/reload receipt producers only
   as needed. No new schema, Step kind, control kind, or retry mechanism.
3. Add runtime/fs wording and consolidate preflight instructions in
   `executor/prepare.py` and `executor/prompts/`.
4. Extend shared `cli/common/execution_progress/` projection and Script/Chat
   presenters for receipt rendering and compact refresh.
5. Test actual honor/retry, pick, reload, and compact results, including no-op,
   changed/removed rules, recall failure, and cancellation. Verify controls exist
   before receipts, metadata matches their targets, and the first blocked attempt
   has no side effect. Ordinary errors with identical retry text stay visible.
6. Round-trip outputs through existing records/events and replay progress without
   resource reads. Test that every runtime operation is visible, tool-owned wording,
   unchanged run/execute/counts, narrow/parallel output, and fake-clock cleanup.
   Verify result/recall-message ordering, protocol injection, and unfiltered model
   text. Run ruff check/format, ty, and the complete offline pytest suite.

Model compliance with quiet recovery remains instruction-dependent. Human
approval of this scope precedes implementation.
