# Current and recent activity

`too top` observes the selected root's validated local resident, visiting and
roaming inventory. `too alice top` selects one resident agent. Observation neither
starts agents nor acquires server leases. Discovery refreshes every five seconds;
known source paths remain available for eight hours after inventory removal.

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
its own invocation facts. Period and folds do not change these values. RUN always
shows the full owning physical root ID. Terminal Runs show only their state.

Top reads persisted facts through `toolang.stats.recent` and current paths through
LiveTree. The recent reader opens only `runs.db`, read-only, without historical
receipts, `stats.db` or an archival worker. For UTC second T, usage uses `[T-W,T)`
and root completions use `[T-R,T]`. Terminal model/tool invocation facts belong to
Step start; model tokens and cost belong to Step completion. Unfinished calls
appear in live rows immediately and enter window counters after settlement.
Unknown usage/pricing remains unknown. Offline readable stores still contribute
durable usage and completions; disconnected live paths freeze and are marked stale.

The read-only agent adapter exposes:

- `GET /api/v1/top/snapshot?recent=300`: live trees and the first 100 completed
  roots, with terminal counts over the whole interval. Hosting pins a source
  snapshot and captures LiveTree under the executor publication gate, then releases
  the gate before scanning. Page with both `before_time` and `after_id`, using the
  previous page's last `(finished_at,id)`; `completed_at` preserves the initial
  page's UTC-second upper bound. Ordering is completion descending, then ID ascending.
- `GET /api/v1/stats/recent?period=3600`: bounded source usage, independent of
  historical Stats initialization. The local observer uses this same Stats reader
  directly. Usage workers are separate from inventory and live polling; obsolete
  window responses are discarded. Source read failures return HTTP 503.

Up/Down selects; Left folds or selects the parent; Right expands or selects the
first child. PgUp/PgDn pages the table and PgDn loads more completed roots.
F5 switches Tree/List while retaining Tree folds. F7 cycles Recent and F8 cycles
Period; their footer labels show the next setting. F1 opens Help. F3/Enter toggles
the Details placeholder and Esc closes it. Details content remains deferred.
F10, q and Ctrl-C exit; the footer displays function keys only.
