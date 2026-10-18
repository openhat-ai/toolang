# Execution period statistics

Status: approved for implementation on 2026-10-18. Definition only.
Goal: exact agent/thread/root statistics with incremental historical aggregation
and no executor coupling. No open product decisions.
The executable [SQL catalog](execution-period-statistics.sql) is part of this
definition; statement names below refer to that file.

## Scope and invariants

- Independent `toolang.stats` starts with the agent server. Its lazy worker
  writes only `runtime/stats.db`; executor code has no statistics calls, queues,
  triggers, or subscriptions. Source readers open `runs.db` read-only.
- An archive candidate is a physical root (`parent IS NULL`) in a terminal status
  with `finished_at < pass_time - archive_after_seconds`. Default is 86400 seconds;
  agent-home `[stats]` accepts integers >=60. Changes affect future candidates.
- Its owned Runs and Steps must all be terminal before root completion, and
  remain immutable. Current normal execution drains owned async children first;
  verify startup, failure, and cancellation paths too. Invalid trees stay on the
  source side; report the error and continue other roots. Independent spawn is
  another root. Statistics never changes execution state.
- `stats_roots` receipts define the split: `result = stats(R) + source(roots - R)`.
  A waiting-time cutoff never defines the query split. Old Steps in a running or
  not-yet-archived root remain on the source side.
- Keep original records, including archived trees: exact partial-minute queries
  still read their original fields. Source replacement/deletion or mutation of
  an archived tree requires rebuilding this derived database.
- Uses the current [terminal invocation/usage/cost fields](executor-live-tree.md)
  in source schema 59. SQL linkage checks exercise fixture projections of this
  contract; implementation must use the source-owned codecs.

## Metric projection shared by both paths

| Public field | Persisted fact | Timestamp |
| --- | --- | --- |
| `in` | Model `noted.usage.input_tokens` | Step completion |
| `out` | Model `noted.usage.output_tokens` | Step completion |
| `cached` | Model `noted.usage.cache_read_tokens`, already included in `in` | Step completion |
| `spend` | Selected model `noted.cost.amount` in USD | Step completion |
| `model_calls` | One per terminal model Step with `invoked=true` | Step start |
| `tool_calls` | One per terminal tool Step with `invoked=true` | Step start |
| `root_runs` | One per root with `started_at`, including running/no-call roots | Root start |

Skipped/synthetic/pre-entry-canceled Steps contribute zero. Child Runs add no
root count; same-root continuations count once. Run Noted is a verification
reference, never an additional contribution. Logical fork/rewind/adoption views
do not copy consumption. Thread attribution uses each owning Run's physical Thread.

One pure `project` function uses source-owned codecs and produces timestamped
metric vectors: root count at root start; invocation count at Step start; model
tokens/cost at Step completion. It is used unchanged for archival and raw queries.
Filter each projected vector by its own timestamp, even when the Step was selected
by the other timestamp. Missing invocation facts are not inferred from Step kind.

Empty quantities are zero. Unknown usage/cache/USD cost is null, and null absorbs
addition for that quantity. Preserve known zero; do not use ordinary SQL `SUM`
to discard unknown contributors. `spend_approximate` accompanies the seven
quantities and combines with OR. Use existing `selected_usd_cost` and
`selected_cost_is_approximate`; normalize with `cost_units`, sum integer micro-USD,
and convert once for the response; no pricing recalculation or currency conversion.
Model refs are historical full refs. Empty ref is the
non-model leaf for tools/root counts; model-only results return `tool_calls=0`,
`root_runs=null` because a root start has no model attribution.

## Interfaces

Common optional `thread_id` and `root_run_id` are mutually exclusive; neither
means this server's agent. Unknown scope is 404; malformed/conflicting parameters
or a child used as a root are 422. Empty scopes return zero metrics.

