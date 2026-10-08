# Text status bar

Status: implementation requested in chat on 2026-10-08; the human confirmed that
online counts include agents only and the denominator includes all members.

## Goal and scope

Replace Interactive Text's connection and shortcut footer with a compact row:
`from bryan` on the left, the conversation name centered, and `1/3` on the right.
Inset both ends by two terminal cells, aligned with the input text.

## Design

- Normally show `from <viewer name>` without the typed identity prefix. Append
  `· read-only` for observers. Keep the current input placeholder and key bindings.
- Replace the identity with `Connecting…`, `Reconnecting…`, `Stopped`, or
  `Reopen Text` while that state applies. A Hub HTTP 401 means the existing
  identity/token is no longer accepted; Text cannot switch identity in place.
  Keep the explanatory notice and draft. Never display `Connected`.
- Sending/sent acknowledgments do not replace the identity. Preserve actionable
  send/draft errors and existing notices. Remove shortcut hints from this row.
- Custom/system groups show their existing name (for example `gc_abc123` or
  `all`). Direct conversations show `dm_` plus the other participant's name;
  observers see both participant names in stable order, such as `dm_alice_bob`.
  These are display labels only; canonical conversation IDs remain unchanged.
- Refresh membership and online-agent counts through existing Hub directory data
  every ten seconds and after reconnection. Count only online agents belonging
  to the current group; count every member in the denominator. Before the first
  refresh or while disconnected, show `?/N` rather than a stale online count.
- Center against the complete content area, independently of side-label lengths.
  Truncate Unicode by terminal cells. On narrow terminals, omit the center before
  crowding the sides, then shorten the left label; omit the count only when it
  cannot fit. Reduce margins only below five cells. Never wrap or overflow.
- No human presence tracking, new endpoints, conversation renames, or changes to
  message dividers, input editing, transport retries, or persisted messages.

## Touchpoints and acceptance

- Text CLI passes resolved conversation metadata into its TUI. A Text-owned status
  renderer owns labels and row geometry; the TUI caches directory data off-render.
- Hub client and teaming errors distinguish identity expiration from other errors.
- Verify exact margins/centering, aliases, read-only identity, configured widths,
  Unicode and narrow terminals, changing counts, reconnection, HTTP 401 from both
  reads and sends, draft preservation, and absence of connected/shortcut text.
- Update Unreleased through `too aide.too update_changelog`; run default checks.

The online fraction intentionally excludes humans from its numerator, as agreed.
Stale presence and clipping identity errors are the main risks. No open questions.
