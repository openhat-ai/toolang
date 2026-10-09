# Top live updates and layout

Feature definition for review; no implementation in this change. Extends
[Top activity](top-activity.md), replacing its header/footer, COST presentation and
fixed redraw rules. Existing views, ownership, recovery and Stats/Recent semantics
remain the contract.

## Goal and scope

Keep the Hub's local agent roster current and make top readable and responsive.
Success means consistent directory/presence state, independently updated agents,
aligned rows with token consumption, and immediate local interaction even while
HTTP is slow. Includes both Hub and single-agent top. Authentication, execution
controls and new model-generated summaries are outside scope.

## Presence and roster

- Reuse the current lease: accepted current-instance reports renew presence;
  otherwise send a heartbeat every 5 seconds, expiring after 15 seconds. Event
  backlog cannot block heartbeats. Graceful stop releases the lease immediately.
  Hub receipt time is `last_seen`; snapshot observation time remains separate.
  Heartbeats neither create canonical execution events nor extend Recent activity.
- Hub scans its root at startup and every 5 seconds, using the same valid resident
  agent discovery as `too list`. Inject the resolved root/discovery at the hosting
  boundary. Discovery owns existence; the lease owns online/offline; observation
  coverage owns freshness. A newly discovered, unregistered agent is offline
  with unavailable activity, not zero consumption.
- Two successful scans confirming absence and no live lease confirm deletion.
  A live agent whose directory disappeared remains visible with `home missing`.
  Failed scans preserve the previous roster. Only this root's managed entries are
  eligible; never apply directory absence to another root's or transient agents.
- Confirmed deletion removes the current roster entry, memberships and delivery
  eligibility. Retain its private event/activity cache and consumer state for
  24 hours, then reclaim them. Preserve shared conversation messages. Offline
  alone never triggers deletion. Cleanup is restart-safe and idempotent.
- Fence removal and cleanup by agent incarnation. Same-name recreation uses a
  new incarnation without old memberships, cursors or live projections. Late old
  reports and delayed cleanup cannot revive or erase the new incarnation. Existing
  unscoped registrations must be reconciled conservatively before cleanup.

## Delivery and redraw

```text
committed activity / presence / elapsed-time changes
  -> source projection -> HTTP SSE -> asynchronously replace local data
  -> scheduled render at the configured interval
keyboard / local interaction -> update local state -> immediate render
```

- SSE carries elapsed-time changes as well as execution, usage and presence
  changes. Time-only source updates are emitted once per second while needed;
  structural changes publish promptly with bounded coalescing. The CLI does not
  independently extrapolate execution time. Stale observations stay frozen.
- `--refresh SECONDS` sets the finite positive screen refresh interval, default
  `0.1`. Data reception never redraws directly. At each deadline render the latest
  complete local state if dirty, dropping intermediate frames, not data. `--once`
  still prints one complete snapshot and ignores refresh scheduling.
- Navigation, editing, view/layout changes and resize repaint immediately from
  cached state. Coalesce a queued key batch into one repaint. One renderer owns
  terminal writes; an immediate repaint satisfies a coincident scheduled repaint.
  Local navigation neither waits for nor restarts a subscription. Query changes
  preserve the last view until the replacement snapshot completes.
- Hub updates agents independently and reuses shared per-query projections;
  one slow source cannot hold all agents behind a batch barrier. Roster changes
  and each agent's complete replacement have explicit atomic boundaries, so an
  omitted agent in a partial update does not mean removal. Reconnect obtains an
  atomic baseline before suffix updates. Reuse persisted totals; do not aggregate
  raw events or full historical records on each refresh/subscriber.

## Three-region terminal layout

1. Header: team totals for `too top`, or the selected agent's totals for
   `too AGENT top`, independent of selected rows and table view. Show presence,
   root-run/thread counts and consumption, with a compact context line for
   view/layout, Stats (session or explicit start), Recent, sort and refresh interval.
   Single-agent mode identifies the agent here and omits AGENT in the table.
   Only show coverage/filter/loading diagnostics when relevant. New source data
   updates this summary and/or the body on the next scheduled frame; do not wait
   for a separate multi-second publication cycle. Live TIME updates provide
   visible progress even during one long model/tool call. Idle activity is not
   animated or invented; a real last-received timestamp indicates source freshness.
