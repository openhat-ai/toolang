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
  The areas retain distinct backgrounds. Queue's outermost cells always use its
  background. Input retains its cyan accent and only shows its cursor on focus.
- Queue's summary is normal when focused and dim when unfocused, in both modes.
  It is centered at the top when expanded and in its only row when collapsed.
  Entry actions and panel actions have separate hint locations.
- Queue selection remains valid while items are removed or automatically
  submitted, and the panel resets when the queue becomes empty.
- Queue management and steering use keyboard controls only. `/queue`, `/q`,
  `/steer`, and `/s` are unregistered and follow normal unknown-command handling.
- Run cancellation, input clearing, and conditional exit are
  Input-scoped. Queue focus never makes those keys act on Input or the run.
- Contextual panel hints and `/keys` teach the complete interaction without
  requiring slash-command help.

## Scope

In scope:

- interactive Prompt Toolkit input and queue key bindings;
- expanded, collapsed, focused, selected, and empty Queue panel states;
- queue edit, steer, and delete actions;
- prompt history and draft acceptance for direct steering;
- queue-aware layout sizing, truncation, focus recovery, and invalidation;
- shortcut metadata, `/keys`, Chat documentation, and offline tests;
- removal of queue/steer slash handlers and their unused context methods;
- behavior-preserving entry rendering and test cleanup.

Out of scope:

- changing server or client steer protocols;
- persisting queued inputs across Chat processes;
- queue reordering, mouse controls, or a full-screen queue picker;
- a generalized tab strip or focus framework for future Chat areas;
- adding queue behavior to scripted Chat;
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
never between the two areas. Queue's outermost cell on each side always uses
Queue's background; neither indicates focus. The summary uses normal text while
focused and dim text while unfocused, in both modes. Focus never shifts content
horizontally.
Expanded layout:

- A summary, such as `3 items queued`, is centered across the full panel in
  the top row, without a special header fill.
- The middle shows up to eight one-line previews, following selection. There is
  no separate omitted-entry count row; the summary counts the entire queue.
  Short terminals show fewer entries to reserve Input, status, summary, and
  panel-hint rows. At least one entry remains visible while expanded.
- Entry numbers align with Input text, two cells from the left edge. Numbers
  remain dim in selected and unselected entries; body text stays normal.
  There is no selection marker or bold selection text. Only while Queue is
  focused does the selected entry use Input's background, inset one cell on
  each side with one more cell of padding inside each end of the highlight.
  Its right side shows `meta+enter steer · e edit · d delete` in slightly brighter
  gray dim text on that same background. At least two cells separate the body
  and hints; previews truncate first, and hint truncation preserves right padding.
  Unselected and unfocused entries use the available content width without hints.
- Panel actions (focus switching, expansion, and selection) appear at the bottom
  right, directly above Input, flush with the terminal's right edge to align
  with the unchanged status bar. Collapsed panel hints use the same alignment;
  selected-entry hints retain their two-cell right inset. On narrow terminals
  panel hints flow between complete actions. Individual overlong hints truncate
  to their available width.

Losing focus hides both selection highlighting and entry hints, but preserves
the selected index and scroll position for the next focus transition.

With one hint row, expanded height ranges from three rows for one item to ten
rows for eight or more items.

Collapsed Queue is a single focusable row with a centered summary and
right-aligned contextual key hints. There are no extra padding rows. Centering
never uses only the space left by hints. If the right margin cannot fit every
hint, show complete hints in order, prioritizing expansion; never overlap or shift the
summary. No queue content appears inside Input. Previews, summaries, and entry
hints truncate by display cells on narrow terminals, including wide Unicode.
Entry numbers and the start of the preview retain space before hints truncate.

Input's one-cell accent remains cyan regardless of focus. Its cursor is hidden
while Queue has focus and returns to the preserved editing position when focus
returns. Focus and expansion never change the width of either area.

### Keyboard Scope

The scope revision was approved in the 2026-09-03 implementation review.

- Input only: Enter, Meta+Enter with the draft, Ctrl+J, optional Shift+Enter,
  history navigation, Esc Esc, Ctrl+C, and Ctrl+D.
- Queue only: Space; selection, edit, delete, and Meta+Enter with an entry also
  require expansion. Input-only control keys are inert.
- Shared: Tab/Shift+Tab switch focus; Esc only dismisses transient status and
  never moves focus; Ctrl+L clears the display when idle; Ctrl+Q explicitly exits
  Chat from either area. No special Queue override for Ctrl+L is needed.

Repeated Esc while Queue is focused never cancels the active run. Full `/keys`
help groups actions by scope; inline hints keep their existing wording and order.

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

Inline hints use lowercase `key action`, dim styling, no brackets, and ` · `
between actions. Abbreviate Space as `sp`; chords retain `+` (`meta+enter`),
and arrows represent navigation (`↑↓`). Full `/keys` help retains standard
key labels and alternate keys in parentheses. Optional Shift+Enter stays
qualified with `also Shift+Enter if supported`. The notation does not change
bindings. Keep `Meta` portable rather than claiming physical Cmd support.

Panel hints depend on focus and expansion:

| State | Panel hints | Selected-entry hints |
| --- | --- | --- |
| Unfocused, either mode | `tab focus` | None |
| Focused, collapsed | `sp expand · tab input` | None |
| Focused, expanded | `↑↓ select · sp collapse · tab input` | `meta+enter steer · e edit · d delete` |

