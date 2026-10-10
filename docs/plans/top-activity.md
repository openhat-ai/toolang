# Top activity

View, statistics and accounting contract for the
[dashboard](top-live-layout.md).

## Terms and scope

| Term | Meaning |
| --- | --- |
| Level | Agent, Thread or Run; determines row objects and their statistics. |
| Layout | List or Tree at Run level. Tree adds steps and child runs. |
| Stats | Common consumption range; default `session`, configured by `--since`. |
| Activity | Independent rolling eligibility range; default `30m`, configured by `--recent`. |
| Active | Unfinished execution: pending/running or last known unfinished work with stale observation. |
| Running path | Confirmed running execution and its required ancestors. |
| RUN | Owning top-level run ID, repeated down its tree. |
| STEP | Full row reference: `r1`, `r1.1`, child run `r3`, its step `r3.2`. |

In the UI, Run means a run without a parent step; use **child run** when needed.
Agent/Thread are ownership, never tree nodes. Existing execution states remain
`pending / running / succeeded / failed / canceled`. Presence
(`online / offline / unknown`) is separate from freshness/execution state.
`idle` means no active work with complete counts.

## Views

Default to Agent for `too top`, Thread for `too alice top`; Run starts in List.
Columns share the prefix `AGENT S MODEL TOOL IN CACHED OUT SPEND TIME+`.
Single-agent mode omits AGENT and retains a Table row at Agent level.

| Level / layout | Remaining columns | Row and ACTIVITY |
| --- | --- | --- |
| Agent | ACTIVITY | Agent counts: `1 active · 4 failed`, or `idle`. |
| Thread | THREAD, ACTIVITY | Thread counts using the same rule. |
| Run / List | THREAD, RUN, ACTIVITY | One run's current work or terminal result. |
| Run / Tree | THREAD, RUN, STEP, ACTIVITY | Run plus steps/child runs on running paths. |

`S` is agent presence on every row: `+` online, `-` offline, `?` unknown;
include the legend in help. Offline ACTIVITY is `-` at every level and has no
expanded paths; historical metrics remain available. Unknown/stale observation
uses `-` for unfinished ACTIVITY and preserves known terminal results. Coverage/
reconnect feedback belongs in Status bar/Details, not repeated in each cell.
Hub disconnection makes presence unknown, not offline; Header must not present
the last online count as current.

Agent/Thread counts use Activity-eligible runs before filters; show nonzero active
and failed counts. Failed means a failed top-level run, not a step/child run.
Agent/Thread never show task summaries; incomplete counts cannot assert `idle`.
Roster agents stay visible unless filtered. In team Thread/Run levels, agents
without eligible work have a branchless placeholder with inapplicable fields
`-`; single-agent mode instead shows an empty Table. Actual Agent rows show
known metrics even without work.

Each Tree row's metrics belong to its own run/step; RUN expresses ownership only.
Example IDs are shortened; actual values retain full stored references:

```text
AGENT S MODEL TOOL  IN CACHED  OUT SPEND TIME+ THREAD RUN STEP ACTIVITY(30m)↓
alice +    16   30 88k    64k 8.8k $0.88 8m00s t1     r1  r1   review_project
alice +    16   29 88k    64k 8.8k $0.88 7m59s t1     r1  r1.1 └─ map: files · 2/8 completed
alice +     2    3 11k     8k 1.1k $0.11   40s t1     r1  r3      └─ review_file
alice +     1    0  4k     3k  400 $0.04   32s t1     r1  r3.2       └─ model-name · Analyze configuration
alice +     4    8 22k    16k 2.2k $0.22 2m00s t1     r2  r2   succeeded · Configuration updated
```

Branches appear only in ACTIVITY. Preserve recorded run/step parentage and step
nesting, including parallel flow. Only confirmed running runs expand; pending,
stale and terminal runs stay single rows. An async launch step that finishes
before its child remains as a required ancestor with its terminal status;
completed siblings disappear. Follow new paths except at manual folds; `[+]`
means hidden running paths. A finished selected node maps to its nearest visible
ancestor; run completion atomically replaces the tree with one result row.

Tree runs show runnable names, flow steps operation/progress, and model/tool steps
their names/summaries; ancestors do not repeat descendant summaries. List shows a
current-call summary; multiple calls use a count and representative summary in
execution order. Use `pending`, `succeeded`, `failed` or `canceled` inline only
when applicable, without a separate execution-state column.

