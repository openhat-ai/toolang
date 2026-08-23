# Define Chat Status Activity Layout

## Work Type and Status

Feature definition approved and implemented on 2026-08-21.

## Verified Current Behavior

- The idle status begins with two spaces, the default runnable, and no marker.
- The running status shows the animated spinner after the runnable, followed by
  `running` below one second and elapsed time thereafter.
- The default model remains right-aligned. While running with a different
  default runnable, that default appears immediately before the model.
- Error status replaces the normal composition with a full-width error line.

## Goal and Success Criteria

Restore the status glyph to the bottom-left corner while keeping the runnable
and activity label stable. The feature succeeds when the idle marker or running
spinner occupies column zero, the runnable begins in column two, running shows
`running` below one second and elapsed time thereafter, and the existing
right-side alignment and active/default state rules remain intact.

## Presentation Design

The stable shapes are:

```text
■ flow:research                                    openai/gpt-5
◓ flow:research running                            openai/gpt-5
◓ flow:research 18s                                openai/gpt-5
◓ agic:chat 18s                 flow:research · openai/gpt-5
```

The idle marker or running spinner begins in column zero, followed by one space
and the runnable in column two. The idle marker is dim; the running spinner
retains its active accent. A dim activity label follows the runnable only while
running. That label is `running` below one elapsed second and the whole-second
duration thereafter. It never renders `0s`, `for`, or an extra separator glyph.

The model remains the final, right-aligned segment. A differing default
runnable retains the existing `DEFAULT_RUNNABLE · MODEL` form on the right.
Error status and status-bar background behavior are unchanged.

## State and Width Rules

- Idle uses the current default runnable and default model, with the marker
  rendered through dim `class:status.marker` styling.
- Running uses the active root runnable on the left and the current defaults on
  the right under the existing active/default rules.
- The default spinner rotates through `◐`, `◓`, `◑`, and `◒` at the existing
  300-millisecond cadence with short-run retention. It inherits the terminal's
  normal foreground without dim styling.
- The dim activity label is `running` below one second, then uses the existing
  whole-second elapsed formats.
- Width fitting accounts for the two-cell status prefix and running activity. It
  preserves at least one separating cell before the right-side content and
  truncates labels under the existing priority while keeping the model at the
  terminal edge.
- Stopping activity replaces the spinner with the idle marker and removes the
  activity label.

## Scope and Touchpoints

In scope:

- `src/toolang/cli/toolang/commands/chat/widgets.py`: compose idle and running
  status segments, style the static marker as dim, and retain width accounting.
- `tests/unit/cli/test_chat_tui.py`: cover exact idle/running order, styles,
  state transitions, narrow widths, and right-edge alignment.
- `tests/system/cli/test_chat_tui_e2e.py`: update real-terminal expectations
  for the left-corner status glyph and activity label.
- `docs/execution-presentation.md` and
  `docs/plans/execution-progress-state-machine.md`: update the current status
  presentation contract.

Out of scope:

- active/default runnable selection and queued-run behavior;
- model selection or right-side composition;
- spinner frames, cadence, elapsed-time formatting, and activity retention;
- input box, queue panel, control bars, execution progress, and error styling.

## Acceptance Tests

1. Idle status starts with `IDLE_MARKER RUNNABLE`, with a dim marker in column
   zero and runnable in column two.
2. Running status below one elapsed second starts with
   `SPINNER RUNNABLE running` and does not contain `0s`.
3. At one elapsed second and later, running status starts with
   `SPINNER RUNNABLE DURATION`, in that order, with the spinner using
   unmodified `class:status.spinner` and duration using
   `class:status.elapsed`.
4. Stopping activity restores the idle marker in column zero.
5. A differing default runnable still renders as
   `DEFAULT_RUNNABLE · MODEL` on the right.
6. The model's right edge is unchanged across idle and running states at a
   fixed terminal width.
7. Narrow layouts fit or truncate all labels without exceeding terminal width.
8. Short-run retention and queued-run transitions retain the existing state
   semantics while using the new segment order.
9. The default repository verification passes.

## Risks

- Incorrect fixed-width accounting could move the model edge or overflow narrow
  terminals.
- Retained tests must distinguish the idle marker from running spinner frames
  while both occupy the first fragment.
- Moving the spinner must not change animation state or active-runnable
  selection.

## Open Questions

None.
