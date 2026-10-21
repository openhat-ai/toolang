# Current and recent activity

`too top` discovers only active references in the selected Toolang root's
`.runtime/instances/`. `too alice top` selects that resident's exact instance name;
other placements with the same agent name are excluded. Installed/stopped homes
are not scanned. Observation starts no runtime and acquires no server lease.
Discovery refreshes every five seconds; already observed sources remain in memory
for eight hours to supply historical windows. A new session starts from current
registrations only and creates no second source registry.
These retained sources contribute historical completions and usage only; their
departure stops polling the old live endpoint and does not make current
active-root coverage unavailable. Queued live requests are canceled; running
requests may supply durable completions but cannot restore an obsolete live
sample. Rediscovery updates the existing source binding without counting its
history twice. Instances confirmed `not running` contribute zero active roots
without live diagnostics; their durable completions and usage remain visible.
Live diagnostics distinguish failed published endpoints from pending or
unsupported observation.
For an embedded instance without an HTTP endpoint, a pinned local source with
no pending or running physical roots proves zero active roots. Unfinished roots
of any age keep live coverage unknown; failed endpoints and unreadable stores
also remain unknown.
Instance departure or shutdown never implies a Run's terminal status.
Discovery and live reads verify the registration's launch ID before publishing
results. Removal or replacement during a read discards the old observation,
including its failures, without an error notice. A still-registered instance
with unreadable or missing state remains unavailable. A departed instance whose
history store was never created contributes zero; losing an established store
remains an error.

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

The agents summary shows `N running, M total: R resident, V visiting, O roaming`.
These are agent-instance counts, independent of root Runs: a running idle agent
still counts as running, and multiple roots do not add agents. Installed stopped
residents remain in total; retained departed sources contribute history only.
Known preparing/starting instances are not yet running. Unknown liveness leaves
running unknown while complete membership still supplies total and placements;
unrelated driver metadata does not invalidate readable statistics.

The other summaries count current active roots and recent terminal statuses,
then model/tool calls, input/cached/output tokens and USD spend.
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
LiveTree. `up.observation` owns registration identity and generation checks;
`up.activity.ActivityReader` owns endpoint, history and paging reads, sharing
established-history state across a source's workers and rebindings.
`up.top.TopObserver` owns polling, retention, obsolete results and snapshot
projection. The recent reader opens only `runs.db`, read-only, without historical
receipts, `stats.db` or an archival worker. For UTC second T, usage uses `[T-W,T)`
and root completions use `[T-R,T]`. Terminal model/tool invocation facts belong to
Step start; model tokens and cost belong to Step completion. Unfinished calls
appear in live rows immediately and enter window counters after settlement.
Unknown usage/pricing remains unknown. Offline readable stores still contribute
durable usage and completions; disconnected live paths freeze and are marked stale.
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
