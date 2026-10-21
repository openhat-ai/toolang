# Execution statistics

Each agent server exposes read-only usage statistics:

`STATS_API_VERSION = 0` owns these queries and the
[`recent` activity query](top.md), independently of the agent API.
`STATS_STORE_SCHEMA = 2` owns `stats.db`; incompatible databases are rejected
without rewriting them.

| Endpoint | Input | Result |
| --- | --- | --- |
| `GET /api/v0/stats/summary` | Optional `model_ref` | All-time, 365d, 30d, 7d, 24h, 4h, 1h, and 30m windows |
| `GET /api/v0/stats/models` | `window`, default `1mo` | One window grouped by historical model ref |
| `GET /api/v0/stats/daily` | `start_date`, exclusive `end_date`, IANA `timezone`; optional `model_ref` | Daily rows suitable for a contribution heatmap |

All endpoints accept either `thread_id` or `root_run_id`; omitting both selects
the agent. A root filter includes owned child Runs. Thread statistics use physical
record ownership; forked or imported conversation history does not copy usage.
Unknown scopes return 404; invalid scopes, child IDs used as roots, and invalid
ranges return 422. Daily ranges contain 1-366 dates through today, including empty
dates. Today's row is partial; local dates respect timezone and DST boundaries.

The seven quantities are `in`, `out`, `cached`, `spend`, `model_calls`,
`tool_calls`, and `root_runs`. Cached tokens are already included in input.
Spend sums recorded USD amounts; `spend_approximate` preserves the recorded cost
classification. Unknown quantities stay null and known zero stays zero. Model
rows have `root_runs=null`, since a root start has no model attribution.

Root counts belong to root start, including still-running roots. Invocation counts
belong to Step start and become queryable once a terminal Step records actual
invocation. Tokens and spend belong to Step completion. A call crossing a window
or date boundary can therefore contribute its count and usage to different rows.
Finite windows include their start and exclude their end; `1y` and `1mo` mean
365 and 30 days.

Responses contain `scope`, `snapshot`, and `data`. The first historical request
returns `data=null` while a background worker initializes the cache; retry the
request to obtain results. Recent-only requests can read directly during this
initialization. Snapshot metadata reports query/cache times, initialization,
pending refresh work, and a bounded refresh error. After initialization, pending
archival does not omit usage: queries merge cached and unarchived roots.
An unavailable statistics store returns 503 without stopping execution.

The independent module writes schema 2 of `AGENT_HOME/.runtime/stats.db`, with
`roots`, `buckets`, and `state` tables. An older cache can be discarded while
the server is stopped and rebuilt from compatible execution records. Only completed root
trees older than the configured waiting period are archived. Requests wake one
lazy background thread; it does not poll, modify execution records, or retain an
automatic server. Publication and the root's archive receipt commit atomically.
Already archived roots are excluded before Step collection; delayed completion
does not depend on a time cursor.

Configure the waiting period in the agent home's `config.toml`, then restart the
server. The default is one day; the minimum is 60 seconds:

```toml
[stats]
archive_after_seconds = 86400
```

Keep the original `runs.db` records: exact partial-minute boundaries still read
their fields. Replacing the source or changing archived records requires rebuilding
the derived statistics database.

Imported replay history is excluded through source ownership views before recent,
completion, and archival projection. Only newly owned facts count for a destination;
repeated replay preserves original ownership and never duplicates usage.
