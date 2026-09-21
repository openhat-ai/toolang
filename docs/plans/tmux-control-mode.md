# Shared tmux clients and Chat routing

Status: routing rule confirmed by the user: select or create-and-select within
the invoking session; create or reuse without selection across sessions.
Metadata scopes remain uniform. Approved for implementation and a pull request.
Work type: feature definition.

## Goal

Reuse or create the correct Chat target while ordinary terminal clients and
control clients share tmux state. Avoid redundant session reconstruction and
ambiguous client switching. A successfully prepared target remains useful even
when the invoking terminal stays where it is.

## Identity and configuration

Control mode belongs to a client, not to a session. Attachments must not change
the metadata schema or reinterpret existing containers:

| Scope | Identity |
| --- | --- |
| Session | `@toolang_agent` |
| Window | `@toolang_thread` |
| Pane | `@toolang_pad=chat` |

Preserve agent-session/thread-window organization, metadata-based lookup after
user renames, and existing metadata lifetimes. Reject the earlier proposal for
control-specific pane identity. Keep OSC 0 publication and clearing, TTY gates,
`TOOLANG_TMUX` semantics, and asynchronous publication unchanged.

## Routing rule

Apply the same rule to ordinary and control clients. Control-mode detection is
not needed for this policy.

| Target state when routing is needed | Action |
| --- | --- |
| Agent session absent | Create it detached, with one initial chat window/pane |
| Session exists, thread window absent | Create one detached chat window |
| Thread window exists, chat pane absent | Create one detached chat split there |
| Chat pane exists | Reuse it; start no second Chat process |

- Preserve the existing branch that runs Chat in place when already inside the
  correct agent session and no existing thread target redirects the invocation.
  This feature does not redesign that branch's container ownership behavior.
- Use explicit detached creation: `new-session -d`, `new-window -d`, and
  `split-window -d`, through libtmux with concrete arguments. Use stable IDs for
  targets. A missing thread ID keeps the existing `new_chat` naming until first
  submission produces the ID; never invent a thread ID for placement.
- Once the target is prepared, select its window/pane only if it belongs to the
  invoking session. Selection is shared tmux state and may affect other clients
  attached to that session. If it is already the current pane, run in place.
  This applies equally to an existing target and a newly created window/pane:
  same-session creation is followed by selection; cross-session creation is not.
- For a target in another session, leave both sessions' selections alone. Do not
  call `switch-client` or use `attach-session` as a fallback. Do not automatically
  activate a separate iTerm connection attached to the destination.
- Report whether the target was created or reused and its actual session/window/
  pane location. For a cross-session target, explicitly say it was not selected.
  Use the resolved current names and IDs, not only the agent's derived name.
- Creating/reusing a target and selecting it have separate results. A failed or
  deliberately skipped selection must not destroy a successfully created window,
  or launch a second Chat in the invoking pane. Report the prepared target and
  return (with a nonzero exit and the error for failed selection). If creation
  itself fails, report that failure and do not claim success or write identity
  onto an unrelated container. Show the tmux error in the invoking
  terminal and exit nonzero. Success confirms tmux creation, not subsequent TUI
  startup; a child readiness protocol is outside scope.
- A spawned Chat must enter its prepared pane without repeating placement and
  recursively opening more splits. Pass process-local placement context into
  the child entry point and verify the target pane before bypassing routing;
  add no new tmux identity option or user-facing environment setting.

## Verified behavior and limits

An isolated tmux 3.7c experiment confirmed that switching a client to its already
attached session still emits `%session-changed`. iTerm2 3.6.11 handles that event
by closing its mapped panes and reopening session windows. Removing redundant
client switches addresses that mechanism; the original user's complete event
trace has not been captured.

With control clients attached to sessions A and B, selecting a window in B left
both clients attached to their original sessions. Both received the window-change
notification, but iTerm ignores it for a different attached session. Ordinary
selection cannot make connection A display session B.

Detached session/window/pane creation was also verified in an isolated server:
existing selections remained unchanged. Detached does not mean invisible. When
iTerm is attached to the destination session, a genuinely new window can appear
as a GUI tab/window and a new split changes its visible layout. Toolang will not
change iTerm preferences or promise zero GUI additions when creating objects.

## Touchpoints

- `src/toolang/cli/common/tmux.py`: detached creation with returned target IDs;
  same-session selection separated from creation and client attachment.
- `src/toolang/cli/toolang/commands/chat/main.py`: explicit routing outcomes,
  child placement context, and accurate created/reused location messages.
- `tests/unit/cli/test_tmux_launcher.py`, relevant Chat command tests, and isolated
  tmux integration tests; `docs/chat.md` for the uniform no-cross-session-switch
  behavior. Metadata schema changes are outside scope.

## Acceptance

1. For each missing level, create exactly one required target and no duplicates.
   A spawned Chat starts once without recursively placing itself.
2. Reusing an existing chat starts no new process. Same-session selection emits
   no `%session-changed`; cross-session routing moves no client and changes no
   existing selection in the destination.
3. Successful creation survives skipped/failed selection. Failed creation gives
   a truthful failure without a second in-place launch or foreign identity writes.
4. Ordinary and control clients sharing a session read identical metadata.
   Attach/detach clients after startup; identity and routing policy remain stable.
5. Test missing session/window/pane, renamed containers, linked windows, mixed
   clients, new chats without IDs, disabled integration, and non-TTY execution.
6. Preserve OSC title/clearing, focus-report filtering, queue/input keyboard
   behavior, and asynchronous publication. No mouse reporting, iTerm API,
   client-mode polling, or terminal preference changes.
7. Verify actual iTerm `-CC` behavior, distinguishing legitimate new objects from
   reopening existing GUI windows. Run default lint, format, type, and offline
   tests before committing implementation.

## Risks and open questions

The confirmed rule stops automatic cross-session switching for ordinary clients
too; this avoids mode-dependent behavior and originating-client guesses.
No routing-policy question remains. Existing crash cleanup and simultaneous-launch
races are not redesigned; targeted tests must prevent deterministic duplicate
spawning introduced by placement recursion.

## References

- [tmux Control Mode](https://github.com/tmux/tmux/wiki/Control-Mode)
- [tmux command manual](https://man.openbsd.org/tmux)
- [tmux 3.7c client selection](https://github.com/tmux/tmux/blob/3.7c/cmd-find.c)
- [iTerm2 3.6.11 notification filtering](https://github.com/gnachman/iTerm2/blob/v3.6.11/sources/TmuxGateway.m)
- [iTerm2 3.6.11 session and tab handlers](https://github.com/gnachman/iTerm2/blob/v3.6.11/sources/TmuxController.m)