Panel hints are bottom-right when expanded and beside the summary when collapsed.
Selected-entry hints appear only at the right edge of that entry.
Within each tier, frequent actions for the current state come first; preserve
that order when wrapping or truncating hints.

Tab hints use verbs: `focus` enters Queue and `input` returns to typing. Full
help documents aliases and action preconditions; the panel does not repeat them.

The shortcut help keeps these concise rows under their respective focus scopes:

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

- `QueuePanel` composes summary, entry, and footer rows. Entry rendering owns
  its padding, truncation, and styles directly, without reparsing fragments.
- `ChatTuiApp` owns queue mutations. Every removal, including FIFO dequeue,
  reconciles selection and empty-queue focus through one helper.
  It supplies Queue's available height after reserving Input and status rows.
- `shortcuts.py` owns key definitions and scoped help groups. Input, Queue,
  and shared bindings remain explicit; no general focus framework is needed.
- Rendering tests assert final text positions and cell styles, not fragment
  ordering. Separate visual contracts from focus/expansion/resize transitions.

Files:

- `src/toolang/cli/toolang/commands/chat/shortcuts.py`
- `src/toolang/cli/toolang/commands/chat/slashes.py`
- `src/toolang/cli/toolang/commands/chat/base.py`
- `src/toolang/cli/toolang/commands/chat/main.py`
- `src/toolang/cli/toolang/commands/chat/events.py`
- `src/toolang/cli/toolang/commands/chat/widgets.py`
- `src/toolang/cli/toolang/commands/chat/tui.py`
- `docs/chat.md`
- `docs/input-syntax.md`
- `docs/execution-presentation.md`
- `tests/unit/cli/test_chat_tui.py`
- `tests/unit/cli/test_chat_slashes.py`
- `tests/unit/cli/test_chat_command.py`
- `tests/system/cli/test_chat_tui_e2e.py`

## Acceptance Tests

- Prompt bindings distinguish submit, direct steer, canonical newline, and
  optional Shift+Enter without parsing Meta+Enter input.
- Enter starts an idle run, queues during starting, active, and disconnected
  states, and still executes immediate interactions during a run.
- Direct steer retains an empty or rejected draft, records and clears an
  accepted draft, and creates the existing steer control presentation.
- The Queue area covers absent, default-expanded, collapsed, unfocused,
  focused, more-than-eight, narrow-terminal, and automatically emptied states.
  Eight- and twelve-row terminals reduce the viewport instead of obscuring Input.
- Both modes occupy the full terminal width across resize and directly adjoin
  Input, without a separator or queue text inside Input's padding.
- The summary is normal when focused and dim when unfocused, without bold text.
  It stays centered across the full panel: at the top when
  expanded and in the only row when collapsed, with right-aligned panel hints.
- Queue's outermost cells always use Queue's background, in both modes and
  regardless of focus. The summary, not the edges, indicates focus.
  Queue uses its own background, distinct from Input, with no special header
  fill. Only while focused does the selected entry use Input's background,
  including its dim action hints but excluding one cell on each side. Unfocusing
  hides all selection styling without losing the selected index.
  Input's accent stays cyan and its cursor tracks focus.
- Unfocused Queue shows only the focus-in hint. Focused, collapsed Queue shows
  only focus-out and expand. Focused, expanded Queue shows focus-out, collapse,
  and selection at the bottom right, and edit, delete, and steer only on the
  selected entry's right side. Neither tier repeats the other's actions.
- Expanded hints flow between complete actions on narrow terminals. Collapsed
  hints omit actions that cannot fit to the right of the centered summary.
- Panel hints align with the status bar at the terminal's right edge in both
  modes and focus states, including wrapped footer hints after resize.
- Entry hints end two cells from the right edge. Selected entries have
  one cell of highlighted inner padding on each side, and at least two cells
  between body and hints. Truncation preserves this padding and gap.
- Entry numbers align with Input text and stay dim regardless of selection;
  body text stays normal. Selected hints use a brighter gray while staying dim.
  There is no selection marker; only the background indicates selection.
  Long and CJK previews truncate without wrapping.
  There is no omitted-count row, even beyond eight items; the summary counts all.
- Space toggles only focused Queue without moving focus; Tab never expands it.
  Input spaces and draft steering retain their behavior. Hidden entries cannot
  be selected or mutated.
- Tab and Shift+Tab focus transitions preserve completion behavior, and
  Up/Down and Ctrl+P/Ctrl+N selection remain deterministic.
- Edit covers empty and non-empty prompt drafts and documents rebuilt request
  snapshot behavior.
- Meta+Enter steers the selected item; missing or blocked active runs keep it
  queued. d and Del remove it and clamp selection.
- Inline hints are lowercase, dim, and unbracketed, with `sp` for Space and
  middle dots between actions. Full `/keys` labels and aliases stay unchanged.
  All hint rows remain width-limited, and dim styling does not leak into entry bodies.
- Automatic FIFO dequeue reconciles panel state without changing queued request
  snapshots.
- `/keys` groups shared, Input, and Queue actions and preserves optional
  Shift+Enter wording. Removed commands and aliases cannot mutate the queue,
  selection, draft, or run in interactive or scripted Chat.
- Real key dispatch verifies that Esc Esc, Ctrl+C, and Ctrl+D are inert
  in both Queue modes and keep their Input behavior; Esc stays in Queue,
  Ctrl+L and Ctrl+Q remain global, and Meta+Enter still targets the focused source.
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
