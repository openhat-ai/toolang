# Locate, create, and enter a Chat pane

Status: approved in conversation, including eager thread creation for launcher
paths. Supersedes PR #575's original same-session-only policy. Process-title
naming and service process identity are separate work.

## Goal and success criteria

Inside an interactive tmux terminal, `too <agent> chat` ensures one target
agent/session, thread/window, and chat/pane, starts Chat if needed, and enters it.
Outside tmux, run directly without tmux operations. Preserve failed Chat panes
and their error output, including immediate startup failures.

## Identity and thread allocation

| Scope | Identity |
| --- | --- |
| Session | `@toolang_agent` |
| Window | `@toolang_thread` |
| Pane | `@toolang_pad=chat` |

Keep scopes identical for ordinary and control clients, including shared
sessions. Locate by metadata rather than display names, use stable IDs for
operations, and qualify linked windows with their intended session.

Before placement, validate a requested thread or create an empty thread through
`ThreadManager` in the agent's shared execution store. Allocation does not start
an agent or model. Each new Chat gets its own thread/window; empty threads remain
if the user exits before submission. Direct execution retains lazy allocation.

New sessions use agent names, new windows use thread IDs. Existing marked
containers keep user-assigned names. An unowned matching session name may be
adopted; another agent's session is never overwritten.

## Launcher boundary

Keep placement policy and tmux operations inside the launcher. CLI orchestration
resolves the agent layout, thread, terminal/environment conditions, working
directory, and chat options, then supplies concrete values to the launcher.
Construct child argv from those resolved values, not the invoking command text.
Reuse the existing public command, factories, and parameter parsing.

`TOOLANG_TMUX` controls both launcher and metadata publication. False values
(`0`, `false`, `no`, `off`) disable both. Otherwise placement/publication require
TTY stdin/stdout plus `TMUX` and `TMUX_PANE`. Outside tmux, never query, create,
or attach to a server. OSC 0 titles independently require TTY stdin/stdout.

The launcher publishes target metadata and directly creates missing objects
with a command that starts `TOOLANG_TMUX=0 too ... chat --thread <id>`. No empty
pane, waiting placeholder, separate startup protocol, hidden `_chat` command,
`--here`, or private `_TOOLANG_CHAT_PANE` marker is needed. The child runs Chat
without reentering placement or publishing metadata. Child startup never waits
for the parent to finish navigation.

A key binding can call the public Chat command from any session. A binding that
wants Chat to stay in its own dedicated pane uses `TOOLANG_TMUX=0`. It deliberately
omits metadata, while preserving OSC titles. Do not infer intent from pane age.

## Resolution and navigation

| Target state | Action |
| --- | --- |
| Missing session | Create it with the first Chat window/pane |
| Missing thread window | Create its Chat window/pane |
| Live chat exists | Reuse; do not start another process |
| Exact thread window, current unmarked pane, no live chat | Run here |
| Retained failed Chat, no live chat | Explicit invocation restarts that pane |
| No Chat pane | Create a split in the thread window |

Being in the agent's session alone never justifies repurposing the current
window. All new objects are created detached; select after preparation.

| Target location | Navigation |
| --- | --- |
| Current pane | None |
| Same window, another pane | Select pane |
| Same session, another window | Select window and pane |
| Another session | Select window and pane, then switch one client |

Use tmux's normal current-client resolution. With shared clients this may select
the most recently active eligible client; do not promise exact input-origin
identification. Never switch every client. Window/pane selections are shared
state. Same-session operations never call `switch-client`; cross-session iTerm
window rebuilding is expected for an actual session change.

## Errors and lifetime

The creation command first sets its own `remain-on-exit failed`, then executes
Chat with `TOOLANG_TMUX=0`. Expand `TMUX_PANE` inside that new pane. A short shell
prefix establishes retention before Chat can fail; no global option is changed.
If retention setup fails, show the error and wait for Enter without starting Chat.

- Creation failure: report in the invoking terminal and return nonzero.
- Chat startup/runtime failure: keep pane, output, and exit status, including
  when it is the last pane of the window/session. It remains selectable.
- Navigation failure: keep the target, report its location and error, return
  nonzero, and never start a duplicate local chat.
- Normal exit: created pane closes; in-place Chat returns to its shell. Newly
  created sessions use `detach-on-destroy off` to allow client fallback.

Failed panes retain pad identity; check `pane_dead` before reusing a live Chat.
Explicit retry can respawn the failed pane without killing another live process.
There is no automatic retry. Parent creation success does not claim child startup
readiness; later errors are displayed in the target, not forwarded to the caller.
Preserve normal-exit OSC clearing and error visibility after the alternate screen.

## Touchpoints and acceptance

Files: CLI common tmux launcher, Chat command orchestration, existing launcher
unit tests, isolated tmux integration tests, and `docs/chat.md`.

- Cover the missing/existing session/window/pane matrix and in-place execution.
- Verify early persistent allocation, existing-thread validation, canonical
  child options, empty-thread persistence, and direct-mode lazy allocation.
- Verify disabled/outside/non-TTY paths perform no placement or publication;
  independent OSC behavior remains intact. Child Chat starts exactly once.
- Cover renamed containers, linked windows, ordinary and control clients.
  Same-session selection emits no session-change notification; cross-session
  navigation switches one client and selects the intended target.
- Cover immediate and later nonzero exits in each creation path, retained errors
  in singleton sessions, explicit retry, normal exit, creation/navigation errors,
  and retention setup failure. Never force-replace a live unrelated process.
- Run default lint, formatting, type checks, and offline tests before commits.
  Real iTerm GUI behavior remains a manual check after updating the PR.

## Risks and exclusions

Allocation moves work to startup and can leave empty threads; both are accepted.
No mouse reporting, iTerm preference changes, custom control protocol, process
renaming, or service identity changes. No unresolved behavior decisions remain.

## References

- [tmux command manual](https://man.openbsd.org/tmux)
- [tmux Control Mode](https://github.com/tmux/tmux/wiki/Control-Mode)
- [tmux 3.7c client selection](https://github.com/tmux/tmux/blob/3.7c/cmd-find.c)
- [iTerm2 3.6.11 session handling](https://github.com/gnachman/iTerm2/blob/v3.6.11/sources/TmuxController.m)
