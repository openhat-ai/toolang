# Talk group member presence and shortcuts

Status: approved by the human on 2026-10-12 for a PR containing this plan and its
implementation.

## Goal and scope

Replace the interactive Talk GC footer's total count, such as `#all(4)`, with
online/total members, such as `#all(3/4)`. Apply to every GC, including observers.
Support the Talk shortcuts below alongside canonical conversation IDs. DM labels,
terminal titles, the directory, membership permissions, and message delivery
retain their existing behavior.

## Target shortcuts

| Target | Resolution |
| --- | --- |
| `alice` | The current human's DM with agent `alice`; other bare agent names follow the same rule. |
| `all` | The Hub's system GC, identified by its stored system mapping. |
| `alice,bob` | An existing DM between the two named agents, independent of name order; the human observes it. |
| `dm_...` or `gc_...` | The existing conversation with that canonical ID. |

- Shortcuts remain valid on every invocation. After resolution, use only the
  canonical ID for the interactive view, message sends, drafts, and pane reuse.
- The agent-pair syntax is `alice,bob`, as in `too talk alice,bob`. Shell quoting
  is outside Toolang's target grammar; literal quote characters in the received
  target are invalid.
- A missing human-agent DM stays pending until its first accepted send. Opening
  it without sending creates nothing. Missing agent-agent DMs fail; observers
  cannot send. Preserve the existing literal message-body parsing.
- `all` is a reserved CLI shortcut and continues to select the system GC after
  its display name changes or another conversation is named `all`. An agent
  named `all` is accessed through its canonical DM ID. Other conversation names
  are not positional shortcuts.
- Single targets starting with `dm_` or `gc_` must be valid canonical IDs.
  Malformed or unknown IDs fail without falling back to agent/name lookup or
  creating data. Reject malformed pairs and unknown agents using the existing
  validation rules.
  A comma-separated pair always selects agents, including names with these
  prefixes, in either order.
- Translate `all` explicitly at the Talk call site via the existing Hub resolve
  endpoint with a new `kind="system"` lookup for `target="all"`. Hub resolves
  its existing backend system mapping and returns normal `Resolution` metadata.
  Reject other targets for this lookup kind. Default API/tool resolution keeps
  its existing bare-agent behavior; `all` is not a global naming heuristic.

## Decisions

- `m` counts all current conversation participants, including humans.
- Every human participant always counts toward `n`, regardless of open clients.
  This is a display convention, not tracked human presence.
- An agent counts toward `n` only while its Hub lease is live. Use the existing
  team snapshot and deadline semantics; process existence does not prove online
  status. A missing agent in a successful snapshot counts as offline.
- Intersect presence with this GC's participants; exclude other team members.
  An empty GC shows `(0/0)`; a human-only GC always shows `(m/m)`.
- For a GC containing agents, show `(?/m)` before the first successful presence
  snapshot and after a failed presence refresh. Resume the numeric count on
  recovery. Presence lookup failures must not stop message following or sends.
- Refresh membership and presence while the view is open. Reuse the team-change
  feed for conversation and presence notifications, with a fallback refresh
  interval of at most one second. Recompute expired leases even without a new
  message. Keep the conversation-only subscription behavior for existing callers
  unless they opt into presence changes.
- Preserve the footer's styling, width limits, truncation, centered ID, and
  existing connection/error presentation from the
  [Talk interface contract](talk-status-bar.md). This plan supersedes that
  contract's exclusion of presence and total-only GC count.

## Implementation touchpoints

- `src/toolang/cli/toolang/commands/talk/status.py`: format the GC ratio from a
  concrete online count or unknown state.
- `src/toolang/cli/toolang/commands/talk/tui.py`: own presence refresh state and
  combine it with current membership without coupling lookup errors to delivery.
- `src/toolang/teaming/client.py`: opt-in presence notifications through the
  existing subscription; reuse `team()` and its lease deadline semantics.
- `src/toolang/cli/toolang/commands/talk/__init__.py`: resolve CLI shortcuts,
  enforce canonical-prefix validation, and retain the resolved ID for pending DMs.
- `src/toolang/teaming/schemas.py`, `src/toolang/teaming/messaging.py`: accept and
  resolve the explicit system lookup through the existing messaging router.
- `src/toolang/teaming/backend/protocol.py`: expose the existing backend system
  mapping lookup in the backend contract.
- `src/toolang/cli/toolang/main.py`: document `too talk all` in command help.
- `tests/unit/cli/test_talk_status.py`, `tests/unit/cli/test_talk_layout.py`, and
  `tests/unit/messaging/test_latest_observation.py`: acceptance coverage.
- `tests/unit/cli/test_talk.py`, `tests/unit/messaging/test_hub_api.py`, and
  `tests/unit/messaging/test_protocol.py`: shortcut and lookup coverage.
- Update the existing Talk interface contract, `docs/messaging.md`, and README
  examples to the approved behavior; generate the implementation's Unreleased
  entry through `aid.too`.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Mixed group | One human and three agents, two online, show `#all(3/4)`. |
| Human convention | Opening or closing human clients never changes the count. |
| Lease lifecycle | Agent start, stop, and silent lease expiry update the numerator within one fallback interval after the Hub reflects the change. |
| Membership | Joining/leaving updates both counts; unrelated agents have no effect. |
| Empty and human-only groups | Render `(0/0)` and `(m/m)` without requiring agent presence. |
| Lookup failure and recovery | Mixed groups show `(?/m)` while unknown, resume numeric counts after recovery, and continue receiving/sending messages. |
| Existing presentation | DM labels, observer permissions, terminal titles, Unicode widths, narrow layouts, and error rows remain correct. |
| Subscription compatibility | Existing conversation-only callers ignore presence; opted-in Talk GC subscriptions react to presence changes. |
| Agent shortcut | `too talk alice` opens/reuses the human-agent DM; first send creates it once, while closing a pending view creates nothing. |
| System shortcut | `too talk all` opens the same system GC before and after renaming it, including when another GC or an agent is named `all`. |
| Agent pair | Both name orders resolve the same existing DM; literal quotes, missing, duplicate, or malformed pairs fail, and the human cannot send. |
| Canonical targets | Valid DM/GC IDs open the exact conversation; malformed/unknown IDs fail without fallback or writes. |
| Reuse and sends | A shortcut and its ID share drafts and tmux placement; one-shot messages use the resolved ID and preserve literal text. |
| API compatibility | Default bare `all` still selects an agent through API/tools; explicit system lookup selects the system GC and rejects other targets. |

Use deterministic clocks and offline transports for tests. Run the repository's
default verification for implementation.

## Risks and open questions

The human convention can overstate actual availability; no human heartbeat is
introduced. Snapshot refresh adds Hub requests and must remain bounded and avoid
duplicate concurrent lookups. Reserving CLI `all` and canonical prefixes changes
selection for agents with those names; use their canonical DM IDs. No open design
questions remain.
