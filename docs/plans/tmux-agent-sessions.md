# Tmux Agent Sessions

Status: Approved for implementation on 2026-09-16. Revised 2026-09-17 after review: every
value now has one scope, and container state (session and window) is separated from the pad.

## Goal

`too <agent> chat` should put chat windows where the user expects them, in the user's
own tmux server:

```text
tmux server = the user's server (not a dedicated one)
session     = agent   name = agent name    @toolang_agent
window      = thread  name = thread id     @toolang_thread_id, @toolang_thread_title
pane        = pad                          @toolang_pad (`chat` today)
```

The session and the window are **containers**: the launcher creates them and later chats
reuse them, so their names and options outlive any one chat. The pad is the pane's role
and ends with chat. A pad is a readable and writable thread view; `chat` is the only kind
today, later `shell`, `logs` and friends share the thread id in the same window.

`prefix w` reads none of the options on its own, so it shows the container names:

```text
eve
  term_xxx
  new_chat
```

`docs/chat.md` ships a recommended `-F` that shows the thread title (or the pad kind
before the thread exists); that is the intended way to label chats.

Outside tmux nothing changes: `too <agent> chat` is still a plain terminal app.

## Behaviour

`too <agent> chat [--thread ID]` decides where to run:

| situation | behaviour |
| --- | --- |
| not inside tmux | run chat in the current terminal |
| inside tmux, current session is the agent's session | run chat in this pane; publish the pad and thread metadata |
| inside tmux, another session, `--thread` container with a live chat pad | switch the client to that window, print the notice, exit 0 |
| inside tmux, another session, `--thread` container without a live chat pad | open a chat pad (split) in that window, switch the client to it, print the notice, exit 0 |
| inside tmux, another session, otherwise | ensure the agent's session, open a window running `too <agent> chat …`, switch the client to it, print the notice, exit 0 |

The notice is one line on stdout: `↪ opened in tmux session <agent>`.

## Metadata

Every value is written at exactly one scope:

| option | scope | value | written |
| --- | --- | --- | --- |
| `@toolang_agent` | session | agent name | the launcher, when it determines the agent's session |
| `@toolang_thread_id` | window | full thread id, e.g. `term_6xp42qxg` | the launcher with `--thread`, chat otherwise, as soon as the id exists |
| `@toolang_thread_title` | window | thread title, single line, at most 60 display columns | chat, once the thread has runs |
| `@toolang_pad` | pane | `chat` | chat, when it starts |

One scope per value, because tmux inherits user options: a window with no value of its own
reads its session's, so the agent is visible across the whole session while the thread
marks stay on the one window that shows that thread. Those options are the index the
launcher reads (`session.show_option("@toolang_agent")`,
`window.show_option("@toolang_thread_id")`), so no thread data is read to answer a lookup
and a user renaming a session or window breaks nothing.

Names follow the same convention and are cosmetic: the agent's session is named after the
agent, and a thread's window after its thread id — `new_chat` while a new chat has no id,
the full id once chat knows it. Nothing else is renamed, no name is restored, and the pane
title is left to the user's own shell or theme. Chat never writes the agent mark.

Outside tmux, or before a value exists, only what exists is published.

## Thread lookup and reuse

The lookup only runs when `--thread ID` was given (a new chat has no id yet), and it is a
libtmux read: resolve `Server.from_env()`, walk `server.sessions` for the agent's session,
then `session.windows` and `window.show_option("@toolang_thread_id")`. When a bulk read
matters, one `server.cmd("list-windows", "-F", …)` call is used instead — `Server.cmd` is
still the library's own API. Several matches pick the newest.

A thread's window is a container that may have outlived its chat, so a match is not enough
to switch to it: the launcher also reads the active pane's pad
(`window.active_pane.show_option("@toolang_pad")`). A live chat pad means the thread is
open and the client switches to that window; otherwise the launcher splits a fresh chat
pad into the same window and switches to that, instead of opening a second window for the
thread.

## Lifecycle

Chat exits → its pane exits. A window whose only pane it was is destroyed, and the agent's
session with it when that was its only window, so an unconfigured `prefix w` keeps no stale
entry. A window that outlives the chat because it holds other pads keeps its name and
thread options — they are container state. Only the pad mark is unset, so the pane stops
claiming to be a chat and the container can host the next one.

## Session naming