2. Body: full-width table header and selected row; remaining height belongs to
   the row viewport. Keep one physical line per row. Tree branches indent only
   ACTIVITY and retain the existing run/step parentage.
3. Footer: fixed bottom key bar. Details/help open immediately above it, reducing
   the body viewport. Full IDs, result text and inspect commands live in Details;
   remove the persistent duplicate selected-identity/command line.

```text
AGENT MODEL TOOL     IN CACHED    OUT   SPEND    TIME THREAD RUN STEP ACTIVITY
alice    24   46 128.4k  96.0k  12.8k   $1.28  12m30s t1     r1  r1   review_project
```

Use one column-width definition for header and all rows. Numeric fields and their
headers align right; identity and ACTIVITY align left. Widths include placeholders,
coverage markers, TIME*, Unicode display-cell widths and separators; values changing digit
count must not shift unrelated columns. Format tokens with compact decimal k/M
units and exact values in Details. Clip only summaries with an ellipsis; preserve
complete inspect references through horizontal scrolling. Retain the existing
narrow-screen rule of hiding MODEL/TOOL first. Header/selection fills span the
viewport, even with few rows. No wrapping or grid boxes in the body.

ACTIVITY uses existing runnable/model/tool summaries or a short plain-text result.
Do not flatten a whole Markdown reply into a pseudo-summary. Detailed output stays
in Details; use the runnable name when no concise result is available. No extra
model calls. Preserve failure and offline cues without repeated normal states.

## Consumption

- Column order is `MODEL TOOL IN CACHED OUT SPEND TIME`. CACHED is input cache-read
  tokens, included in IN; cache writes are not cache hits. OUT uses the existing
  normalized output total. Counts, tokens and spend use inclusive ownership;
  TIME retains the existing duration semantics. All metrics use the selected
  Stats range; header totals count each agent once, independent of visible rows.
  Recent controls visibility only.
- Add token facts and aggregates beside existing attempt usage and buckets in the
  records transaction. Final accounting settles tokens and spend at the same
  boundary; duplicates, retries, rewind and fork follow existing consumption
  rules. Known partial sums retain coverage markers; unknown is not zero.
  Resume a versioned backfill from available durable accounting; missing/deleted
  historical attempts remain explicitly incomplete.
- Display and sort use SPEND/spend; accept `--sort cost` as a compatibility alias.
  Show known SPEND as the amount alone, e.g. `$1.28`, without `~` or `+`; unknown
  remains `-`. Preserve estimate/coverage metadata internally and in Details,
  with incomplete coverage also covered by the header diagnostics. Keep existing
  cost fields/contracts internally. Additive token fields retain unknown coverage
  when reading older snapshots; do not default missing historic usage to zero.

## Touchpoints and acceptance

Likely owners: `up/process.py` and `up/hub.py` for discovery/hosting;
`teaming/` for roster, lease, cleanup and delivery; `work/teaming.py` for agent
reporting; `execution/statistics.py`, `activity.py`, `schemas.py` and accounting
migration for tokens; `api/routers/activity.py`, `cli/common/activity.py`,
`activity_view.py` and `cli/toolang/commands/top.py` for SSE and presentation.

| Scenario | Pass condition |
| --- | --- |
| Idle, busy, crash and graceful stop | Online lease stays fresh without execution events; offline transition follows release/expiry; no invented Recent activity. |
| Create/delete, failed scans, Hub restart and same-name recreation | Roster converges; cleanup respects the grace period, root and incarnation; shared messages survive; old reports cannot resurrect data. |
| Slow source, reconnect and partial multi-agent delivery | Other agents continue updating; no partial state or accidental roster removal. |
| SSE/time bursts and configured refresh | Data updates asynchronously; scheduled frames use latest complete state; stale time freezes; no backlog of paints. |
| Keys during a deliberately long refresh interval/slow HTTP | Selection, filter editing and expansion repaint immediately without an unnecessary reconnect. |
| All views, List/Tree, single agent, resize and Unicode | Shared column boundaries, full-width selection, fixed footer, readable clipped activity and intact inspect references. |
| Token ranges, retry, replay, migration and missing usage | Local/Hub totals agree; CACHED is not added to IN; no double counting or fabricated zeros. |

Risks: destructive cleanup must identify root and incarnation correctly; old token
coverage may be unrecoverable; independent Hub updates must preserve atomic
recovery. No open user-facing layout decisions remain in this draft.
