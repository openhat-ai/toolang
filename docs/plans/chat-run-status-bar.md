# Chat Run Status Bar

## Status and Goal

Feature definition approved for implementation on 2026-09-29. Move active-run elapsed time above
the queue/input area so run information has its own two-row surface. Keep the
existing bottom bar as the session status bar.

## Verified Current Behavior

- `ChatTuiApp` stacks the live area, a variable spacer, optional queue panel,
  input box, and one-row `StatusBar`.
- `StatusBar` renders elapsed immediately left of the centered `agent@workspace`
  identity. It shows `running` below one second, then durations such as `1m20s`.
- A monotonic timer starts with run submission and clears on run settlement.
  The footer sizing helpers currently reserve one row for the status bar.
- Session errors replace the bottom status line and currently suppress timer-only
  invalidation, which must change when elapsed is independently visible.

## Layout and Behavior

```text
live area
existing variable spacer, when needed
                                      <- run status row 1: blank
  Working for 1m3s                    <- run status row 2
queue area, when present
input box
  runnable       agent@workspace       model
```

- Always allocate two rows for the run status bar, including while idle. Its
  first row is blank; its second row directly touches the queue, or the input
  box when the queue is absent. The existing variable spacer stays above it.
- Put elapsed at display column two (two leading spaces), with dim text and
  the terminal's normal background. Add no border, marker, or heading.
- Keep duration units without zero padding: `1s`, `59s`, `1m0s`, `1m20s`, and
  `1h1m1s`. Preserve whole-second flooring. Show `Working` below one second,
  then `Working for DURATION`, for example `Working for 1m3s`.
- While idle, render no text on either row. Starting each queued run resets
  elapsed; normal completion, failure, and settled cancellation clear it.
  A cancellation request alone does not stop the timer.
- Reserve the concept of a right-side field ending two cells before the right
  terminal edge. Leave it empty: no context data, placeholder, or separator.
- Keep both `Working` and elapsed out of the session status bar. Preserve its
  runnable/model edges, centered identity, workspace lifecycle, and errors.
  Elapsed no longer consumes any of its width budget.
- Session errors must not hide or stop run-bar updates. Keep the existing timer
  cadence and presenter refresh behavior.
- Account for the two new fixed rows in input, queue, live-area, and footer-floor
  sizing. On tiny viewports, retain the existing focus/feedback priorities and
  allow blank spacing to yield before usable input. Fit text by display cells;
  drop insets when needed and truncate without wrapping or negative sizes.

## Scope and Touchpoints

- `src/toolang/cli/toolang/commands/chat/widgets.py`: add the run-bar rendering
  surface and remove elapsed composition from the session bar.
- `src/toolang/cli/toolang/commands/chat/tui.py`: integrate the new surface,
  route elapsed updates, and adjust row budgets and error-time invalidation.
- `tests/unit/cli/test_chat_tui.py`: cover rendering, lifecycle, and sizing.
- `tests/system/cli/test_chat_tui_e2e.py`: verify actual terminal placement.
- `docs/chat.md` and `docs/execution-presentation.md`: update the current layout
  contract after implementation. This plan supersedes elapsed/context placement
  in `chat-status-live-cluster.md`, not its session identity/edge behavior.

No changes to execution events, persisted accounting, context usage collection,
or queue controls.

### Approved Duration Consolidation

The user additionally requested a single duration formatter for all four CLI
surfaces: the run status bar, execution facts (including inspect and run
summaries), operational progress, and agent uptime. Use
`common/time.py` to format numeric seconds without I/O, clock access, or
timestamp parsing. Keep timestamp parsing and live-clock flooring at call sites.
Render `250ms` below one second, otherwise round to whole seconds and show
`1m8s`, `1m0s`, or `1h1m1s` without spaces or zero padding. Zero and negative
values render as `0s`. Remove duplicated formatters and the unused humanize
dependency. Cover unit boundaries and each surface with regression tests.

## Acceptance Tests

1. With and without a queue, the new two-row bar immediately precedes the first
   input-area surface; its first row is blank and elapsed starts at column two.
2. Idle keeps both rows but displays no run information. Starting, completing,
   failing, cancelling, and advancing queued runs follow the lifecycle above.
3. Values at 0, 1, 59, 60, 80, and 3661 seconds match the defined labels; timer
   updates use deterministic clocks in unit tests.
4. The session bar never contains elapsed or `Working`; its identity and edge
   anchors remain stable as time changes. The future right-side field is empty.
5. Elapsed continues repainting while a session error is visible.
6. Multiline input, expanded/collapsed queues, live output, steer feedback, and
   terminal resizing preserve focus and fit available rows without stale lines.
   Extremely narrow or short sizes do not wrap status text or crash rendering.
7. Documentation matches the implementation; `git diff --check` and the default
   Ruff, formatting, ty, and offline pytest checks pass.

## Risks and Open Questions

The permanent two-row cost reduces live-output space; incomplete row-budget
updates could clip the prompt or leave stale terminal rows. Timer ownership
changes must preserve workspace reset and presenter refreshes.

No open questions. The user specified `Working` below one second and
`Working for DURATION` thereafter, with duration units visible and no leading
zeros.
