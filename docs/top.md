# Current and recent activity

`too top` observes the selected root's validated local resident, visiting and
roaming inventory. `too alice top` selects one resident agent. Observation neither
starts agents nor acquires server leases. Discovery refreshes every five seconds;
known source paths remain in memory for eight hours after their last discovery.
Each new Top session starts from the resident list and current validated instance
registrations. It does not recover previously deregistered visiting/roaming
instances from an earlier session or persist an additional source registry.
These retained sources contribute historical completions and usage only; their
departure stops polling the old live endpoint and does not make current
active-root coverage unavailable. Queued live requests are canceled; running
requests may supply durable completions but cannot restore an obsolete live
sample. Rediscovery updates the existing source binding without counting its
history twice. Instances confirmed `not running` contribute zero active roots
without live diagnostics; their durable completions and usage remain visible.
Running or unknown instances retain the normal unavailable/stale live diagnostics.
Instance departure or shutdown never implies a Run's terminal status.

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

The header counts inventory placements, current active roots and recent terminal
statuses, then model/tool calls, input/cached/output tokens and USD spend.
The table combines active trees and recently completed roots. Row consumption is
lifetime consumption; a parent Run includes descendants, while a Step shows only
its own invocation facts. Large numeric values use compact units to keep columns
aligned; USD spend uses four decimal places without an approximation marker.
JSON retains source amounts and approximation metadata. Period and folds do not
change these values. RUN always shows the full owning physical root ID. Terminal Runs show
only their state. AGENT and source diagnostics show the existing instance name
(for example, `aid@r70200d1f`) without the appended `#runtime_instance` ID.

Interactive Top fills the terminal: a green table heading, deep cyan selected
row, and cyan status/function-key bars anchored at the bottom. Fn numbers use
white on black; action labels use black on cyan. The title, clock and summary
fields have normal intensity and weight. Active roots and their entire visible
subtrees use normal text; finished roots use dim text across the whole row,
including when selected. Active roots have a leading `+` whether expanded or
collapsed. Child rows retain tree branches, and finished roots show only their
state. The physical cursor stays hidden.

Interactive OSC titles are `top team` or `top AGENT`, using the existing
`terminal.title.prefix.top` and `TOOLANG_TITLE_PREFIX_TOP` settings. Top publishes
its pane identity through the same terminal session and clears both publishers
on exit. Snapshot and JSON output do not publish titles or pane identity.

Top reads persisted facts through `toolang.stats.recent` and current paths through
LiveTree. The recent reader opens only `runs.db`, read-only, without historical
receipts, `stats.db` or an archival worker. For UTC second T, usage uses `[T-W,T)`
and root completions use `[T-R,T]`. Terminal model/tool invocation facts belong to
Step start; model tokens and cost belong to Step completion. Unfinished calls
appear in live rows immediately and enter window counters after settlement.
Unknown usage/pricing remains unknown. Offline readable stores still contribute
durable usage and completions; disconnected live paths freeze and are marked stale.

The read-only agent adapter exposes:

- `GET /api/v0/top/snapshot?recent=300`: live trees and the first 100 completed
  roots, with terminal counts over the whole interval. Hosting pins a source
  snapshot and captures LiveTree under the executor publication gate, then releases
  the gate before scanning. Page with both `before_time` and `after_id`, using the
  previous page's last `(finished_at,id)`; `completed_at` preserves the initial
  page's UTC-second upper bound. Ordering is completion descending, then ID ascending.
- `GET /api/v0/stats/recent?period=3600`: bounded source usage, independent of
  historical Stats initialization. The local observer uses this same Stats reader
  directly. Usage workers are separate from inventory and live polling; obsolete
  window responses are discarded. Source read failures return HTTP 503.

Completed-root paging runs separately from live polling and keeps its original
completion bounds. Local observation resolves previously live root IDs through
the source Stats reader when their completion falls outside the first page,
without advancing the page cursor or loading results/errors. Window changes keep
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
an arrow marks the chosen direction. Sorting uses root lifetime values within
each partition, with unknowns last. Subtrees retain execution order, selection
follows its identity, and finished roots always stay below active roots. Only
loaded roots participate; paging inserts additional completions in sort order.
Click any visible row to select it.

F7 cycles Recent and F8 cycles
Period; their footer labels show the next setting. F1 opens Help. F3/Enter toggles
the Details placeholder and Esc closes it. Details content remains deferred.
F10, q and Ctrl-C exit; the footer displays function keys only.
