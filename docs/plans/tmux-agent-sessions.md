# Tmux Agent Sessions

Status: Proposed on 2026-09-16; needs human approval before implementation.

## Goal

`too <agent> chat` should put chat windows where the user expects them, in the user's
own tmux server:

```
tmux server = the user's server (not a dedicated one)
session     = agent
window      = chat / thread
pane        = the chat process
```

which makes `prefix w` show exactly one entry per open chat:

```
eve
  term_xxx: "hello world"
  term_yyy: "debug parser"
ta
  term_zzz: "fix tests"
```

Outside tmux nothing changes: `too <agent> chat` is still a plain terminal app.

## Behaviour

`too <agent> chat [--thread ID]` decides where to run:

| situation | behaviour |
| --- | --- |
| not inside tmux | run chat in the current terminal |
| inside tmux, current session is the agent's session | run chat in this pane; publish window metadata |
| inside tmux, another session, `--thread` already open in the agent's session | `switch-client` to that window, print the notice, exit 0 |
| inside tmux, another session, otherwise | `ensure` the agent's session, open a window running `too <agent> chat …`, `switch-client` to it, print the notice, exit 0 |

The notice is one line on stdout: `↪ opened in tmux session <agent>`.

## Window metadata

Each chat window carries the same three values as the pane marks defined in
`docs/plans/tmux-pane-marks.md`:

| option | value |
| --- | --- |
| `@toolang_agent` | agent name |
| `@toolang_thread_id` | full thread id |
| `@toolang_thread_title` | thread title, single line, at most 60 display columns |

Plus the presentation that makes `prefix w` readable without any configuration:

- window name = thread id once known (so `term_xxx` identifies the chat);
- pane title = thread title (tmux's built-in tree line already renders
  `window_name: "pane_title"` for single-pane windows — verified against tmux 3.2a).

Outside tmux, or when the thread is not known yet, only what exists is published.

## Thread lookup

The lookup only runs when `--thread ID` was given (a new chat has no id yet):

```
tmux list-windows -t <agent> -F '#{@toolang_thread_id}'   # or list-panes -a
```

A match switches to that window; several matches pick the newest. No thread data is read
for this: the marks are the index.

## Lifecycle

Chat exits → its pane exits → tmux destroys the window, so there is no bookkeeping and no
stale entry in `prefix w`. The marks are unset on exit (plan #1), which covers windows that
outlive the chat because they hold extra panes.

## Session naming

The agent's session is named after the agent, sanitized as recorded in
`docs/plans/tmux-pane-marks.md`:

- trailing `.` and `:` are rewritten to `_` by tmux, so sanitize before using the name as a
  target;
- if that name is taken by a session that is not this agent's, add a numeric suffix instead
  of renaming someone else's session;
- never create a second session for the same agent: find it by the `@toolang_agent` session
  option first, name second.

## Non-goals

- No dedicated server, no `tmux.conf` management, no status-line or key-binding
  configuration (the `/tmp/toomux` prototype explored those; they are not part of this).
- No change to `too inspect`, thread data, or trace behaviour.
- Nothing for users who never use tmux: no new option, no new output.

## Acceptance Tests

1. Not inside tmux: chat runs in the current terminal, zero tmux calls.
2. Inside tmux in the agent's session: chat runs in this pane, and once the thread exists the
   window carries the three options, the window is named after the thread, and the pane title
   is the title.
3. Inside tmux in another session with `--thread` already open: one `switch-client` to that
   window, no new window, notice printed, exit 0.
4. Inside tmux in another session otherwise: session ensured, window opened with the chat
   command, `switch-client` to it, notice printed, exit 0, and the original pane returns to a
   shell.
5. `prefix w` shows `term_xxx: "title"` for an open chat, with no tmux configuration.
6. Session naming: sanitization, foreign-name collision adds a suffix, an existing session is
   never renamed.
7. Every tmux interaction is best-effort: a failing tmux call falls back to running chat in
   place instead of failing the command.

## Risks

- Chat may start in a different place than before; `docs/chat.md` must state the rule.
- Renaming a window is intrusive when it holds several panes, so only single-pane windows are
  renamed (the same condition tmux uses to show the title suffix).
- Two `too <agent> chat` calls racing on a brand-new thread both create windows, because the
  thread id does not exist until the first message. Each window resolves to its own thread;
  acceptable and identical to today's behaviour.
- `switch-client` needs a client in the target server; when none is attached the launcher
  attaches instead of switching.

## Implementation Notes

- Delta to plan #1 (merged): plan #1 publishes the three marks on the **pane**; this plan adds
  the same three as **window** options (the window is what the launcher and any window-scoped
  tmux format read) plus the window name and pane title. The chat process publishes both, so
  an in-place chat needs no launcher-side writing.
- Delta to plan #1 (open decision): pane marks could be dropped in favour of window marks
  only. Recommendation: keep both — the pane mark is the process-level truth used by plan #1's
  consumers and tests, the window mark is the session-level metadata this plan is built on.
- `cli/common/tmux.py` grows the operations the launcher needs (`list_windows`, `ensure_session`,
  `open_window`, `switch_client`) instead of adding a second tmux layer; the launcher decision
  lives in `chat/main.py`.
- Files: `src/toolang/cli/common/tmux.py`, `src/toolang/cli/toolang/commands/chat/main.py`,
  `src/toolang/cli/toolang/commands/chat/marks.py`, `docs/chat.md`, and tests for the four
  behaviour rows above.
