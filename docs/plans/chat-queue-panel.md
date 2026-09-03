# Add Interactive Chat Queue Controls

## Status

Approved in the 2026-09-02 design discussion, with layout, keyboard-hint,
focus, and spacing revisions approved on 2026-09-03. This definition
follows the slash-command output work and is authoritative for interactive queue
presentation, input submission keys, and steer shortcuts.

## Goal

Give terminal Chat one predictable input model: Enter sends, Meta+Enter steers,
and Ctrl+J inserts a newline. Queued runnable inputs should remain visible and
editable through a compact keyboard-operated panel without taking attention
away from the active run.

## Success Criteria

- Enter submits runnable input when idle and queues it while a run is starting
  or active; slash commands and other immediate interactions retain their
  existing behavior.
- Meta+Enter always treats the current draft or selected queue item as a raw
  steer message and never parses, submits, or queues it.
- Ctrl+J inserts a newline. Shift+Enter does the same when the terminal and
  Prompt Toolkit expose that key distinctly.
- A non-empty queue appears expanded above the input and never steals focus
  when a new item arrives.
- Tab and Shift+Tab move focus between the visible Queue and Input areas while
  preserving completion-menu behavior.
- Space toggles the focused Queue without moving focus; Tab only changes focus.
  Selection is preserved, but actions on hidden entries are disabled.
- Queue uses the full terminal width and joins Input without a separator row.
  The areas retain distinct backgrounds. Queue shows its left accent only on
  focus; Input retains its cyan accent and only shows its cursor on focus.
- Queue's summary stays centered in its last row, directly above Input.
  Collapsed Queue occupies only that row. Hints reflect focus and expansion.
- Queue selection remains valid while items are removed or automatically
  submitted, and the panel resets when the queue becomes empty.
- The existing hidden `/queue`, `/q`, `/steer`, and `/s` commands retain their
  behavior for compatibility but are not restored to `/help`.
- Contextual panel hints and `/keys` teach the complete interaction without
  requiring slash-command help.

## Scope

In scope:

- interactive Prompt Toolkit input and queue key bindings;
- expanded, collapsed, focused, selected, and empty Queue panel states;
- queue edit, steer, and delete actions;
- prompt history and draft acceptance for direct steering;
- queue-aware layout sizing, truncation, focus recovery, and invalidation;
- shortcut metadata, `/keys`, Chat documentation, and offline tests.

Out of scope:

- changing server or client steer protocols;
- persisting queued inputs across Chat processes;
- queue reordering, mouse controls, or a full-screen queue picker;
- a generalized tab strip or focus framework for future Chat areas;
- adding queue behavior to scripted Chat;
- removing or documenting the hidden queue and steer slash commands;
- changing slash-command output or execution progress presentation.

## Input Model

The prompt owns three distinct actions:

| Key | Idle | Run starting or active |
| --- | --- | --- |
| Enter | Submit runnable input | Queue runnable input |
| Meta+Enter | Keep the draft and report that no run is active | Send the normalized draft as steer input |
| Ctrl+J | Insert a newline | Insert a newline |
| Shift+Enter | Insert a newline when supported | Insert a newline when supported |

Enter continues to dispatch recognized slash commands and `:?` immediately,
including during a run. Runnable inputs retain their current request-building
and setting-snapshot behavior before entering the queue.

Meta+Enter is represented at the terminal layer by the standard Meta sequence,
`Escape` followed by `Enter`. A terminal may map a physical macOS Command-Enter
keystroke to that sequence. Meta+Enter bypasses slash, colon-override, prompt,
and runnable parsing, so a draft beginning with `/`, `:`, `$`, or `@` is still
sent as literal steer text. Empty drafts do nothing. A locally accepted steer
is recorded in input history and clears the unchanged draft. If no accepted
active run exists, Chat retains the draft and shows a transient status error.

Alt-Enter no longer inserts a newline because it occupies the same terminal
Meta sequence as Meta+Enter. Ctrl+J is the canonical multiline key. The
conditionally registered Shift+Enter binding remains best effort.

## Queue and Input Areas

The Queue area is absent when the queue is empty. The first queued input makes
it visible and fully expanded above the prompt without moving Input focus.
Further queued inputs update it without changing focus or selection.
New items and FIFO dequeue preserve the expanded/collapsed choice until the
queue empties, when the next non-empty queue defaults to expanded again.

