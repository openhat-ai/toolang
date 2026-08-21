# Define Chat TUI Active and Default Status

## Work Type and Status

Feature definition. Approved for implementation on 2026-08-21.

## Verified Current Behavior

- The status bar always renders the current session default runnable on the
  left and the current default model on the right.
- While a run is active, the spinner and elapsed time are attached to that
  default runnable even when the run uses a different runnable.
- A model `StepBegin` may replace the displayed default model with the model
  used by the latest model step.
- Chat setting commands remain available while a run is active. Queued calls
  retain the settings captured when they were submitted.

## Goal and Success Criteria

Keep the one-line status bar while distinguishing the active root runnable
from mutable session defaults. The feature succeeds when:

- setting commands remain available and update session defaults immediately;
- an idle status shows the default runnable on the left and default model at
  the terminal's right edge;
- a running status shows the active root runnable and elapsed time on the left;
- when the active and default runnables differ, the default runnable appears
  immediately before the default model on the right;
- the rightmost model always represents the session default and keeps the same
  right edge as runnable settings change; and
- no active or child model is projected into the status bar.

## Presentation Design

The stable shapes are:

```text
■ flow:research                                  openai/gpt-5
◩ flow:research · 18s                            openai/gpt-5
◩ agic:chat · 18s             flow:research · openai/gpt-5
```

The animated marker continues to identify running state, so the status omits
`running` and `running for`. Elapsed time uses ` · DURATION` after the active
runnable. The differing default runnable has no redundant `default` label.

The model is always the last segment and remains right-aligned. Adding,
changing, or removing the differing default runnable consumes or releases
padding to the model's left rather than moving the model's right edge.

## State Rules

- Chat setting commands are accepted in both idle and running states. They are
  neither queued nor applied to the active run.
- While idle, both displayed values come from current session defaults.
- At submission, the queued call's runnable snapshot provides the provisional
  active label. The root `RunBegin.runnable` replaces it with execution truth.
- Child `RunBegin` and model-step events do not change status identity.
- While running, model setting changes update the rightmost default model
  immediately. Runnable setting changes update only the differing-default
  segment.
- Run completion retains the active runnable through the existing short-run
  visibility interval. It returns to the idle default after the running marker
  stops, unless the next queued call starts first.

## Scope and Touchpoints

In scope:

- `src/toolang/cli/toolang/commands/chat/tui.py`: resolve and retain active and
  default runnable state; keep the model default-only.
- `src/toolang/cli/toolang/commands/chat/widgets.py`: render the one-line
  left/right composition and stable model alignment.
- `tests/unit/cli/test_chat_tui.py`: cover state transitions, settings changes,
  queued snapshots, rendering, styles, elapsed time, and terminal width.
- `docs/execution-presentation.md`: document the revised status contract.

Out of scope:

- command syntax or policy-merging changes;
- changes to active runs or queued-call setting snapshots;
- active-model, child-run, or model-step presentation;
- execution event, persistence, API, or transcript changes.

## Acceptance Tests

1. Idle output shows the qualified default runnable on the left and the
   resolved default model at the right edge.
2. Running output shows the root runnable followed by ` · DURATION`.
3. A differing default runnable renders as `DEFAULT_RUNNABLE · MODEL` on the
   right, while an equal default runnable is omitted.
4. Changing the default runnable during a run adds, replaces, or removes only
   the right-side runnable segment.
5. Changing the default model during a run updates the rightmost model without
   changing the active runnable.
6. Root `RunBegin` replaces the provisional active runnable; child run and
   model-step events do not.
7. The model's right edge remains fixed across idle, equal-running, and
   differing-running states at a fixed terminal width.
8. Short-run retention and immediate queued-run transitions do not attach the
   spinner to a default runnable that is not active.
9. The default repository verification passes.

## Risks

- Clearing active identity before the minimum running-visibility interval ends
  would briefly attach the spinner to the default runnable.
- Resolving a provisional runnable from mutable defaults instead of the queued
  call would mislabel queued work.
- Long labels can consume the padding needed for a stable one-line layout;
  truncation must use display-cell width and preserve the model as the final
  segment.

## Open Questions

None.
