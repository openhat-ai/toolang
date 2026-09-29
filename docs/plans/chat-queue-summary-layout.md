# Chat queue summary layout

Status: approved for implementation on 2026-09-29.

## Goal and success criteria

Save one row in the expanded queue while leaving the left side below
`Working for 1m3s` visually empty before the first queue entry. Keep a
discoverable, focusable collapsed queue with a centered count and right-aligned hint.

## Current behavior and scope

Verified against `QueuePanel` and `RunStatusBar` in
`src/toolang/cli/toolang/commands/chat/widgets.py` at `f39551d8`:

- Expanded queues contain a left-aligned summary, a blank row, up to eight
  visible entries, and a trailing blank row. Collapsed queues use one summary row.
- Tab transfers focus; Space toggles expansion while the queue is focused.
- Empty queues disappear and reset expansion. Queue additions and removals
  preserve expansion while items remain.
- Run status shows `Working` before one second, then `Working for <duration>`.
  Duration is compact, without added spaces or zero padding: `1m3s`.

Change queue presentation and its height accounting only. Preserve queue
execution, shortcuts, selection, preview actions, run status text and timing,
input behavior, and session diagnostics. Do not add a spinner or completion label.

## Layout and states

Remove the expanded queue's old summary row. Place the count and hint in its
former blank row, immediately above the entries. Leave the left portion
of that row blank; this is visual breathing room, not an additional empty row.
Keep the trailing blank row between entries and Input.

When queue entries disappear, preserve the input's lowest reached screen
position while terminal geometry and draft height stay unchanged. Reuse freed
rows as live spacing above the queue/input area, consume that spacing as live
content grows, and never serialize it into scrollback. Existing footer-floor
accounting already provides this behavior in the verified scenarios; add
regression coverage without changing it unless a failing case is reproduced.

Center the count relative to the full panel width, independently of hint length.
Right-align the hint with two terminal cells of right padding, matching existing
entry hints and status content. Do not display disclosure icons or parentheses.
Keep the queue background and magenta leading accent on every queue row.

| Queue state | Centered count | Right-aligned hint | Body |
| --- | --- | --- | --- |
| Empty | Absent | Absent | No queue rows |
| Expanded, unfocused | `4 queued` | `tab to focus` | Entries, then one blank row |
| Expanded, focused | `4 queued` | `space to collapse` | Selected entry highlighted; existing entry actions retained; trailing blank row |
| Collapsed, unfocused | `4 queued` | `tab to focus` | No entries or trailing blank row |
| Collapsed, focused | `4 queued` | `space to expand` | No entries or trailing blank row |

Expanded height becomes visible entry count + 2; minimum expanded height
becomes 3. Collapsed height stays 1. Empty height stays 0.

Examples below omit backgrounds, accent strips, input padding, and session
status. Blank rows between entries and Input are intentional.

```text
  Working for 1m3s
                         2 queued             tab to focus
  ↳ First request
  ↳ Second request

  Ask or describe a task
```

```text
  Working for 1m3s
                         2 queued          space to expand
  Ask or describe a task
```

When no queue exists, preserve current run-status/Input spacing. When no run
is active, hide the Working label as today; a retained queue still displays
its normal state and summary. Keep errors in the existing session status.

## Styling and transitions

- Use dim styling for the unfocused count. When focused, render the count in
  normal foreground with bold weight; keep the hint dim. This also makes
  collapsed focus visible.
- Preserve the existing selected-row background and actions when expanded.
- Tab changes focus without changing expansion; Space changes expansion only
  while the queue is focused. Item actions remain unavailable while collapsed.
- Preserve expansion and selection as items arrive or dequeue. Emptying the
  queue removes its summary, returns queue focus to Input, and resets the next
  nonempty queue to expanded.
- Preserve the existing eight-entry limit and selected-item viewport behavior.
  Use the saved row in existing height budgeting; do not increase that cap.

## Width, height, and risks

Never wrap the summary. Use terminal cell widths for alignment. Center the count
at floor((panel width - count cell width) / 2), reserving the accent cell and
clamping to the available content area on extremely narrow terminals. Show the
hint only if it fits completely with at least two blank cells after the centered
count; otherwise omit it without moving the count. If the count itself cannot
fit beside the accent, truncate it using the existing ellipsis behavior. Count
text takes priority over horizontal padding. Preserve two-cell right padding
whenever it fits; clamp padding for extremely narrow widths and never overwrite
the accent or overflow.

Retain existing short-terminal prioritization while updating frame/minimum
row counts together. Main risks are off-by-one viewport sizing, stale cells
after collapse or resize, and ambiguous focus without a selected entry.
Renderer tests must cover these cases, including wide-character previews.

## Implementation touchpoints and acceptance checks

- `src/toolang/cli/toolang/commands/chat/widgets.py`: update queue row
  composition, frame sizing, count/hint alignment, and focus styles.
- `src/toolang/cli/toolang/commands/chat/tui.py`: inspect queue minimum/available
  row calculations and change only assumptions that depend on frame height.
- `tests/unit/cli/test_chat_tui.py`: update existing layout expectations and
  cover the state matrix using actual rendered cells and style attributes.
- `tests/system/cli/test_chat_tui_e2e.py` and
  `tests/support/chat_tui_e2e.py`: update plain-hint expectations and verify
  input position during gated FIFO draining in a real terminal.

Acceptance checks:

1. All four nonempty focus/expansion states match the table; empty queues
   render no panel. Expanded panels save exactly one row for equal entry counts.
2. Running and idle combinations preserve status behavior, including `Working`,
   `Working for 1m3s`, and clearing elapsed status after the run ends.
3. Counts stay centered independently of hint text; hints end two cells before
   the right edge at ordinary widths. No disclosure icons or parentheses appear.
   Narrow widths degrade as specified without wrapping or overlap.
4. Tab, Space, item navigation, edit, steer, delete, queue additions, FIFO
   removal, and empty-to-nonempty transitions preserve existing behavior.
5. Expanded focus highlights the selected entry; collapsed focus visibly
   emphasizes its summary. Unfocused entries show no item action hints.
6. Short terminals, long queues, wide-character previews, resize, and multiline
   Input retain correct sizing and repaint without stale content.
7. Removing entries through empty leaves reusable live spacing, without a
   scrollback write. Growing live output consumes it without moving Input.
   Gated FIFO completion does not move Input upward in the terminal grid.

For implementation, run focused queue/status renderer tests, then the required
`uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
`uv run pytest -n auto`. For this definition, verify source references and run
`git diff --check`.

## Open questions

None. The user approved implementation and pull request creation.
