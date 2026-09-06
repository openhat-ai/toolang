# Define Chat Steer Feedback and Run Context

## Work Type and Status

Feature definition. Approved through the 2026-09-06 design discussion;
no implementation included.
The 2026-09-06 discussion specifies independent message-only steer bars, one
ordinary marked feedback row, root context in the bottom-right padding, and
one permanent padding row above and below every control's body. Steer accents
identify the interaction, not application state. Only steers known to remain
unapplied when the Run ends receive a bottom-right `not applied` label.
Status text remains flush with both terminal edges. Control text keeps its
two-cell left offset and two-cell right inset; bottom-right information uses
the same right inset within its own bar.

## Goal and Success Criteria

Make waiting steers visibly acknowledged, including several submitted during
one long Step. Preserve the runnable and model selection for each historical
root input after session defaults change. Every control bar preserves its
vertical padding regardless of the number of body lines.

## Verified Current Behavior

Verified against `origin/main` at `7daa7d73`:

- `RunControlBlock` stores only input text. Its `QueuedCall` already snapshots
  the resolved runnable, model request, and per-input overrides.
- Status shows the active root runnable and mutable session model defaults.
  It is not committed to scrollback.
- `_control_bar_lines` uses `max(0, 3 - body_rows)` padding. Two body rows
  consume the bottom padding, and three or more consume both padding rows.
  Root, steer, and quick-command bars share this behavior; the editable Input
  already has separate top and bottom padding rows and a right inset.
- Both Chat clients send steer controls with `timing="next_step"`; the active
  Step continues. A subsequent model call consumes them. Chat discards the
  returned `ControlInfo` and finalizes matching-run steer bars on any next
  `StepBegin`, without checking actual consumption.
- `StepBegin.preceded_by` identifies consumed controls. The executor marks
  those controls applied and marks remaining pending controls `wontapply` at
  Run end. Recovered `RunDetail` includes durable controls and Steps.

## Presentation Decisions

### Permanent Control Padding

Apply to root inputs, steer inputs, and every quick-command control variant:

```text
one top padding row
all wrapped authored body rows
one bottom padding row
```

The control height is always `wrapped_body_rows + 2`, with at least one body
row. Two body rows therefore occupy four rows; three occupy five. Never reuse
padding for authored text, including text that wraps after a terminal resize.
Semantic metadata may occupy a padding row without turning it into body
space. Root controls use bottom padding for context; terminal unapplied steers
use it for a status label. All top padding and quick-command bottom padding
remain empty, as does bottom padding for pending or applied steers.

Preserve accents, adaptive Input background, authored blank lines, normal body
foreground, and the existing outside gaps. Body text begins two cells from
the bar's left edge (accent plus one space) and leaves two cells on the right.
Wrap using this inner width without consuming either inset. Root bars remain
full width; steer/quick-command bars use the existing output-width limit.
Editable Input uses the same two-cell left offset and right inset, retaining
its existing separate top/bottom padding. At extreme widths, reduce horizontal
insets only as necessary to retain a content cell.

### Root Context in Bottom Padding

Render the following dim string, right-aligned in the bottom padding row:

```text
agic:research · openai/gpt-5 · high
```

Keep two background-painted cells between its final character and the right
edge of the bar. This shares the body text's right boundary. The body remains
left-aligned; metadata never shares its last line or appears above it. Do not
add field labels or a Run ID.

- Snapshot from the submitted `RunRequest`, including queued settings and
  colon overrides. Later defaults never rewrite historical context.
- Use the qualified runnable and canonical model reference. Show explicit
  reasoning effort or token budget; absent reasoning adds no suffix.
- An absent model request renders `model unspecified`. The line describes
  the submitted selection, not all actual models used by a Flow or its children.
- Before committing, replace the provisional runnable with its own root
  `RunBegin.runnable`. Keep the model selection fixed. Rejection preserves
  attempted context with the existing diagnostic; it implies no execution.
- Keep metadata within its single padding row. Elide the runnable name first,
  then the model reference, preserving the rightmost reasoning suffix while
  feasible. Use display-cell widths and an ellipsis, never broken separators
  or wide characters. At extreme widths fit the remaining string safely.
  Retain the full snapshot in memory; already written terminal scrollback
  retains the fitted label and cannot expand after a resize.

