# Text status bar

Status: implementation requested on 2026-10-08 and revised in chat on 2026-10-09.
Online counts include agents only; the denominator includes all members, as confirmed.

## Goal and scope

Show conversation and presence on the left, and the viewer's login name or current
connection warning on the right. Inset both ends by two terminal cells, aligned
with the input text. Keep the existing placeholder and key bindings.

## Design

- A direct conversation with the viewer shows the other participant as `@alice`.
  An observer sees both participant names in stable order, such as `alice,bob`.
  Custom/system groups show `#name(online/total)`, for example `#dev(2/3)` for
  `gc_dev`; strip a leading `gc_` from group display names. These are display
  labels only; canonical conversation IDs remain unchanged.
- Style each online participant name (including `@`) in ANSI green. For groups,
  only the positive online count is green. Offline names, zero/unknown counts,
  group names, commas, parentheses, and total counts use the default foreground.
  Footer text is not dimmed.
- The right side shows the login name only, including for read-only observers;
  omit `from`, role prefixes, and read-only suffixes. Replace it with `Connecting…`,
  `Reconnecting…`, `Stopped`, or `Reopen Text` while that state applies. A Hub
  HTTP 401 means the existing identity/token is no longer accepted; Text cannot
  switch identity in place. Preserve the explanatory notice and draft.
- Sending/sent acknowledgments do not replace the login name. Preserve actionable
  send/draft errors on the right and existing notices. Omit `Connected` and key hints.
- Refresh membership and online-agent identities through existing Hub directory
  data every ten seconds and after reconnection. Count only online agents in this
  group and every member in the denominator. Before the first refresh or while
  disconnected, show `?/N` for groups and no green participant names.
- Truncate Unicode by terminal cells while preserving per-name styles. On narrow
  terminals, prioritize the right-side identity or warning, then shorten or omit
  the left side. Reduce margins only below five cells. Never wrap or overflow.
- No human presence tracking, new endpoints, conversation renames, or changes to
  message dividers, input editing, transport retries, or persisted messages.

## Touchpoints and acceptance

- Text CLI passes resolved conversation metadata into its TUI. A Text-owned status
  renderer owns labels and row geometry; the TUI caches directory data off-render.
- Hub client and teaming errors distinguish identity expiration from other errors.
- Verify exact margins, all three conversation formats, independent ANSI-green
  participant styles, zero/unknown counts, configured widths, Unicode truncation,
  membership/presence updates, reconnection, HTTP 401 on reads and sends, draft
  preservation, and absence of connected/shortcut/from/read-only text in the footer.
- Update Unreleased through `too aide.too update_changelog`; run default checks.

Stale green presence and clipped identity warnings are the main risks. No open questions.