| GET path | Additional input | Data |
| --- | --- | --- |
| `/api/v1/stats/summary` | Optional `model_ref` | Eight window results |
| `/api/v1/stats/models` | `window`, default `1mo` | One window grouped by model ref |
| `/api/v1/stats/daily` | `start_date`, exclusive `end_date`, IANA `timezone`; optional `model_ref` | Local-date rows |

Windows: `all`, `1y=365d`, `1mo=30d`, `1w=7d`, `1d=24h`, `4h`, `1h`, `30m`.
Capture UTC second T once. Finite ranges are `[T-W,T)`; all-time has no time
predicate and null bounds. Return sorted model refs, omitting neutral-only rows.
Daily accepts 1-366 dates through today, converts local boundaries to UTC, caps
today at T, and fills empty dates. Resolve ambiguous/skipped midnight to the
earliest instant on or after it; entirely skipped dates have empty intervals.

Responses have `scope`, `snapshot`, and `data`. Snapshot contains `queried_at`,
`computed_at`, `initializing`, `stale`, `refreshing`, and a bounded `refresh_error`.
Each window/model/date row contains the seven metrics plus `spend_approximate`;
window rows echo name/bounds, daily rows echo date/bounds and partial-today status.
During initial archival, historical requests return 200 with `data=null`;
recent-only requests may return merged results. After initialization all pending
roots are read from source, so `stale` describes cache work, not missing usage.
An unavailable statistics store returns 503 without stopping execution.

## Storage and connections

`stats_schema` defines only receipts, model/non-model leaf buckets, and one state
row. Buckets have agent/thread/root scopes and all/minute/hour/day resolutions;
agent scope ID is empty. UTC bucket starts use integer floor; all-time starts at 0.
Each projected vector has at most 12 keys, coalesced across its root. Unknown-only
vectors are retained; only the all-known-zero vector can be omitted.

`source_indexes` belongs to the normal source Store migration. It includes root
inventory, root eligibility, parent lookup, all three timestamp indexes, and a
replacement `(run,id)` index for stable Step paging. No statistics reader runs
schema changes. `stats.db` has its own schema version and WAL mode.

| Connection | Databases | Operations |
| --- | --- | --- |
| Query/collection reader | main=`stats.db` RO; src=`runs.db` RO | SELECT/read transactions |
| Publisher | main=`stats.db` RW; no attached source | Archive transactions |

Bind parameters. Source time bounds use canonical UTC text; bucket bounds use
epoch integers. Never wrap indexed timestamp columns in conversion functions.

## Reading and merging: SQL sequence

1. `BEGIN`; execute `pin_stats`, then `pin_source`. The first SELECT pins R and
   its buckets; source snapshot is acquired afterward. Keep both through merge.
   A bare `BEGIN` alone is not a pinned snapshot. Execute statements individually;
   do not use `executescript` inside these transactions.
2. Read cached complete ranges with `bucket_totals`, grouped by model ref.
   For all-time use `(period='all',lo=0,hi=1)`. For a finite interval partition
   into disjoint full days, uncovered hours, uncovered minutes, then edges.
3. For archived edges, use `time_step_ids` -> `steps_by_ids` -> root resolution
   via `runs_by_ids`/StepRef parsing -> `archived_root_ids`. Keep only roots in R.
   Add `root_starts_by_time(archived=1)`. Project and filter each vector to its edge.
   `UNION` deduplicates Steps selected by both start and finish.
4. For raw all-time/long ranges, use `unarchived_roots`, then `owned_runs` and
   paged `source_step_page`. A root scope instead uses `root_by_id` and a receipt
   lookup. Add each selected root-start marker. Keep only roots outside R and
   filter each projection by scope/model/time; child Thread filters apply after
   ownership resolution, not to root seeds.
5. For recent-only queries, especially before initialization, use time-indexed
   Step selection instead of enumerating old trees. Keep roots outside R and add
   `root_starts_by_time(archived=0)`. Coalesce time ranges across windows/dates.
6. Merge by model key, then interval/date: nullable addition for token/money
   quantities, integer addition for counters, OR for approximation. Missing rows
   supply the zero vector. Divide micro-USD only after merging; format scope totals
   or model groups. Close the snapshots, then signal any needed refresh.

