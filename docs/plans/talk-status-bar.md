# Talk status bar

Status: implementation requested on 2026-10-08 and revised in chat on 2026-10-09.
Online counts include agents only; the denominator includes all members, as confirmed.

## Goal and scope

Show conversation and presence on the left, and the viewer's login name or current
connection warning on the right. Inset both ends by two terminal cells, aligned
with the input text. Rename the messaging command from `text` to `talk`, with
`write a message` as the input placeholder. Keep existing key bindings.

## Design

- Expose `too talk [TARGET] [MESSAGE...]` with the existing flags, literal-message
  parsing, and human identity. Remove `too text` without an alias; do not add
  prefix-agent syntax. Rename the command package and UI notices to Talk, retaining
  `.runtime/text/` and tmux identity marks so drafts, history, and windows survive.
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
  `Reconnecting…`, `Stopped`, or `Reopen Talk` while that state applies. A Hub
  HTTP 409 with code `hub_changed` means the backend or human identity no longer
  matches; Talk cannot switch identity in place. Preserve the explanatory notice
  and draft.
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

- Talk CLI passes resolved conversation metadata into its TUI. A Talk-owned status
  renderer owns labels and row geometry; the TUI caches directory data off-render.
- Hub client and teaming errors distinguish identity changes from other errors.
- Verify Talk directory/send/interactive routing, literal flags in messages, the
  absence of a Text alias, and restored drafts/history from the existing namespace.
- Verify exact margins, all three conversation formats, independent ANSI-green
  participant styles, zero/unknown counts, configured widths, Unicode truncation,
  membership/presence updates, reconnection, `hub_changed` on reads and sends, draft
  preservation, and absence of connected/shortcut/from/read-only text in the footer.
- Update Unreleased through `too aide.too update_changelog`; run default checks.

Stale green presence and clipped identity warnings are the main risks. No open questions.
