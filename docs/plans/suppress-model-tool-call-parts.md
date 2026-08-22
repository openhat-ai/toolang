# Suppress Model Tool-Call Parts in Execution Progress

## Work Type and Approval

Feature implementation. The human approved this behavior on 2026-08-22 by
confirming implementation in the existing tool-call presentation pull request.

## Goal and Success Criteria

Keep execution progress focused on user-visible model text and Tool Step
activity. Model output must not render `ToolCallPart` values. A successful Model
Step whose output contains only tool-call parts must disappear from live
presentation without being finalized, allowing the following Tool Step to use
the same live position.

## Scope and Design

- Exclude every `ToolCallPart` from Model Step output projection in Chat and
  Script, including root, nested, and parallel-lane presentation.
- Preserve the order and current rendering of all remaining model parts.
  Mixed text/tool-call output renders only its text and other displayable
  non-tool-call parts.
- When a successful Model Step has one or more output parts and every part is a
  `ToolCallPart`, emit no committed terminal block for that Step.
- Remove that Step's live block through normal live-snapshot reconciliation:
  the terminal `ProgressUpdate` has no committed block for the Step and omits
  its key from `live`. Chat and Script therefore discard the replaceable live
  presentation instead of calling `finalize_block` or writing it to
  scrollback.
- Let the following Tool Step create its normal live block in the vacated
  position immediately before the Run footer. Do not introduce a replacement
  row, placeholder, extra separator, synthetic cancellation event, or explicit
  presentation-cancel protocol.
- Keep an already committed visible fragment from the same Model Step. For
  example, streamed text remains in scrollback; a later `ToolCallPart` produces
  no additional row.
- Continue to show Model Step failures and cancellations even when their output
  contains only tool-call parts. Their terminal diagnostic remains useful.
- Preserve the current fallback for a successful Model Step with no output
  parts. Only non-empty, tool-call-only output triggers presentation
  suppression.
- Continue processing the real `StepEnd`: validate completed parts, clear
  projector state, record model-call metrics, tokens, and cost, and retain
  durable execution data. "Cancel" refers only to discarding replaceable
  presentation; it does not change execution status or records.
- Do not change tool invocation, Tool Step summaries/results, plugin protocol,
  event schemas, durable records, API/web presentation, or Run footer metrics.

## Touchpoints

- `src/toolang/cli/common/execution_progress/step_projection.py`
- `src/toolang/cli/common/execution_progress/projector.py`
- `tests/unit/cli/test_execution_progress_projector.py`
- `tests/unit/cli/test_chat_tui.py`
- `tests/unit/cli/test_script_run_presenter.py`
- `docs/execution-presentation.md`

## Acceptance Tests

1. Mixed Model output containing text and `ToolCallPart` values renders the
   text without a `requested <tool>` row or serialized tool-call payload.
2. Ending a successful, tool-call-only Model Step after its live `thinking`
   state yields no committed progress block, removes the live block, clears the
   Step from projector state, and still records its model-call metrics.
3. Chat does not finalize the suppressed Model Step. The next Tool Step appears
   in the vacated live position before the existing Run footer.
4. Script TTY clears the replaceable Model Step without appending it to
   scrollback; non-TTY emits no Model Step row.
5. A Model Step with previously committed visible text keeps that text and
   emits no later row for tool-call parts.
6. Failed and canceled Model Steps retain their terminal diagnostics.
7. A successful Model Step with zero output parts retains its existing empty
   output fallback.
8. Parallel-lane Model output does not expose tool-call parts and allows the
   following lane Tool Step to replace its activity in place.
9. Tool Step presentation and Run footer metrics remain unchanged.
10. The default repository verification passes.

## Risks and Open Questions

The design relies on full live-snapshot replacement already shared by Chat and
Script. A sink that incorrectly treats omission as finalization would leak the
temporary Model row, so lifecycle regression coverage is required. There are
no open product questions.
