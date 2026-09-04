# Define Chat TUI Root Run Boundaries and Status

## Work Type and Status

Feature definition. Approved for implementation on 2026-09-04; no
implementation is included in this definition.

This definition supersedes the control-bar width and animated status-marker
decisions in `execution-progress-state-machine.md` and
`chat-tui-status-activity-layout.md`. It preserves the terminal-adaptive
surface colors defined by `terminal-adaptive-chat-surfaces.md`.

## Verified Current Behavior

- A submitted root-run input, a steer input, and each quick-command input use
  the same three-row-minimum control-bar geometry. Root-run and steer bars use
  the full terminal width. Quick-command bars use the lesser of the terminal
  width and the configured progress maximum, which is still full width when
  the terminal is no wider than that maximum.
- The identical right edge makes a steer or quick command look like the start
  of another root Run when the transcript is scanned from the right.
- The status bar reserves column zero for a dim idle marker or an animated
  running spinner. The runnable begins in column two. While running, the
  activity text is `running` below one second and a bare duration thereafter.
- Status animation redraws at a 300-millisecond cadence and retains the running
  presentation for at least 600 milliseconds, including after a short Run has
  already completed.
- The active runnable is shown on the left while running. A differing session
  default runnable and the session default model remain right-aligned.

## Goal and Success Criteria

Make root Run boundaries readable from the control surface's output boundary
and make running state legible without a decorative animation. The feature
succeeds when:

- a submitted input that starts a new root Run targets the terminal width;
- steer and quick-command control bars use the same configured output width as
  execution and command output, which produces a visible right-side distinction
  whenever that output width is narrower than the terminal;
- the status runnable begins in column zero with no marker or spinner;
- active state is expressed as `running` below one second and
  `running for DURATION` from one second onward; and
- status redraws are driven only by state or visible elapsed-time changes.

## Presentation Design

### Root Run Boundary

A root-run submission retains the existing full-width Input surface:

```text
<accent> investigate the failure
```

The surface reaches the right edge and marks the beginning of a new root Run.
The bottom editable prompt remains full width and is not transcript history.

Steer and quick-command inputs retain their existing accents, Input
background, vertical padding, message alignment, and wrapping. Their surface
uses the same width as ordinary output:

```text
<accent> adjust the search                                            |
<accent> /models                                                      |
```

The `|` above illustrates the terminal edge; it is not a rendered glyph. The
auxiliary control width is:

```text
max(1, min(available_width, configured_progress_max_width))
```

`available_width` is the terminal or current Rich render width. This is the
existing output-width rule: on a terminal wider than the configured maximum,
the terminal background remains visible to the right; on a terminal no wider
than the maximum, the bar may use the full available width. No artificial
one-cell gutter is added. Long messages wrap within this output width. A quick-
command control bar aligns with its associated result, help, table, or reopened
output. A steer bar uses the same output width even though it has no result body.

Rejected root-run submissions are not control bars and keep their current
diagnostic presentation.

### Status Bar

Ignoring right-aligned defaults, the left-side forms are exactly:

```text
agic:default
agic:default running
agic:default running for 1s
agic:default running for 1m20s
```

Idle begins with the current default runnable in column zero. Running begins
with the active root runnable in column zero. There is no idle marker, spinner,
accent glyph, or leading padding.

The activity suffix is dim and uses these rules:

- below one elapsed second: ` running`;
- at one elapsed second and later: ` running for DURATION`;
- elapsed time is monotonic, floored to whole seconds, and never displays
  fractional seconds or `0s`; and
- compact durations contain no spaces: `59s`, `1m00s`, `1m20s`, and
  `1h01m01s`.

Spinner styles, frames, indices, and the 300-millisecond frame cadence are
removed. The UI invalidates when running starts or stops, when the active
runnable changes, at the one-second threshold, and once per subsequent elapsed
second. A completed Run returns immediately to idle unless the next queued
root Run has already become active; the old minimum-visibility hold is removed.

