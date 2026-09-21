# Tmux Chat routing

Status: approved, including eager thread allocation and the `[new chat]` OSC
placeholder. Process titles and service process identity are separate work.

## Goal and scope

Interactive `too <agent> chat` in tmux locates or creates one target chat pane
and enters it. Missing targets are created detached with Chat as their command.
Outside tmux, non-TTY, or with `TOOLANG_TMUX=0`, run directly without placement.
The switch also disables metadata publication; OSC 0 independently requires
TTY stdin/stdout. Keep the public CLI and existing factories; add no entry point,
private pane marker, empty-pane protocol, or `--here` option.

## Identity and ownership

| Scope | Metadata | Default name |
| --- | --- | --- |
| Session | `@toolang_agent` | Agent name |
| Window | `@toolang_thread` | Thread ID |
| Pane | `@toolang_pad=chat` | OSC title |

CLI orchestration validates a supplied thread or creates an empty thread through
`ThreadManager` before placement. Empty threads persist if the user exits without
submitting; direct execution retains lazy allocation. The launcher owns tmux
routing and publishes metadata, then starts the public command with
`TOOLANG_TMUX=0` and the resolved thread ID, root/agent, options, and directory.

Locate by metadata, preserving renamed containers. An unowned matching session
name may be adopted; foreign ownership gets a suffixed name. Keep the same scopes
for ordinary/control clients. OSC initially shows `[new chat]` until the real
title arrives, independently of window identity; clear OSC on normal exit.

## Resolution and navigation

| Condition | Action |
| --- | --- |
| Session/window missing | Create it with its Chat pane |
| Live Chat exists | Reuse it |
| Current unmarked pane in the exact thread window, no live Chat | Run here |
| Failed Chat exists, no live Chat | Explicit invocation retries its pane |
| No Chat pane | Create a split |
| Current target pane | No navigation |
| Same window, different pane | Select pane |
| Same session, different window | Select window and pane |
| Different session | Select window/pane, then switch one client |

A new chat always gets its own thread/window. Never issue redundant same-session
`switch-client`. Qualify linked windows with session IDs. Use tmux's normal
current-client selection: exact input-origin identification is not guaranteed
with shared clients; window/pane selection itself is shared state. Real session
changes may rebuild iTerm's mapped windows.

## Errors and bindings

The creation command sets its own `remain-on-exit failed` before executing Chat.
Nonzero startup/runtime exits retain output and status even in a singleton
session. Setup failure displays the error and waits for Enter without starting
Chat. Successful exit closes the created pane; new sessions set
`detach-on-destroy off`. No global tmux options are changed.

Lookup, creation, and navigation failures are reported to the caller. Failed
lookups must not be mistaken for absent targets. Navigation failure preserves
the target; no duplicate local fallback. Creation success does not imply child
readiness. Explicit retry never kills an unrelated live process; no retry loop.

Success notices use `switched to chat pane <target>`, `created chat pane <target>`,
or `restarted chat pane <target>`. Use `session:window.%pane_id` so the address
works as a tmux `-t` target. In-place startup prints no placement notice.

Bindings use the normal Chat command for routing, or `TOOLANG_TMUX=0` to remain
in their dedicated pane without metadata. OSC remains enabled in both cases.

## Implementation and acceptance

Touchpoints: `cli/common/tmux.py`, Chat orchestration/title, their unit tests,
isolated tmux integration tests, their CI dependency, and `docs/chat.md`.

Verify missing/existing targets, current-pane execution, early allocation and
empty-thread persistence, canonical child options, disabled/non-TTY bypass,
metadata scopes, renamed/linked windows, ordinary/control clients, single-client
cross-session switching, lookup/creation/navigation failures, immediate/later
child failure, retained errors/retry, normal exit, and OSC placeholder updates.
Run default lint/format/type checks and offline tests before commits. iTerm GUI
behavior remains a manual check. Linux exit tests use a pinned tmux build with
the upstream utempter/SIGCHLD fix (#4559); users need tmux 3.6+ for reliable exit
handling on affected builds. No mouse reporting or iTerm settings changes.

References: [tmux manual](https://man.openbsd.org/tmux),
[Control Mode](https://github.com/tmux/tmux/wiki/Control-Mode).
