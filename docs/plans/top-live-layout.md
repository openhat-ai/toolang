# Top dashboard

Dashboard layout and observation contract.
[Top activity](top-activity.md) owns row semantics, statistics and controls.
The presence/roster and recovery contracts below remain unchanged.

## Goal and scope

Make `too top` and `too alice top` full-screen dashboards with the density and
interaction of htop: aligned consumption, visible work and responsive selection.
Success means equivalent statistics through local and Hub observation, accurate
rolling windows, and readable details without terminal scrollback. Execution
controls, authentication, generated summaries and new retention policies are
outside scope.

## Regions and header

Use the alternate screen and restore the terminal on exit. The four regions are
**Header**, **Table**, **Details** and **Status bar**. Header, Table and Status bar
are always present; optional Details sits above the fixed bottom Status bar and
reduces the Table viewport. Scrolling belongs to a region, never terminal scrollback.
`--once` and non-TTY output remain single snapshots.

Header has six lines at every width: identity/clock, blank, two compact statistics
rows, settings, blank before Table. Keep the local clock at the upper right;
identity and clock use normal weight. Put Period/Recent settings directly below
statistics at the left:

```text
Team 1/2 online                                                                    19:08:51

Threads: 1     Runs:   1    Models: 24   Tools: 46
In:      128k  Cached: 96k  Out:    12k  Spend: $1.28
Period: session  Recent: 30m

AGENT S MODEL TOOL   IN CACHED OUT SPEND  TIME+ THREAD RUN ACTIVITY(30m)↓
alice +    24   46 128k    96k 12k $1.28 12m30s t1     r1  review_project
bob   -     -    -    -      -   -     -      - -      -   -

                                                                (remaining Table viewport)
F1Help F4Filter F5Tree F6Sort F7Recent F8Period F10Quit
```

Append active filters to the settings line. Both modes retain the AGENT column.
Single-agent mode replaces the first-line team summary:

```text
Agent uptime 5m43s
```

- `Team` means Hub scope; `Agent` means the agent named in Table. Do not repeat
  agent names in statistics labels or ACTIVITY.
- Team shows online/total agents. Agent uptime is wall time since the current
  executor session started, independent of Period and TIME+. Tick while online
  and fresh; show `-` for offline, stale, reconnecting or missing session data.
  Do not repeat accumulated execution time in the first line.
- Header metrics cover the whole observation scope, independent of view,
  selection, filters and folds. Threads and Runs count Activity-eligible work
  before filters, including completed Runs; show `0` when empty and `-` when
  unavailable. Active/failed counts and `idle` belong in Table ACTIVITY.
- Statistics groups share four compact column starts; align labels and values
  within each column. Use subdued labels and emphasized values. Keep both rows
  on one physical line each; crop at the right edge without ellipses or reflow.
- Settings update immediately after input. Show active filters compactly in
  Header only when set. Sort belongs on the sorted column heading; level/layout
  is apparent from columns. Do not display refresh
  frequency or repeat settings as prose above Table.
- Status bar uses htop-style function-key cells: normal key numbers followed by
  colored action labels. Show only Fn hints; retain other shortcuts in Help.
  F5 names the next view: Agents, Threads, Runs or Tree. F7 Recent and F8 Period
  match Header labels and cycle activity visibility and statistics ranges.
  On narrow terminals, omit whole key cells as needed and retain F10 Quit.
  Keep the filter input tail and cursor visible while editing long values.
  Keep `Connecting`, `Updating` and `Reconnecting` feedback; omit `Incomplete`.
  Coverage/freshness details belong in Details. The wall clock keeps an idle
  dashboard visibly current without inventing execution activity.

## Table and Details