### Status Alignment

Keep status content flush with the terminal's left and right edges in idle,
running, and error states. The left text starts at column zero; the rightmost
model ends at the final terminal column. Control bars retain their two-cell
text insets independently of status alignment.

Keep the status surface full width and unpainted. Fit the existing single-line
composition within the full terminal width, retaining existing reduction
priorities and active/default meanings. Do not indent execution markers or
move control accents to achieve text alignment.

### Steer Bars and Shared Feedback

Keep a separate bar for every locally accepted steer, in submission order.
Use the same existing purple accent in every state, including all padding
rows. Do not change color or intensity on application. Keep the authored body
unchanged and show neither steer numbers nor runnable/model labels.

The bottom-right padding contains a dim `not applied` label only when the Run
has ended and the steer is known not to have been consumed. End the label two
cells before its own bar's right edge, keeping it on the single padding row.
Do not show a marker, reason, or separate diagnostic for this normal terminal
case. The Run's own footer already communicates that execution ended.

| Steer condition | Bottom-right padding |
| --- | --- |
| Pending while its Run is active | Empty |
| Applied, during or after its Run | Empty |
| Confirmed unapplied after its Run ends | `not applied` |

This is an exception annotation, not a full state display. Empty padding does
not claim successful application. Uncertain transport outcomes must not be
relabeled as known non-application.

Place one ordinary execution feedback row beneath the latest unresolved
steer bar, on the terminal background and within the output width. It uses the
existing `•` marker at column zero, one following space, and normal text.
This is part of live output, not a new panel, footer, or focus target. Keep
active execution output above the pending steer bars and their feedback row;
new steers insert immediately before the row. Queue remains joined to Input.

Use these exact single/plural forms for accepted pending controls:

```text
• 1 steer will apply after the current step
• 3 steers will apply after the current step
```

The count covers all accepted unresolved steers targeting the active root,
including bars outside the live viewport. If no target Step is active, use
`• N steer(s) waiting for the next model call`. There is no timer, spinner,
progress percentage, or extra pending-count suffix in the status bar.

Before any receipt is known, the shared row reads `• Sending N steer(s)`.
Mixed accepted/sending requests append `· sending M more` to the waiting
sentence. Use normal singular/plural grammar, never literal `(s)`. When there
is no active Step, use the next-model-call wording. These are aggregate live
messages only; they never add pending labels to individual bars.

Actual request rejection or transport failure retains the existing error
feedback, associated with its source message. Do not add a new per-steer
unconfirmed/revoked state display or duplicate errors in bottom padding.

When a subset is consumed, commit those bars without a state label or color
change before the consuming Step's output if their identities are already known.
Leave other bars live and update only the remaining pending count. When all
are consumed, remove the waiting row entirely, without a replacement success
message or extra blank row. At Run end, remove the waiting row and finalize
remaining bars, adding `not applied` only where non-consumption is established.
Do not infer application from an arbitrary next Step or merely elapsed time.

Ordinary feedback wraps with continuation text aligned after `• `. At short
terminal heights, reserve Input, status, Queue's required rows, and the live
feedback row before clipping optional live output. Preserve every full input
when finalizing, even if older live bars were outside the viewport.

## State Ownership and Ordering

Keep feedback in Chat. Give each submission an internal immutable key and
target run, and correlate receipt/error callbacks by that key. Forward the
existing `ControlInfo` from both adapters through a typed UI event; match
accepted controls by `(run_id, index)`. Mutate UI state only on its event loop.
Do not change execution or HTTP schemas, timing, or submission ordering.

Replace generic-next-Step finalization with identity-based transitions. Cache
consuming refs while receipts are outstanding: consumption can arrive first.
In that case finalize the live bar without a label when correlation becomes
possible, rather than rewriting earlier output. Identical messages remain
separate submissions; out-of-order receipts neither deduplicate nor reorder them.

At Run end, determine non-consumption from accepted controls unmatched in a
complete ordered stream, or from recovered durable controls/Steps. A durable
`wontapply` or revoked control is known unapplied; both use the same short
terminal label. If event coverage is incomplete, use the existing recovery
diagnostic and leave unestablished per-bar status blank.

