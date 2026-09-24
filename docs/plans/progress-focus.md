# Define Progress Focus and Compact Steer Feedback

## Status and Goal

Approved on 2026-09-24 for implementation in the same pull request.
Make active tools as visible as Thinking, remove Thinking's trailing dots,
remove automatically appended progress dots from tool summaries, and shorten
pending steer feedback.
Success means active work uses normal intensity, finished tool summaries and
pending feedback are secondary, and steer counts remain accurate.

## Verified Current Behavior

Verified against `1b72a0e4`:

- `step_projection.live_row` gives model activity the `active` tone but tool
  activity the `progress` tone. Rich maps these to normal and dim respectively.
- Terminal tool summaries also use `progress`. Tool summaries stay on one line
  and truncate at the available display width, including in parallel lanes.
- `SteerFeedbackBlock` uses normal text and distinguishes an active step from
  the interval before the next model call with long waiting sentences.
- Sending counts are separate from accepted unresolved counts; the presenter
  already owns receipt matching, consumption, and feedback removal.

## Presentation Decisions

### Active Tools

Use the existing `active` tone for live tool summaries, including ordinary `›`
and runtime `✧` markers. Render the entire summary at normal intensity, with
no added bold or color, just like `• Thinking`:

```text
• Thinking
› Running “cmd .line”
› Reading repo:/src/main.py
```

Change the model activity fallback from `• Thinking...` to `• Thinking` in
the shared projection for Chat and Script. Preserve model preview content
and genuine truncation ellipses; this is a literal status-label change.

Built-in tool summaries and the default executor summary template must not
append `...` while running. Change summary generation, not rendered strings,
so authored command/argument punctuation and truncation ellipses remain intact.
Custom plugin summaries remain plugin-owned; no blanket suffix stripping.
Cancellation prefixes the running summary without stripping target punctuation.

This includes the command or target, rather than trying to split arbitrary
plugin summaries into verbs and arguments. Preserve action wording, markers,
one-line truncation, and parallel lane identity styles. Finished tool summaries
remain dim; existing error diagnostics retain their error styling. Apply this
through the shared projection used by Chat and Script, not a Chat-only override.
Do not infer activity solely from the renderer's `live` flag: completed rows
can coexist with active rows in a live region.

### Pending Steers

Render the aggregate feedback row, including its `•` marker, dim. Use these
exact forms with normal singular/plural grammar:

| State | Example |
| --- | --- |
| One unresolved submission | `• 1 steer pending` |
| Several unresolved submissions | `• 3 steers pending` |
| Two requests awaiting receipts | `• 2 steers pending` |
| Three accepted pending and one awaiting receipt | `• 4 steers pending` |

Use the same pending wording during a step and between steps. The displayed
count is the sum of the existing accepted-pending and awaiting-receipt counts.
Pending describes locally submitted unresolved input, not confirmed server
acceptance or application immediately after a step. Keep receipt tracking
internal; never display Sending or a sending suffix. A pending receipt becoming
an accepted pending control must not change the total. Preserve existing error
feedback and count removal on rejection. Remove the row when no unresolved
submissions remain, without a success message.

Preserve wrapping with continuation text aligned after `• `, vertical spacing,
viewport protection, individual steer bars, and terminal `not applied` labels.
Do not truncate authored steer input or change control timing and consumption.
This supersedes only the aggregate feedback wording and intensity in the older
[steer feedback plan](chat-steer-feedback-and-run-context.md).

## Scope and Likely Files

- `src/toolang/base/utils/tool_descriptions.py` and
  `src/toolang/execution/executor/steps/tool.py`: stop appending progress dots
  in built-in and default running summaries.
- `tests/unit/plugin/test_tool_descriptions.py`,
  `tests/unit/execution/test_tool_step_summary.py`,
  `tests/integration/execution/test_event_scenarios.py`, and
  `tests/integration/execution/test_honor_rules.py`: exact running summaries,
  persisted wording, and preservation of authored punctuation.

- `src/toolang/cli/common/execution_progress/step_projection.py`: active tool
  tone and the Thinking fallback label; preserve terminal projection.
- `projector.py` and `state.py` in the same package: distinguish current lane
  activity from its last finished summary, retaining normal active tool styling
  after a handoff and dim terminal tool styling within the live region.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: shorter feedback and dim
  styling. Keep presenter state ownership unchanged.
- `tests/unit/cli/test_execution_progress_projector.py` and
  `tests/unit/cli/test_tool_progress_rendering.py`: projected tones and rendered
  intensity for active/terminal tools, runtime tools, and parallel lanes.
- `tests/unit/cli/test_runtime_tool_progress.py`,
  `tests/unit/cli/test_agic_run_progress.py`, and
  `tests/unit/cli/test_script_run_presenter.py`: runtime tool tone and the shared
  Thinking label.
- `tests/unit/cli/test_chat_tui.py` and
  `tests/system/cli/test_chat_tui_e2e.py`: exact feedback strings, styles, and
  existing receipt/consumption lifecycle scenarios.
- `docs/execution-presentation.md`: document the revised presentation.

Out of scope: new animations, marker changes, plugin summary parsing, scheduling,
runtime execution/API changes, queue layout, and new steering actions.

## Acceptance Checks

1. Active ordinary/runtime tool markers and full summaries have no dim, bold,
   or added color, matching Thinking intensity in Chat and Script.
   Model activity without preview text reads exactly `• Thinking`; model
   preview text and width-driven truncation ellipses remain unchanged. Built-in
   and fallback running tool summaries have no automatically appended dots;
   literal dots in commands and target paths remain unchanged.
2. Completed summaries remain dim even within a live region. Error diagnostics
   and parallel lane identity styling retain their current behavior.
3. Narrow and wide terminals keep tool summaries on one line; long commands
   and wide characters truncate safely with the existing ellipsis.
4. Awaiting-receipt-only, accepted-pending-only, and mixed counts all use
   `N steer(s) pending` with correct grammar and the combined count. Receipt
   acceptance alone does not change the total; rejection removes its count and
   retains existing error feedback. No Sending text or sending suffix appears.
   Wording is identical with and without an active step. The complete feedback
   row is dim in both Rich and Prompt Toolkit rendering.
5. Existing partial-consumption, out-of-order receipt, run-end, and short-height
   scenarios still pass. Feedback disappears after resolution without adding
   a permanent pending/applied message or changing individual steer bars.
6. Before implementation commits, run `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.
   Keep provider calls opt-in. Definition-only validation uses source inspection,
   link validation, and `git diff --check`.

## Tradeoffs and Open Questions

- Full live summaries give long commands more visual weight, but retain useful
  context and avoid fragile verb parsing. Existing truncation bounds their width.
- Compact pending wording omits application timing; it accurately describes
  state while execution remains the visual focus. Actual timing is unchanged.
- No implementation questions remain. The presentation choices are approved.
