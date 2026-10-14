# Chat queue count and action hints

Status: approved for implementation on 2026-10-13.

## Goal and scope

Simplify the queue above Input: show only a dim count in the summary's upper
right corner and use compact shortcut hints on the selected entry.

This definition supersedes count placement, count focus styling, summary hints,
and selected-entry hint text in [Chat queue summary layout](chat-queue-summary-layout.md)
and [Chat queue controls](chat-queue-panel.md). All other contracts remain in effect.
Scope is presentation only; queue actions, bindings, focus, expansion, sizing,
and execution retain their current behavior.

## Verified baseline

At `21830d5f`, `QueuePanel` in
`src/toolang/cli/toolang/commands/chat/widgets.py` centers `N queued`, makes the
count normal when focused, and places `tab to focus` or `space to expand/collapse`
at the summary's right edge. Selected entries show
`m-enter steer · e edit · d delete`. `shortcuts.py` binds steering to Escape then
Enter and uses `m-enter` as the queue's existing inline abbreviation.

## Decisions

- In every nonempty focus/expansion state, the summary contains only `N queued`,
  right-aligned with two terminal cells of right padding. It is always dim,
  with normal weight and the existing queue background. Focus does not restyle it.
- Remove the summary's focus and expansion hints. Retain the leading accent,
  summary row, entry viewport, and expanded queue's trailing blank row.
- Only the focused, selected entry shows `(m-enter)steer (e)dit (d)elete`,
  using single spaces between actions. Use the existing dim hint style and
  two-cell right padding. Parentheses mark the shortcut; retain `m-enter` for
  consistency with the existing abbreviation.
- Preserve selected-entry truncation: keep at least two cells between preview
  and hint, shorten the preview first, and truncate hints with the existing
  ellipsis behavior when the full hint cannot fit.
- Summary alignment uses terminal cell widths. When the count and two-cell
  padding cannot fit beside the accent, reduce padding before truncating the
  count. Do not wrap, overwrite the accent, or exceed the panel width.
- Empty queues remain absent. Tab, Shift+Tab, Space, navigation, edit, steer,
  delete, and `/keys` keep their existing behavior and help text.

## Implementation touchpoints

- `src/toolang/cli/toolang/commands/chat/widgets.py`: summary composition,
  count styling, selected-entry hint formatting, and removal of unused summary
  hint code/styles. Keep shortcut formatting local to queue presentation.
- `tests/unit/cli/test_chat_tui.py`: summary alignment, focus styles, hint text,
  narrow widths, and real rendered-cell assertions.
- `tests/system/cli/test_chat_tui_e2e.py`: update affected terminal hint expectations.
- `docs/chat.md`: align the queue presentation documentation with this definition.
- `CHANGELOG.md`: generate the user-visible entry through the existing
  `too aid.too update_changelog` runnable during implementation.

## Acceptance checks

1. Expanded/collapsed and focused/unfocused queues show the count at the right
   inset with dim rendered attributes; no summary shortcut hint appears.
2. Only the focused selected entry shows the exact compact hint, with dim
   attributes, the existing selection background, and two-cell right padding.
3. Ordinary, narrow, and wide-character layouts fit terminal cell widths;
   summary padding degrades as specified and entries retain existing truncation.
4. Focus, expansion, selection, queue mutations, short-terminal layout, and
   input position retain existing behavior. The default verification passes.

## Risks and open questions

Removing summary hints and focus emphasis reduces discoverability, especially
when collapsed. This is the requested tradeoff; `/keys` still documents controls.
Narrow-width alignment and stale hint cells after repaint require coverage.

No open design questions. The user approved implementation and a pull request
including this definition.