Use one width definition for headings and rows: numeric values/headings align
right, ownership and ACTIVITY align left. Selection and heading fills span the
viewport. Keep one physical line per row, hard-clipped at display-cell boundaries
with **no ellipsis**, including wide/combining characters. Preserve column order
at every width; crop the right edge without hiding columns or horizontal scrolling.
Details preserves full values. No grid boxes or wrapped Table rows.
[Views](top-activity.md#views) defines columns and branches.

Details follows the selected object, using short dashboard labels:

```text
Run: r1  Thread: t1  Status: running
Inspect: too alice inspect r1
Stats: Models 16  Tools 30  In 88000  Cached 64000  Out 8800  Spend $0.88  Time+ 8m00s
Total: Models 20  Tools 35  In 96000  Cached 70000  Out 9600  Spend $0.96  Time+ 9m00s
Result:
(rendered Markdown)
```

- Show applicable full IDs, runnable/model/tool summary, selected-range `Stats`,
  lifetime `Total`, exact tokens and inspect command. Agent/Thread details show
  counts, not task summaries. Add matches/coverage only when relevant.
- Render results with existing
  [`TerminalMarkdown`](../../src/toolang/cli/common/markdown.py) and shared terminal
  colors. Render the document at panel width before paging rendered lines,
  preserving lists and fenced code across pages. Cache rendered lines by result
  revision and panel width, not per repaint. Reuse the theme; results may wrap.
- Enter toggles Details; Esc closes it. Up/Down and Ctrl-P/Ctrl-N change Table
  selection even with Details open. Mouse wheel also moves Table selection.
  Capture wheel events while on the alternate screen and restore mouse reporting
  on exit; do not clear the terminal's existing shell history. Changed selection
  resets Details scroll; PgUp/PgDn scrolls Details when open, otherwise Table.
- Cap Details at half the available content height, leaving Table headings and
  a selected row visible when height permits. Status bar never scrolls.
- Load full results asynchronously only while Details is open, on selected
  reference, execution-status change or source recovery. Cancel obsolete loads
  and reject stale responses. Use `Result: Loading` / `Result: Unavailable` as
  applicable. No token-delta feed or full output in compact activity frames.

## Data ownership

```text
top agent -> teaming observation service -> execution reader -> runs.db
top team  -> Hub HTTP                    -> teaming observation service
                                                  -> local execution reader
                                                  -> existing agent HTTP / cached coverage
```

- Team mode requires a running Hub. Local agent mode uses the reusable service
  directly, requires neither Hub nor Redis/Valkey, and can inspect offline history.
  Observation never starts an executor.
- `teaming` owns observation, source selection and cross-agent aggregation.
  `execution` owns schemas, accounting, SQL and the shared statistics reader.
  HTTP exposes the service; top owns presentation/interaction. Do not duplicate
  queries or aggregation in CLI/router modules.
- Resolve layout, presence and source paths at hosting/CLI boundaries; pass
  concrete values into the service. Prefer accessible local `runs.db`; retain
  existing agent HTTP/cached coverage for sources without a local store. This
  adds no remote-agent modes.
- Opening top is read-only: no database creation/migration, executor lease or
  Hub backend initialization on the local path. Missing stores/session metadata
  yield unavailable values. Local history remains readable offline; cache-only
  sources retain observation time/coverage rather than claiming fresh windows.
- Direct and HTTP observation reuse committed facts/aggregates through the same
  reader. Presence is independent of history availability. Preserve the existing
  [HTTP recovery protocol](../api.md#team-activity).

## Delivery and redraw

Execution and source clock/window changes asynchronously replace complete local
projections; scheduled paints use the latest data. Structural changes publish
promptly; source clock updates occur once per second. Window expiry updates
values/eligibility even without execution events. The CLI clock drives Header's
clock and live session uptime; it never extrapolates accumulated execution time.

`--refresh SECONDS` remains finite and positive, default `0.1`. Coalesce data
updates at render deadlines; navigation, setting changes and resize repaint
immediately. One renderer owns terminal writes. Slow queries/result requests
cannot block input or accumulate paints. Stale execution time stays frozen.

On query changes, show requested settings immediately and mark affected values
as updating until replacement completes; never label old values with a new window.
Swap Header/Table values together at each source checkpoint; ignore obsolete query
responses. Keep independent per-agent updates, so one slow source cannot block
others. Explicit roster frames control removal; partial updates never remove
omitted agents. Reconnect takes an atomic baseline before suffix updates.
Navigation/layout/sort alone reuse the subscription.

## Presence and roster

Retain existing defaults:

- Accepted reports under the process lease renew presence. Independent heartbeats
  run every 5 seconds; leases expire after 15. Backlog cannot block heartbeats;
  graceful stop releases the lease. Hub receipt time is `last_seen`, separate
  from observation time. Heartbeats do not extend Activity.
- Hub scans its root at startup and every 5 seconds with the same valid resident
  discovery as `too list`. Discovery owns existence, leases own presence and
  coverage owns freshness. Failed scans preserve the previous roster.
- Two successful absent scans and no live lease confirm deletion. A live agent
  with a missing directory stays visible with a diagnostic in Details. Clean up
  only this root's managed entries, never transient/unscoped registrations.
- Confirmed deletion removes the roster/participant and ordinary group membership.
  Preserve DM mappings/memberships, messages and history. Offline alone never
  removes an agent; recreated names retain history and DM identity.
- Keep default-query publication, shutdown drain and default/last-query cached
  snapshots. Do not replace discovery, leases or event recovery in this work.

## Touchpoints and acceptance

Likely changes: `cli/common/activity.py`, `activity_view.py`,
`cli/toolang/commands/top.py`; a focused observation-service module in `teaming`,
`teaming/activity.py`, `activity_feed.py`; `execution/activity.py`, query schemas
and agent/Hub HTTP adapters. Reuse `cli/common/markdown.py` and existing accounting;
do not move execution persistence into teaming or rewrite executor lifecycle.

| Scenario | Pass condition |
| --- | --- |
| Team, Agent alice and Agent team; all levels | Unambiguous scope, scoped Header totals, and Table present in single-agent Agent level. |
| Wide/narrow/short terminals, Unicode, resize, exit | Aligned columns, no ellipses, visible Status bar and restored terminal state. |
| Details and rapid selection during slow HTTP | Styled Markdown pages correctly; Ctrl-P/N follows selection; obsolete responses cannot replace output. |
| Long refresh interval, SSE bursts and idle clock | Input repaints immediately; data paints coalesce; clock/window updates continue without events. |
| Hub stopped, offline history, missing DB | Team requires Hub; local works without it; observation never creates stores or starts services. |
| Same query through local reader and Hub | Metrics agree at the same observation boundary, with one accounting implementation. |
| Query switch, slow agent and reconnect | No mislabeled ranges or invented presence; complete per-agent Header/Table replacements. |

Risks: narrow-screen pressure, Markdown rendering cost and stale/offline coverage.
Bound viewports and async work; expose unavailable data. Rolling-window acceptance
belongs to [Top activity](top-activity.md#acceptance). No open product questions remain.
