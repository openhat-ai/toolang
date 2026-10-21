# Current and recent activity

`too top` includes every materialized resident home under the selected Toolang
root, whether running or stopped, plus visiting/roaming identities in the current
validated `.runtime/instances/` registry. `too alice top` selects only that
resident home and its optional runtime binding. Hub keeps its runtime-only
inventory. Observation starts no runtime, creates no stores and acquires no lease.

Discovery refreshes every five seconds. Resident departure removes its live
binding while preserving its home, usage and completions. Visiting/roaming
departure removes the complete source from summaries and table immediately on
successful rediscovery; there is no session history retention or alternate source
catalog. Removing an unregistered resident home also removes its source.
Identical homes, registrations and data produce the same scope after reopening.
Discovery failures retain unknown coverage rather than proving departure.
Registration launch IDs fence reads and probes: obsolete live results and errors
cannot republish a departed or replaced launch. A current unreadable registration
remains diagnostic. Queued jobs for removed sources are canceled; running jobs
receive cancellation and cannot restore removed results.

Instances confirmed `not running` contribute zero active roots without live
warnings, including while runtime management retains resource cleanup references. Embedded instances without an endpoint can prove idle from a pinned
source with no unfinished physical roots. Otherwise live coverage stays unknown;
shutdown never implies a Run's terminal status. Genuine failures of a current
endpoint or history store remain visible.

```sh
too top
too alice top --period 4h --recent 15m
too top --once
too top --json
```

`--period` selects `1h` (default), `4h` or `8h` usage. `--recent` selects `1m`,
`5m` (default) or `15m` completed physical roots. `--refresh` sets a finite,
positive polling interval in seconds (default `1`). JSON and non-TTY output take
one snapshot. JSON includes per-source observations and a scope-wide `summary`;
incomplete totals are null rather than partial values presented as complete.

The summary order is `agents:`, `usage:`, `runs:`, followed by the table.
Their left-hand names have normal intensity. Only the right-aligned time captions
are dim: `right now`, `last <Period>`, and `last <Recent>`. The agents line shows
resident, visiting and roaming scope counts. It does not show running/total
counts. JSON retains `summary.running` for confirmed running instances and
`summary.total` for the complete roster, including stopped residents.

Usage includes model/tool calls, input/cached/output tokens and USD spend within
Period. Runs shows active, succeeded, failed and canceled root counts; the
aggregate completed total remains in JSON and paging, without a separate text
field. All summaries and table use the same scope. Runs and table share Recent:
active roots of any age plus roots completed within Recent. Child Runs do not
inflate root counts. Loading pages, folding or selecting rows changes no totals.

The table combines active trees and recently completed roots. Row consumption is
lifetime consumption; a parent Run includes descendants, while a Step shows only
its own invocation facts. Large numeric values use compact units to keep columns
aligned; USD spend uses four decimal places without an approximation marker.
JSON retains source amounts and approximation metadata. Period and folds do not
change these values. THREAD appears between AGENT and RUN and shows the full
owning root's thread ID on every row, or `-` when unavailable. RUN always shows
the full owning physical root ID. Terminal Runs show
only their state. AGENT shows the existing instance name
(for example, `aid@r18e03d00e6223810692a4880f56a5894`).

Interactive Top fills the terminal: a green table heading, deep cyan selected
row, and one cyan function-key bar anchored at the bottom. Fn numbers use
white on black; action labels use black on cyan. Labels compact at narrow widths,
keeping F10 visible; all bindings remain available. There is no separate status
line or error text in the footer. The title and clock keep normal intensity and
weight. The summaries have two cells of left and right padding, with dim captions
on the right. Only nonzero numeric values use cyan; zeros, unknown values, field
names and punctuation retain normal foreground. Summaries wrap inside their
padded columns, omitting captions when the terminal is too narrow. Active roots
and their entire visible subtrees use normal text; finished roots use dim text
across the whole row, including when selected. Active roots have a leading `+` whether expanded or
collapsed. Child rows retain tree branches, and finished roots show only their
state. The physical cursor stays hidden.

Interactive OSC titles are `top` or `AGENT top`, using the existing
`terminal.title.prefix.top` and `TOOLANG_TITLE_PREFIX_TOP` settings. Top publishes
its pane identity through the same terminal session and clears both publishers
on exit. Snapshot and JSON output do not publish titles or pane identity.