Queue and Input are the only focus-switching areas in this scope. Tab and
Shift+Tab move between them without changing expansion. In Input, these bindings
yield to an active completion menu. When the queue is empty, normal Prompt
Toolkit Tab behavior remains unchanged.

Queue occupies the full terminal width in both modes, directly above Input
without a separator row or horizontal margins. Queue and Input retain distinct
backgrounds. Any dynamic space used to stabilize the footer belongs above Queue,
never between the two areas. A one-cell steer-purple left accent spans every
Queue row only when focused; otherwise that column uses the Queue background.
Its space stays reserved so focus never shifts content horizontally.
Expanded layout:

- Right-aligned dim key hints occupy the top row, without a special header fill.
  Hints describe only the focused/expanded state's available actions. On narrow
  terminals they flow onto additional rows between complete actions.
- The middle shows up to four one-line previews, following selection. If any
  entries are outside the visible window, a dim content row below them reports
  `… N items not shown` before the summary.
- Only the selected entry displays a `>` marker. All entries use the full
  content width; none contain action hints. Selection stays visible when
  Queue is unfocused.
- A dim summary, such as `3 items queued`, is centered across the full panel in
  its last row, directly adjoining Input's top padding. No Queue row follows it.

With one hint row, expanded height ranges from three rows for one item to seven
rows when entries are omitted.

Collapsed Queue is a single focusable row with the same centered summary and
right-aligned contextual key hints. There are no extra padding rows. Centering
never uses only the space left by hints. If the right margin cannot fit every
hint, show complete hints in order, prioritizing Tab; never overlap or shift the
summary. No queue content appears inside Input. Previews and summaries truncate
by display cells on narrow terminals, including wide Unicode. Only hints flow
between rows; individual overlong hints truncate to the available row width.

Input's one-cell accent remains cyan regardless of focus. Its cursor is hidden
while Queue has focus and returns to the preserved editing position when focus
returns. Focus and expansion never change the width of either area.

Focused Queue bindings are:

| Key | Action |
| --- | --- |
| Space | Expand or collapse Queue without changing focus or selection |
| ↑ / ↓ (also Ctrl+P / Ctrl+N) | Select the previous or next queued input without wrapping |
| e | Remove the selected item and place its source in the prompt for editing |
| Meta+Enter | Send the selected source as steer input |
| d (Del) | Remove the selected item |
| Tab (Shift+Tab) | Return focus to Input |

Selection, edit, steer, and delete apply only while Queue is expanded and
focused. Collapsed Queue accepts only focus switching and Space toggling, not
hidden-entry actions. In Input, Space inserts a space and Meta+Enter steers
with the draft. Ctrl+P/Ctrl+N are explicitly bound for Queue selection. Input
retains its existing history bindings.

Editing never overwrites a non-empty prompt draft. In that case the item stays
queued and Chat reports a transient instruction to clear the draft first. A
successful edit focuses the prompt. Resubmitting the edited source builds a new
request from the then-current session settings.

Steering removes the selected queue item only after Chat locally accepts the
steer request. Without an accepted active run, the item remains selected and a
transient status error is shown. A successful steer uses the existing Run steer
control block; it does not create separate slash-command output.

Delete removes immediately without confirmation. After any removal, selection
moves to the item now occupying the same position or to the new last item. If
the queue becomes empty, the area disappears, resets selection, and returns
focus to the prompt.

Run completion retains FIFO behavior: the oldest queued request automatically
becomes the next run. If the Queue remains non-empty, its selection is clamped
after the dequeue; if it becomes empty, focus returns to the prompt.
Ambiguous remote acceptance continues to pause queued submission and prevents
new edit, delete, or steer mutations through the same blocked-state policy.

## Shortcut Help

Inline hints use `[Key] Action`, with sentence-case actions and ` · ` between
actions. Use `+` for chords (`Meta+Enter`, `Ctrl+J`), spaces for consecutive
presses (`Esc Esc`), lowercase for unmodified letters (`e`, `d`), and arrows
for navigation (`↑`, `↓`). Full help puts alternate keys in parentheses;
inline hints show only the primary key. Optional Shift+Enter stays qualified
with `also Shift+Enter if supported`. The notation does not change bindings.
Keep `Meta` portable rather than claiming a terminal exposes physical Cmd.