The agent's session is named after the agent, sanitized as recorded in
`docs/plans/tmux-pane-marks.md`:

- trailing `.` and `:` are rewritten to `_` by tmux, so sanitize before using the name as a
  target;
- if that name is taken by a session that is not this agent's, add a numeric suffix instead
  of renaming someone else's session;
- never create a second session for the same agent: find it by the `@toolang_agent` session
  option first, name second. Determining the session records the option, so the name is
  only a fallback.

## Non-goals

- No dedicated server, no `tmux.conf` management, no status-line or key-binding
  configuration. The recommended `-F` in `docs/chat.md` is a copy-paste example, not
  something toolang installs. (The `/tmp/toomux` prototype explored the managed half; it is
  not part of this.)
- No change to `too inspect`, thread data, or trace behaviour.
- Nothing for users who never use tmux: no new option to set, no new output.

## Acceptance Tests

1. Not inside tmux: chat runs in the current terminal, zero tmux calls.
2. Inside tmux in the agent's session: chat runs in this pane, the pane carries the pad, and
   once the thread exists the window carries the thread id and title in its name and options.
3. Inside tmux in another session with `--thread` on a live chat pad: one `switch-client` to
   that window, no new window or pane, notice printed, exit 0.
4. Inside tmux in another session with `--thread` on a container without a live chat pad: a
   chat pad is opened in that window, the client switches to it, notice printed, exit 0.
5. Inside tmux in another session otherwise: session ensured, window opened with the chat
   command and named `new_chat` (or the thread id with `--thread`), `switch-client` to it,
   notice printed, exit 0, and the original pane returns to a shell.
6. `prefix w` lists the container names with no configuration; the recommended `-F` shows
   the thread title, the thread id, or `[chat]` instead.
7. Chat exit clears the pad only: a pane that outlives chat is no longer marked a chat, and
   its window keeps its name and thread options.
8. Session naming: sanitization, foreign-name collision adds a suffix, an existing session
   is never renamed.
9. Every tmux interaction is best-effort: a failing tmux call falls back to running chat in
   place instead of failing the command.

## Risks

- Chat may start in a different place than before; `docs/chat.md` must state the rule.
- Window names are a convention, not the index: a user rename breaks nothing, but the
  launcher also never repairs it, so `prefix w` can show a name that no longer matches the
  thread until chat renames it again.
- Two `too <agent> chat` calls racing on a brand-new thread both create windows, because
  the thread id does not exist until the first message. Each window resolves to its own
  thread; acceptable and identical to today's behaviour.
- A container whose chat is gone is reused by `--thread`, but a plain `too <agent> chat`
  opens a new window; the two differ only in whether the thread was named.
- `switch-client` needs a client in the target server; when none is attached the launcher
  attaches instead of switching.

## Implementation Notes

- Delta to plan #1 (superseded at scope level): plan #1 published all three marks on the
  pane. The pad (`chat`) stays at pane scope as the pane's role and is the only mark cleared
  on exit; the thread id and title move to the window that shows the thread; the agent moves
  to the session, where every window reads it by inheritance.
- `cli/common/tmux.py` owns the vocabulary and the tmux work: the option names and scopes,
  `Marks` (one scope per name, rejects a name without one, `name_window`), `resolve_marks()`
  for the running pane, and `Launcher` (`agent_session`, `thread_window`, `chat_pad_active`,
  `name_window`, `mark_thread`, `open_pad`, `open_window`, `switch_client`,
  `close_window`).
- `chat/marks.py` keeps the lifecycle: it writes the pad, the thread id and title, and the
  container name, and clears only the pad. `chat/main.py` implements the behaviour table.
- **Every tmux interaction goes through libtmux, never through a toolang-owned subprocess.**
  Typed calls where the library has them (`Server.from_env`, `server.sessions`,
  `session.windows`, `window.show_option`, `server.new_session`, `session.new_window`,
  `window.split`, `server.switch_client`, `session.attach`), and `Server.cmd(...)` — the
  library's own escape hatch — for one-shot format reads such as `list-windows -F`. libtmux
  itself invokes the tmux binary, so the launcher keeps its reads few: one call per session
  it inspects, not one per option.
- Files: `src/toolang/cli/common/tmux.py`, `src/toolang/cli/toolang/commands/chat/main.py`,
  `src/toolang/cli/toolang/commands/chat/marks.py`, `docs/chat.md`, and tests for the
  behaviour rows above.