The right side preserves current behavior. The current session default model
remains the final right-aligned segment. When the active runnable differs from
the session default, the right side remains
`DEFAULT_RUNNABLE · MODEL`; a matching default runnable is omitted. Session
setting changes continue to update these right-side defaults without changing
the active Run identity. Error status continues to replace the normal
composition with the existing full-width error line.

Width fitting reserves at least one separating cell between left and right
content, keeps the model at the terminal edge, and uses the existing reduction
priority: differing default runnable, active/default left runnable, then model.
If an exceptionally narrow terminal still cannot fit the fixed activity suffix,
that suffix is elided last so the line never exceeds the available width.

## Scope and Design Touchpoints

In scope:

- `src/toolang/cli/toolang/commands/chat/blocks.py`: distinguish full-width
  root-run bars from auxiliary steer and quick-command control widths while
  preserving adaptive Input backgrounds and wrapping;
- `src/toolang/cli/toolang/commands/chat/widgets.py`: remove status markers and
  spinner state, compose the new left-aligned status text, and compact duration
  formatting;
- `src/toolang/cli/toolang/commands/chat/tui.py`: replace spinner animation and
  short-run retention with visible elapsed-time updates tied to active-root
  lifecycle;
- `tests/unit/cli/test_chat_tui.py`: cover exact control widths, wrapping,
  styles, status fragments, duration thresholds, state transitions, queued
  Runs, narrow terminals, and right-edge model alignment;
- `tests/system/cli/test_chat_tui_e2e.py`: update real-terminal status and
  control-boundary expectations;
- `docs/execution-presentation.md`: document the current control-boundary and
  status contracts; and
- `docs/plans/execution-progress-state-machine.md`: replace the superseded
  control-width and status-animation decisions in the umbrella definition.

Out of scope:

- root-run creation, steering, quick-command parsing, or execution semantics;
- control-bar colors, accents, height, padding, text alignment, or Input surface
  resolution;
- prompt-box and queue-panel geometry;
- quick-command result-body, help, table, or reopened-output width;
- active/default runnable selection and session setting behavior;
- model selection, status error content or styling, execution progress, root
  Run footers, and transcript persistence; and
- Script, non-interactive Chat, HTTP, or web presentation.

## Acceptance Tests

1. A `RunControlBlock` still paints every row through the available terminal
   width and retains the three-row minimum.
2. A `RunSteerBlock` uses
   `min(available width, configured progress maximum)` in live and stable
   rendering.
3. Every quick-command block variant uses the same output width for its
   submitted command rows and associated content.
4. On a terminal wider than the configured progress maximum, steer and quick-
   command bars end at that maximum while root-run submission remains full
   width; on a narrower terminal, auxiliary bars may use the full width.
5. Multiline and wide-character messages wrap without overflow or content loss
   under the distinct root and auxiliary widths.
6. Idle status starts with `RUNNABLE` in column zero and contains no marker,
   spinner, activity label, or leading space.
7. A running status below one second starts with `RUNNABLE running`, contains no
   duration, and has no animated fragment.
8. At one elapsed second and later, running status starts with
   `RUNNABLE running for DURATION`; the compact duration changes only on whole-
   second boundaries.
9. Stopping a Run immediately restores the idle form; an immediate queued-Run
   transition changes active identity without showing the wrong runnable.
10. A differing default runnable still renders as
    `DEFAULT_RUNNABLE · MODEL` on the right, and the model's right edge remains
    fixed across idle and running states.
11. Narrow status layouts never exceed the available terminal width and retain
    the existing error-line behavior.
12. No spinner frame, spinner-index, 300-millisecond animation, or minimum-
    visibility behavior remains in Chat status code or tests.
13. The default repository verification passes.

## Risks

- Rich and Prompt Toolkit render the same blocks through different paths; both
  must stop auxiliary background painting at the configured output boundary
  without adding implicit terminal-width padding.
- Off-by-one display-cell accounting for CJK or other wide text could misalign
  the auxiliary bar with its output or wrap one row early.
- Removing minimum activity visibility can make very short Runs briefly show
  `running`; this follows actual active-root state and avoids presenting a
  completed Run as active.
- The longer `running for` suffix increases pressure on narrow status layouts
  and must not move the model past the right edge.

## Open Questions

None.