Top reads persisted facts through `toolang.stats.recent` and current paths through
LiveTree. `up.scope.ScopeReader` composes resident homes and runtime registrations
and owns membership failure/recovery policy. `up.observation` validates runtime
identity and launch generations. Top explicitly requests read-only inventory
and process probes, leaving registration cleanup and status repair to runtime
management callers. `up.activity.ActivityReader` owns endpoint,
history and paging reads, sharing established-history knowledge across workers
and resident rebindings. `up.top.TopObserver` schedules bounded independent jobs
and reconciles scope; `up.schemas` derives coverage and totals without I/O, and
the CLI presenter formats snapshots. The recent reader opens only `runs.db`,
read-only, without historical receipts, `stats.db` or an archival worker. For UTC second T, usage uses `[T-W,T)`
and root completions use `[T-R,T]`. Terminal model/tool invocation facts belong to
Step start; model tokens and cost belong to Step completion. Unfinished calls
appear in live rows immediately and enter window counters after settlement.
Unknown usage/pricing remains unknown. Failed model calls retain errors and any
reported usage; HTTP status never implies zero consumption. Known invocation
counts can coexist with unknown tokens or spend. Offline readable stores still
contribute durable usage and completions; disconnected live paths freeze and
are marked stale.
An inactive agent whose history store has not been created contributes zero
completions and usage without creating a database. A previously observed store
that disappears, or an existing unreadable/corrupt store, remains unavailable.

Loading, window switches, samples older than five seconds, normal paging and
missing provider usage/pricing do not produce error notices. Embedded execution
without an HTTP endpoint may have unknown live coverage without an error.
Only actual observation failures produce compact yellow notices between the
scope and clock; raw source errors remain in JSON diagnostics. Metric sentences
have no `partial`, `updating` or `unavailable` suffixes. Paging errors preserve
known totals/rows and remain in JSON without a global notice.

The read-only agent adapter exposes:

- `GET /api/v0/top/snapshot?recent=300` (OpenAPI group `activity`): live trees and the first 100 completed
  roots, with terminal counts over the whole interval. Hosting pins a source
  snapshot and captures LiveTree under the executor publication gate, then releases
  the gate before scanning. Page with both `before_time` and `after_id`, using the
  previous page's last `(finished_at,id)`; `completed_at` preserves the initial
  page's UTC-second upper bound. Ordering is completion descending, then ID ascending.
- `GET /api/v0/stats/recent?period=3600`: bounded source usage, independent of
  historical Stats initialization. The local observer uses this same Stats reader
  directly. Usage workers are separate from inventory and live polling; obsolete
  window responses are discarded. Source read failures return HTTP 503.

Completed-root paging uses separate workers so slow pages cannot occupy live
polling capacity, and keeps its original completion bounds. Local observation
resolves previously live root IDs through the source Stats reader when their
completion falls outside the first page, without advancing the page cursor or
loading results/errors. A failed supplemental
lookup preserves the fresh HTTP snapshot and its totals, records `page_error`,
and can be retried through normal PgDn paging. Window changes keep
eligible terminal facts and selection while the new totals load; a late response
from an older window cannot delay the selected window's next query.

Up/Down selects; Left folds or selects the parent; Right expands or selects the
first child. PgUp/PgDn pages the table and PgDn loads more completed roots.
F5 switches Tree/List while retaining Tree folds. Default ordering puts the most
recently changed active trees first, followed by finished roots in completion
order; clock ticks and folds do not change that order. F6 opens the sort picker
for Default, MODEL, TOOL, INPUT, CACHED, OUTPUT, SPEND and TIME+. Up/Down chooses
a field, Left/Right chooses ascending/descending, F6/Enter applies, and Esc
cancels. Click a table heading to sort descending, then click again to reverse;
a fixed cell before each sortable heading shows `▵` for ascending or `▿` for
descending, keeping field names and data columns stationary. Sorting uses root
lifetime values within each partition, with unknowns last. Subtrees retain execution order, selection
follows its identity, and finished roots always stay below active roots. Only
loaded roots participate; paging inserts additional completions in sort order.
Click any visible row to select it.

F7 cycles Recent and F8 cycles
Period; their footer labels show the next setting. F1 opens Help. F3/Enter toggles
the Details placeholder and Esc closes it. Details content remains deferred.
F10, q and Ctrl-C exit; the footer displays function keys only.