Then finalize bars and clear the current Run's steering state. Late callbacks
for ended Runs cannot affect the next Run or append retrospective status.
Do not track controls across completed Runs, rewrite native scrollback, retry
silently, add polling, or create new transcript persistence. Duplicate evidence
must not duplicate bars or diagnostics.

## Scope and Implementation Touchpoints

- `src/toolang/cli/toolang/commands/chat/blocks.py`: permanent padding, root
  context fitting, terminal steer labels, and adjacent live feedback.
- `base.py` and `events.py` in the same package: receipt callbacks and typed,
  correlated UI messages.
- `local.py` and `remote.py`: preserve control receipts and distinguish known
  rejection from uncertain delivery.
- `presenter.py`: control matching, feedback lifetime, recovery, and ordering.
- `tui.py` and `widgets.py`: request snapshots, live-area sizing, and Input's
  two-cell right inset. Preserve Input/Queue focus and the existing edge-aligned
  active/default status.
- `tests/unit/cli/test_chat_tui.py`, `test_chat_remote.py`, existing local and
  remote Chat integration tests, and `tests/system/cli/test_chat_tui_e2e.py`:
  deterministic fake-client and PTY coverage.
- `docs/chat.md` and `docs/execution-presentation.md`: approved behavior.

Out of scope: immediate interruption; new steering policy; edit/revoke/retry
actions; Queue redesign; shortcuts; actual per-Step/child model metadata;
full parameter dumps; persistence; Script or scripted Chat; API/database changes.

This supersedes the prior shrinking-padding and message-only root-bar rules,
and generic-next-Step steer finalization. It preserves root/auxiliary widths,
terminal colors, status meaning, and edge-aligned status text.

## Acceptance Tests

1. Every control variant has exactly one top and one bottom padding row with
   one, two, three, and many wrapped body lines; authored text occupies neither
   padding row. Resizing never removes padding or loses body content. Body
   rows preserve their two-cell left offset and two-cell right inset.
2. Root metadata occupies only the bottom padding row, ends two cells before
   the right edge, is dim, and has no field labels. Long refs/CJK fit safely
   without adding padding rows. Quick-command padding and pending/applied steer
   padding stay empty. A terminal unapplied steer places only `not applied` in
   its existing bottom padding, with the same two-cell right inset.
3. Submit with runnable/model A, queue with B, then change defaults to C.
   Root bars retain A and B, including input overrides and explicit reasoning.
   Authoritative runnable updates only its own bar; missing-model cases are
   labeled without claiming actual model execution.
4. A blocked fake Step allows one then three steers. Separate message-only
   bars retain both padding rows. One `•` feedback row shows the correct count
   and adoption timing, with no numbered bar labels, timers, or focus changes.
5. Out-of-order receipts, identical texts, child/unrelated Steps, and partial
   consumption update only matched controls. Consuming two of three commits
   their unchanged bars, leaving a single-steer waiting row.
   Consuming the final steer removes that row without any applied/success text.
   Accent colors and intensity remain identical across all steer states.
6. Consumption before receipt, replay, late callbacks, cancellation, rejection,
   uncertain delivery, and recovered applied/wontapply/revoked states converge
   without false application, duplicate transcript, silent resend, or leakage
   into the next queued Run. Each failure remains associated with its message.
   Only confirmed unapplied steers receive the terminal corner label; no
   standalone `Steer not applied` line or retrospective status is emitted.
7. Queue-to-steer retains literal text, dequeue, and history semantics. Rich
   and Prompt Toolkit agree on padding, widths, context, markers, and wrapping.
   Short-terminal PTY coverage retains the feedback row, Input, and Queue focus.
8. Idle, running, and error status retain existing edge alignment. The left
   starts in column zero and the rightmost model ends at the terminal edge;
   control text and corner labels retain their separate two-cell insets.
   Narrow status layouts remain one line within the full available width.
9. Default verification passes before every implementation commit:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
   and `uv run pytest`. Live-provider tests remain opt-in.

## Risks and Open Questions

- Acceptance is not consumption. Lost responses and incomplete streams require
  existing transport/recovery error feedback, not an unsupported `not applied`
  label or a successful-application claim.
- Permanent padding increases height for multiline controls. Live clipping
  must protect the feedback row without dropping finalized transcript text.
- A single context row may shorten long names on narrow terminals. Preserve
  the complete snapshot internally and keep body text independent of fitting.
- No open questions remain.