Use concise plain-text results/errors, falling back to the runnable name for long
or Markdown output. Full output belongs in Details. Strip `agent::` from runnable
display names: `agent::agic:name` becomes `agic:name`, without changing identity
or inspect targets. Preserve existing model previews and explicit tool summaries;
no additional model calls. Color reinforces text rather than replacing it.

## Stats and Activity

All consumption metrics share one Stats range and the row's ownership:

- MODEL/TOOL/IN/CACHED/OUT/SPEND include directly and transitively owned calls.
- TIME+ is a run/step's own duration; Thread/Agent sums top-level run durations.
  Never sum child durations or visible Tree rows into parents/Header totals.
  Parallel runs can make accumulated time exceed elapsed wall time.
- Header counts each agent once, independent of visible rows. Details exposes
  selected-range `Stats` and lifetime `Total`, including at Agent level.

| Stats value | Boundary at observation time T |
| --- | --- |
| `session` | Owning executor's current session start through T; when stopped, its latest recorded session. |
| Duration, e.g. `1h`, `1d`, `1w` | Rolling interval from T minus duration through T; a day is 24 hours. |
| Timezone-aware timestamp | Fixed start through T, preserved across executor restarts. |
| `all` | All available history through T. |

Durations stay relative in query identity and are evaluated by the source at each
observation. Do not resolve them once at CLI startup or create a new subscription/
cache entry per tick. Existing `--since DURATION` changes from fixed-start to
rolling; use an explicit timestamp to retain fixed-start behavior. Missing session
metadata remains unknown. TIME+ labels every range; Header's Stats setting
explains the common range without per-column markers.

Calls count at step begin, tokens/spend settle at step end, and execution duration
is clipped to Stats. A crossing call can contribute tokens/spend without a new-call
count. Retries retain consumption and exclude waiting between attempts; rewind
never refunds it and forked references never duplicate it. CACHED is cache-read
input already included in IN; OUT is normalized output. SPEND shows an amount
(`$1.28`), or `-` when unknown. Provenance/incomplete coverage goes in Details
and relevant Status bar feedback. Unsettled usage is not fabricated as zero.
Format tokens in compact decimal k/M/G units; Details retains exact values.

At each observation use exact timestamp boundaries: indexed facts for partial
buckets and complete buckets for the interior. Expire counts, settled usage and
interval portions as they leave a rolling range, even without record changes.
Shared projections account for clock/window changes as well as revisions.

```text
At 11:10: a run lasted 10:50-11:10, spending $0.50; $0.20 settled at 11:05.
Stats since 11:00: Spend $0.20, Time+ 10m.
Stats 1h at 12:06: Spend $0.00, Time+ 4m (the 11:06-11:10 overlap).
```

Activity retains active runs plus runs/threads changed within its rolling range;
keep recently changed empty threads at Thread level. Descendant execution, retry
and thread create/fork/rewind advance activity; heartbeat/render/replay do not.
Use persisted timestamps on reopen. Activity/filters/folds never alter a retained
object's Stats; Stats never alters eligibility.

Readable sources recompute windows from durable data. Unavailable sources retain
explicitly stale coverage; never claim cached totals as a fresh rolling calculation
or relabel another cached range. Frozen execution time must not accrue downtime.
Pagination exposes loaded/matched/eligible counts only when useful in Status bar/
Details, without inflating scope totals.

## Filtering, sorting and controls

Keep two AND-combined filters: `--filter TEXT` is a case-insensitive literal
substring across IDs, names, states and summaries, including historical descendants;
`--active` retains unfinished work and removes idle placeholders.
Matches retain their owning run and running paths. Agent/thread matches include
their runs or the matching empty object. Highlight/open matching running paths
once; later manual folds prevail. Historical matches stay inspectable in Details
without reopening completed paths. Query compact metadata within Activity and
report coverage. Clearing text clears only that filter.

Sort by `activity` (active first, then latest change), `spend` or `time`
(descending full-precision values, unknown last). `cost` remains an alias for
`spend`. Sort objects across agents, never tree descendants. Placeholders follow
real rows; stable IDs break ties. Preserve selected identity as rows move; clock
ticks alone do not change activity order. Show a downward arrow on the sorted
ACTIVITY, SPEND or TIME+ heading; ACTIVITY includes its range: `ACTIVITY(30m)↓`.