Panel hints depend on focus and expansion:

| State | Hints |
| --- | --- |
| Unfocused, either mode | `[Tab] Queue` |
| Focused, collapsed | `[Tab] Input · [Space] Expand` |
| Focused, expanded | `[Tab] Input · [Space] Collapse · [↑↓] Select · [e] Edit · [d] Delete · [Meta+Enter] Steer` |

Tab labels name the focus destination. Full help continues to document aliases
and action preconditions; the panel does not repeat them.

The global shortcut help uses these concise rows:

```text
Enter            Send input (submit or queue)
Meta+Enter       Steer active run
Ctrl+J           Insert a newline (also Shift+Enter if supported)
Tab (Shift+Tab)  Switch focus (input/queue)
```

Existing navigation, cancel, interrupt, clear, and exit rows remain. A short
Queue-focused section documents Space, ↑/↓ (Ctrl+P/Ctrl+N), e, d (Del), and
Meta+Enter, including selection keys that are not repeated in every entry.

## Design Touchpoints

Likely files:

- `src/toolang/cli/toolang/commands/chat/shortcuts.py`
- `src/toolang/cli/toolang/commands/chat/events.py`
- `src/toolang/cli/toolang/commands/chat/widgets.py`
- `src/toolang/cli/toolang/commands/chat/tui.py`
- `docs/chat.md`
- `docs/input-syntax.md`
- `docs/execution-presentation.md`
- `tests/unit/cli/test_chat_tui.py`
- `tests/system/cli/test_chat_tui_e2e.py`

## Acceptance Tests

- Prompt bindings distinguish submit, direct steer, canonical newline, and
  optional Shift+Enter without parsing Meta+Enter input.
- Enter starts an idle run, queues during starting, active, and disconnected
  states, and still executes immediate interactions during a run.
- Direct steer retains an empty or rejected draft, records and clears an
  accepted draft, and creates the existing steer control presentation.
- The Queue area covers absent, default-expanded, collapsed, unfocused,
  focused, more-than-four, narrow-terminal, and automatically emptied states.
- Both modes occupy the full terminal width across resize and directly adjoin
  Input, without a separator or queue text inside Input's padding.
- The summary is centered across the full panel in its last row in every state,
  directly above Input. Collapsed Queue has one row, with right-aligned hints.
- Queue shows its left accent only when focused, in both modes. Its column uses
  the Queue background when unfocused and stays reserved to prevent shifting.
  All Queue content uses its own background, distinct from Input, with no
  special header fill.
  Input's accent stays cyan and its cursor tracks focus.
- Unfocused Queue shows only the focus-in hint. Focused, collapsed Queue shows
  only focus-out and expand; focused, expanded Queue also shows navigation,
  collapse, edit, delete, and steer. No entry contains action hints.
- Expanded hints flow between complete actions on narrow terminals. Collapsed
  hints omit actions that cannot fit to the right of the centered summary.
- Long and CJK previews truncate without wrapping. Omitted counts follow the
  visible entries and precede the bottom summary.
- Space toggles only focused Queue without moving focus; Tab never expands it.
  Input spaces and draft steering retain their behavior. Hidden entries cannot
  be selected or mutated.
- Tab and Shift+Tab focus transitions preserve completion behavior, and
  Up/Down and Ctrl+P/Ctrl+N selection remain deterministic.
- Edit covers empty and non-empty prompt drafts and documents rebuilt request
  snapshot behavior.
- Meta+Enter steers the selected item; missing or blocked active runs keep it
  queued. d and Del remove it and clamp selection.
- Inline hints and `/keys` share canonical labels. Aliases appear only in full
  help, all hint rows stay width-limited, and the bound key sequences are unchanged.
- Automatic FIFO dequeue reconciles panel state without changing queued request
  snapshots.
- `/keys` shows the new mental model on terminals with and without distinct
  Shift+Enter support; hidden slash compatibility tests continue to pass.
- The default offline verification suite passes.

## Risks

- Terminal emulators do not expose a portable physical Command-Enter key.
  Toolang binds the portable Meta sequence and documents terminal mapping.
- Tab focus switching must not capture Tab or Shift+Tab while Input completion
  candidates are active.
- Queue changes can coincide with run terminal events. All mutations stay on
  the UI event loop and reconcile selection immediately after list changes.

## Open Questions

None.
