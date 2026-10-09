# Talk status bar

Status: implementation requested on 2026-10-08 and revised in chat on 2026-10-09.
Online information is deferred until Hub offers a suitable presence subscription.

## Goal and scope

Show the conversation on the left, its canonical ID in the center, and the
viewer's login name on the right. Errors replace the whole footer using Chat's
error row. Inset the normal footer by two terminal cells on both ends, aligned
with the input text.
Rename the messaging command from `text` to `talk`, keeping `write a message` as
the input placeholder and preserving existing key bindings.

## Design

- Expose `too talk [TARGET] [MESSAGE...]` with existing flags, target resolution,
  literal-message parsing, and human identity. Remove `too text` without an alias;
  do not add prefix-agent syntax. Rename the command package and UI notices to
  Talk, retaining `.runtime/text/` for drafts and history.
- Talk resolves a conversation, displays its retained and incoming messages, and
  allows sending for members; observers remain read-only. Keep current canonical
  IDs and cursor-based message reads. Sending failures show the returned error
  and preserve the draft; never automatically retry a send or reconnect from the
  send path. The user chooses the next action.
- A direct conversation with the viewer shows the other participant as `@alice`.
  An observer sees both participant names in stable order, such as `@alice,bob`.
  Groups show their name and total member count, such as `#dev(3)`; strip a leading
  `gc_` from group display names. Use the conversation metadata loaded on entry.
- The `@` or `#` marker is dim when the viewer cannot send and normal otherwise;
  names and counts always use the normal foreground. Do not show
  online counts, unknown-presence placeholders, or green online indicators.
  Do not poll the agent directory or conversation directory from the message loop.
  No new API or subscription protocol is in scope.
- The right side shows the plain login name, including for read-only observers.
  Omit `from`, role prefixes, and read-only suffixes. While connecting, show the
  connection status. Errors replace all three segments with Chat's red `!` row,
  starting in the first column and retaining its trailing inset. Show the actual
  returned error instead of a generic send-failure label; keep drafts and Hub
  identity validation without changing identity in place.
- Sending/sent acknowledgments do not replace the login name. Keep actionable
  send/draft errors visible. Omit `Connected` and key hints.
- Center the full canonical conversation ID within the footer. Never truncate
  the ID into a misleading copy target. On narrow terminals, prioritize the
  login; hide the ID if it cannot fit at the center without overlapping it,
  then truncate the left label by terminal cells. Reduce margins only below five
  cells. Never wrap or overflow.
- Publish only the conversation label (`@alice`, `@alice,bob`, or `#dev`) as a
  sanitized OSC 0 title on interactive TTYs; omit member totals and clear it on exit.
  This updates iTerm2 tab/window titles and tmux's native pane title.
- All Talk windows share the single session named `talk` in the current tmux
  server, creating it only when absent. Use an existing `talk` session without
  replacing its windows; never create suffixed sessions. Mark it with
  `@toolang_talk=talk`; panes use `@toolang_pad=talk`. New window names and
  `@toolang_convo` contain the canonical conversation ID. Window lookup uses that
  mark plus `@toolang_context` (root/backend/login identity), even after a user
  renames the window. Different contexts stay in separate windows of the same
  session. Do not read or migrate the old Text/Group tmux marks.
- Message dividers, input editing, receive retries, and persisted messages remain
  outside this change. Existing standalone directory output is unchanged.

## Touchpoints and acceptance

- Talk CLI passes resolved conversation metadata into its TUI; the Talk status
  renderer owns labels and row geometry. Chat and Talk share the error-row renderer.
- Verify command routing/help, literal flags in messages, absence of a Text alias,
  and restored drafts/history from the existing storage namespace.
- Verify margins, centered canonical IDs, direct/member/observer labels, group
  member totals, permission marker dimming, Unicode truncation, configured widths,
  and connection/send/draft errors.
- Verify OSC 0 publication/cleanup, non-TTY behavior, safe terminal text, and tmux
  shared session placement across conversations and contexts, canonical marks,
  reuse after manual window renaming, and preservation of unrelated windows.
- Verify that history and live reads never request the full directories, including
  after reconnection, and that retained history is not replayed.
- Preserve Hub identity validation and draft preservation tests. Run default checks.
  Changelog updates are deferred at the user's request.

The main risk is clipped error text on narrow terminals. No open questions.
