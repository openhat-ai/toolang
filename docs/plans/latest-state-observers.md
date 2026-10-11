# Latest-state observers

Approved in chat on 2026-10-11, including implementation and a PR, after the
snapshot/subscription experiments recorded in issue #753.

## Contract

Team Top subscribes to execution and member events. Talk subscribes only to member
events. These are refresh notifications: open the subscription before loading a
current snapshot, coalesce pending refreshes, and load current state again after
reconnection. Do not persist cursors or replay missed UI transitions.

Top reads root-scoped membership independently of activity. Each agent refreshes
its existing activity snapshot independently; execution notifications and a
one-second clock tick request refreshes. Member notifications refresh the roster
and presence promptly. A five-second roster refresh also covers discovery
metadata. Unknown connectivity and unavailable activity remain separate.

Talk uses relevant conversation-member notifications to wake its existing read
loop. Keep message cursors and metadata polling: message delivery and renames are
not included in member events. Preserve draft and permission handling.

## Scope and touchpoints

- `teaming`: a small reconnecting notification adapter, Hub observation, a cheap
  root-scoped roster snapshot, and optional single-agent activity reads.
- CLI: select the Hub observer for team Top and wake Talk on member changes.
- Preserve local/offline Top, accounting, filters, existing HTTP endpoints,
  backend event protocols, lease timing, and filesystem discovery.
- Keep unsupported database schema repair (#752) separate. A failed activity
  request must still allow the observer to display authoritative presence.

## Acceptance

- Top opens both event subscriptions; Talk opens only the member subscription.
- Initial attachment, changes during a read, and fresh reconnect converge on
  current snapshots without replay or an unbounded queue.
- Slow/failing activity cannot delay presence or healthy agents; removed and
  foreign-root agents are excluded.
- Event-triggered reads bypass stale aggregate caches; clock ticks keep rolling
  windows current without execution events.
- Talk membership changes wake idle reads and preserve drafts, permissions,
  message cursors, and rename handling; unrelated events do not wake it.
- Cancellation closes subscriptions and pending reads. Run default verification.

## Risks and open questions

Fresh execution subscriptions include active trees, not all completed history;
activity snapshots remain the accounting/history authority. Coalescing limits
duplicate reads during bursts. No open product decisions remain.