Archived edges are less than two minutes on each timestamp index per interval.
The raw side may contain more than one day because roots can remain active or
await archival. Read it once per request and share projections across windows.
Never add both all-time and finite-resolution buckets for one interval.

## Computing and publishing archives: SQL sequence

```text
inventory = archive_candidates(cutoff) in a short read snapshot; close it
for root in the captured inventory:
    if receipt_exists(root) in a fresh read: continue  # before reading Steps
    runs = owned_runs(root)
    require all runs terminal; require has_open_steps(run) is false for each run
    additions = group project(root start, source_step_page of each run)
                by (scope, period, UTC bucket start, model_ref)
    publisher: BEGIN IMMEDIATE
    if receipt_exists(root): ROLLBACK; continue
    execute upsert_bucket for each grouped key
    execute insert_receipt and touch_state
    COMMIT
after the finite pass: execute finish_pass in its own transaction
```

The root is the atomic unit; source pages of 500 are not separate publications.
Collection uses immutable source trees and releases reads between pages. For
large inventories/group maps use disposable temporary storage, not a durable
queue. Errors roll back the unpublished root; already published roots remain.
An invalid tree is skipped and reported. Stop during collection abandons that
root; stop during publication commits or rolls it back.

`BEGIN IMMEDIATE` serializes publishers before the receipt check. Receipt and
buckets always commit together. Never UPSERT first and merely ignore a duplicate
receipt afterward: that would count twice. A zero-call root still gets a receipt.
The candidate anti-join excludes archived roots before loading any Steps; the
fresh guard also skips receipts committed since inventory capture. One worker
processes coalesced requests serially: ordinary refreshes never recompute a
published root. Rollback or a crash before commit may require recomputation;
overlapping publishers can compute the same root but cannot publish it twice.

A root may run for weeks: while running, and during its post-completion waiting
period, its contributions stay on the source side. Once eligible, the next
anti-join finds it regardless of its old start/Step timestamps or another root's
completion order. Repeated refreshes then skip it. There is no permanent time
watermark or daily Step reconciliation.

## Lifecycle and verification

Server lifespan owns the independent service and exposes `app.state.stats`.
Startup opens the small schema only. A historical query wakes one lazy thread
when initialization or eligible unarchived roots exist. One Event coalesces
requests; first pass is immediate, starts are >=5 seconds apart. Each pass captures
finite work and then sleeps; no polling, source notification, or automatic-server
lifetime extension. Shutdown joins the worker before closing source storage.

Review both SQL and rules against these scenarios: direct-source equality for
all seven metrics; empty/unknown/known-zero values; separate start/completion
windows; scope/model/DST/local-date boundaries; long-running roots; failed/canceled
and independent roots; publication during queries; duplicate publishers; rollback
before receipt and retry after commit; delayed root discovery at the waiting
boundary; repeated refreshes and stale inventories without Step re-reads;
invalid closed trees.
Use `EXPLAIN QUERY PLAN` to check root-only enumeration, parent range lookups,
stable Step paging, timestamp reads, and scope/period/time bucket reads.

SQL review (2026-10-18): executed all 22 catalog statements against SQLite fixtures;
2,049 metric comparisons matched direct-source results. Repeated fresh/stale
inventories did not recollect published roots; a 40-day root archived once after
its waiting boundary, with unchanged windows. Fault/concurrency checks and the
listed index plans passed. This is a correctness check, not a production benchmark.
Performance scales with indexed root identities, unarchived tree size, model
cardinality, and huge roots; profile I/O and source-index write cost before shipping.

Likely changes: `stats/{config,types,schemas,records,service}.py`, server settings/
lifecycle, API injection/router, derived-path layout, generic Store indexes, and
focused tests/docs. No executor logic changes. Implementation must pass the
acceptance tests and default repository checks. This definition requires
executable SQL/link checks and `git diff --check`.