| Control | Action / CLI |
| --- | --- |
| a / t / e | Agent / Thread / Run; preserve `--view agent\|thread\|execution` and the existing e shortcut. |
| F4 | Edit text/Active filters; retain Ctrl-A toggling Active in the editor. |
| F5 | Cycle Agent / Thread / Run / Tree; preserve `--view execution --tree`. |
| F6 | Cycle activity / spend / time. |
| F7 | Cycle Activity: `5m / 30m / 1h / 1d / 1w / all`. |
| F8 | Cycle Stats: `session / 1h / 1d / 1w / all`. |
| Left / Right | Collapse/expand paths; Left on a leaf/folded row selects its visible parent. |
| Up / Down, Ctrl-P / Ctrl-N | Select previous/next row; Details follows. |
| PgUp / PgDn | Page Details when open, otherwise Table. |
| < / > | Scroll Table horizontally. |
| Enter / Esc | Toggle / close Details. |
| F1 | Help, including presence legend and custom CLI window values. |
| F10 / q / Ctrl-C | Exit without affecting execution. |

F7/F8 apply the next preset immediately without an editor/confirmation step; from
a custom value, next press selects the first preset. Custom values remain available
through `--recent` and `--since`. Keep `--refresh` and snapshots as defined in
[delivery and redraw](top-live-layout.md#delivery-and-redraw).
Level shortcuts are disabled during text editing. Only Fn shortcuts are shown in
Status bar; a/t/e and other shortcuts remain available through Help.
Switching level preserves settings, filters and folds, maps selection by ownership,
and never adds an implicit filter. List selects the owning run; returning to Tree
restores an eligible selected descendant. Display terminology changes do not
remove existing CLI flags or accepted values.

## Persistence and reuse

Retain execution's committed accounting contract:

```text
executor -> transaction(records + attempt usage + aggregates + cursor) -> commit
         -> shared statistics/activity reader -> observation service -> top
         -> canonical stream -> execution subscribers / Hub event export
```

Execution owns sessions/checkpoints, attempt facts, inclusive aggregates, time
intervals and the activity index. Genuine begin/end contributes once; retry uses
a new attempt identity. Token settlement/spend share the records transaction,
including early completion. Preserve rollback safety, resumable legacy backfill
and incomplete coverage for deleted/missing attempts. Observation never migrates
stores. Close interrupted intervals at the durable checkpoint without inventing
provider charges or downtime.

Use existing minute buckets plus boundary facts and clipped intervals; no repeated
full-history scans, per-viewer aggregation or copied output in usage facts. Writes
remain proportional to affected ownership paths. Share projections and publish
absolute revisions/values; consumers replace idempotently. Keep reads off the
execution path and preserve [local subscriptions](local-subscriptions.md) and
[Hub recovery](team-observation.md). Agent export remains Hub HTTP, without direct
backend access. Service boundaries and local/offline source selection belong to
[data ownership](top-live-layout.md#data-ownership).

## Acceptance

Likely files: `execution/activity.py`, `schemas.py`, statistics query helpers;
`teaming` observation/HTTP adapters; `cli/common/activity.py`, `activity_view.py`
and `cli/toolang/commands/top.py`. Reuse persistence unless a demonstrated query
requirement needs an additive change. Do not redesign executor/accounting tables
as part of presentation work.

| Scenario | Pass condition |
| --- | --- |
| Sequential/parallel flow and async launch | Correct IDs, ownership and row metrics; only running paths/required ancestors expand. |
| Levels, empty agents, offline/unknown presence | Correct columns/counts; offline ACTIVITY is `-`; no false idle/running or fabricated metrics. |
| Fixed/session/all/rolling Stats and independent Activity | Exact boundaries and idle expiry; visibility never changes totals; session resets only on executor restart. |
| Crossing calls, overlapping runs, retry/rewind/fork | Correct Stats/Total without nested, duplicate or lost consumption. |
| Filters, sorting, folds and completed matches | Stable selection, inspectable historical matches and preserved parentage. |
| Rapid F7/F8 changes, delayed responses and reconnect | Immediate settings, no obsolete replacements, consistent Header/Table data. |
| Multiple observers and large history | Shared bounded projections and indexed boundaries; no per-tick full-history scans. |

Use deterministic clocks for boundary tests and compare local/Hub results at the
same observation boundary. Risks are incomplete legacy usage and rolling-window
cache invalidation; require explicit coverage and time-advancing tests.
