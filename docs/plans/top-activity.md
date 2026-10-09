# Top activity

Feature definition; examples are proposed behavior, not current CLI usage.
Replaces the presentation and retention rules in [team observation](team-observation.md),
preserving its recovery contract. Implementation requires approval and resolution
of the open questions below.

## Goal and terms

One observer for `too top` (Hub) and `too alice top` (agent HTTP): identify current
work, compare consumption, follow running paths, and obtain exact IDs for inspect.
Success means equivalent local/Hub data, clear row statistics, and reliable
recovery without repeated history aggregation. Execution controls, authentication,
provider invoice reconciliation and additional summary-generating model calls
are outside scope.

| Term | Meaning |
| --- | --- |
| View | Agent, Thread or Execution; determines the row object and its statistics. |
| Layout | List or Tree, available in Execution view. Tree is not another view. |
| Stats | Statistics range, from the selected start to the current observation. Default: `session`; CLI: `--since`. |
| Recent | Rolling range for displaying recent work. Default: `30m`; CLI: `--recent`. |
| ACTIVITY | Execution overview: root-run counts in Agent/Thread, current work or result in Execution, with status markers when needed. |
| Active | Unfinished execution: pending, running, or last known unfinished with stale observation. Used by counts, retention and `--active`. |
| Running path | Path from a confirmed running root run through its steps and sub-runs to current execution. Tree retains these nodes and their required ancestors. |
| RUN | Root run ID, repeated on every row of that root run's tree. |
| STEP | The row's full run ID or step reference, including the root row: `r1`, `r1.1`, `r3`, `r3.2`. Placeholders use `-` in both columns. |

Use `agent`, `thread`, `root run`, `sub-run` and `step` consistently.
A root run has no parent step; a sub-run does. Execution List shows root runs;
Execution Tree also shows sub-runs and steps. Step references include their owning
run and step path, e.g. `run_r1.1`. Use existing execution
statuses everywhere: `pending / running / succeeded / failed / canceled`; no
`done` or `run` aliases. Presence (`online / offline / unknown`) and observation
(`syncing / stale`) are separate from execution status. `idle` means no active work.

## Views and presentation

Default: Agent for `too top`, Thread for `too alice top`; Execution initially uses List.
Fixed prefix: `AGENT MODEL TOOL COST TIME`. STATE is not a separate column.

| View / key | Row object | Remaining columns | ACTIVITY |
| --- | --- | --- | --- |
| Agent / a | Agent | ACTIVITY | Counts across the agent's root runs, with presence/observation information when needed. |
| Thread / t | Thread | THREAD, ACTIVITY | Counts across the thread's root runs. |
| Execution / e | Run or step | THREAD, RUN, STEP, ACTIVITY | List shows root run progress/results; Tree adds steps and sub-runs on running paths. |

Agent/Thread ACTIVITY shows nonzero `N active` and `N failed` root-run counts,
e.g. `1 active · 4 failed`; show `idle` when both are zero and coverage is complete.
Counts use Recent-eligible root runs before filters; failures are failed root runs,
not failed steps/sub-runs. Agent counts include all its threads. Thread counts
remain in the header/details. Task titles, call summaries and latest results appear
only in Execution view.

Each Tree row displays its own run/step statistics; repeated RUN denotes root
ownership, not the statistics object. STEP contains the complete record reference,
so nested steps remain unambiguous without tree indentation. Selection/inspect use
that reference directly. Example run IDs are shortened for readability; actual
values use the stored run IDs, retaining the owning run ID in every step reference.

```text
too top  View Execution  Layout Tree  Stats session  Recent 30m
Agents 2 online / 3   Threads 1 active / 2 shown   Root runs 1 active / 3 shown

AGENT MODEL TOOL   COST    TIME THREAD RUN STEP ACTIVITY
alice    16   30  $0.88   8m00s t1     r1  r1   review_project
alice    16   29  $0.88   7m59s t1     r1  r1.1 └─ map: files · 2/8 completed
alice     2    3  $0.11     40s t1     r1  r3      └─ review_file
alice     1    0      -     32s t1     r1  r3.2       └─ model-name · Analyze configuration
alice     4    8  $0.22   2m00s t1     r2  r2   succeeded · Configuration updated
alice     4    8  $0.18   2m30s t2     r4  r4   succeeded · Documentation updated
bob       -    -      -       - -      -   -    idle
carol     -    -      -       - -      -   -    offline · last seen 5m ago

Selected: agent alice / thread thread_t1 / root run run_r1 / run run_r3 / step run_r3.2
Inspect: too alice inspect run_r3.2
[a] Agent [t] Thread [e] Execution  F4 Filter  F5 Layout  F6 Sort  Enter Details  q Quit
```

Each root run starts an independent tree at depth 0. Agent/thread are ownership
columns, never tree nodes; branches appear only in ACTIVITY. Preserve recorded
run/step parentage and step nesting: flow steps, sub-runs and model/tool steps are
individually selectable, including parallel execution. Completed branches disappear;
terminal root runs become one result row with no expansion control. Pending/stale
root runs also stay single rows. Summarize pending sub-runs on their owning run.

Attach each sub-run to its recorded parent step. An async launch step can finish
while its sub-run continues: retain that step only as a required ancestor and mark
its actual terminal status inline. It does not reopen completed sibling branches.
Follow new running paths except at manual folds; `[+]` means hidden running paths.
When a selected node finishes, select its nearest visible ancestor. Root completion
atomically replaces its tree with the result row.

Use one ACTIVITY summary per row, without repeating agent names or normal
online/running markers. In Tree, runs show runnable names, flow steps show operation
and progress, and model/tool steps show their names and summaries; do not repeat
descendant call summaries on ancestors. List summarizes current execution on each
root row; with multiple current calls, show a count and representative summary in
execution order. Use these markers only when applicable:

| Situation | ACTIVITY example |
| --- | --- |
| Agent/thread root-run counts | `1 active`, `4 failed`, `1 active · 4 failed`, or `idle`. |
| Current model/tool call | `model-name · Analyze configuration` / `fs.read · Read configuration`. |
| Pending root run / sub-runs | `pending · review_project` / `review_project · 2 pending`. |
| Terminal root run | `succeeded · Configuration updated`, `failed · Permission denied`, `canceled · review_project`. |
| Incomplete observation | `syncing · counts incomplete` / `stale · review_project · last seen 5m ago`. |

Run results use the root run's own status and direct summary/error. Use existing
summaries, then runnable names as fallback. Failed root-run counts never imply a
failed agent/thread.
Color may reinforce text, never replace it. Shared reconnect/sync information
belongs in the header or agent summary. Preserve known terminal results; show
`syncing` if recovery has not reconciled their descendants. Hub disconnection
means unknown presence, not every agent offline. Freeze stale execution time at
the last observation and restore running paths only after recovery.

Known agents remain visible unless filtered out. In Thread/Execution views, agents
without eligible rows get a branchless placeholder with inapplicable metrics
(`-`). Single-agent mode connects directly without Hub: put identity, presence
and labeled Agent Stats in the header; omit AGENT and placeholders, retaining
object metrics in Thread/Execution tables. Its Agent view is the header alone. Keep
the header for empty/offline results; filtering Hub data to one agent does not
change the layout.

Keep one line per row, clipping summaries; details expose full thread/root run/run/
step IDs, summaries and a copyable inspect command. Narrow terminals hide
MODEL/TOOL first and allow horizontal scrolling for the remaining columns; no
boxed grid. Switching view preserves Stats, Recent, filters, scroll and folds,
mapping selection by ownership without adding an implicit filter. Disabling Tree
selects the root run; reenabling restores the eligible selected descendant.
Presentation changes reuse the subscription and never acquire an executor.

## Stats and Recent

Stats applies to the row object: agent, thread, run or step.
MODEL/TOOL/COST include that object's directly and transitively owned calls.
TIME is a run/step's own execution duration; thread/agent TIME sums root run
durations. Never add nested durations or visible Tree rows to their parents.
Parallel root runs can make TIME exceed executor Uptime. Header Stats counts
each agent once and is independent of row visibility.

All four metrics use the same Stats range:

- `--since session`: owning executor's current session start, not observer start
  or thread/run creation. Only executor restart resets it.
- `--since TIMESTAMP|DURATION`: explicit start; a duration such as `1w` resolves
  once to a fixed timestamp. Preserve it across executor restarts.
- `--since all`: all available history.

Keep TIME for session; use TIME* for any explicit override, including all.
The header explains the common start for every metric and displays a timestamp
with timezone, or all available history. Details label selected-range values
`Stats` and lifetime values `Total`. Missing session metadata remains unknown.

Calls count at step begin, cost settles at step end, and execution duration is
clipped to Stats. A call crossing the start may add cost without a new-call count.
Retries retain consumed calls, cost and execution time; waiting between attempts
is excluded. Rewind does not refund consumption; forked references do not duplicate it.
Known execution wholly outside Stats contributes zero; missing coverage is unknown.
Cost markers: estimated `~$1.28`, partial `$1.28+`, unknown `-`; an unsettled
call may have no cost yet.

```text
Run 10:50-11:10: Total $0.50 / 20m; $0.20 settled after 11:00.
Stats since 11:00: COST $0.20, TIME* 10m.
Stats all:         COST $0.50, TIME* 20m.
```

Recent selects all active root runs plus root runs/threads changed within its
rolling range. Keep recently changed empty threads in Thread view. Presets:
`5m / 30m / 1h / 1d / 1w / all`, default 30m; custom durations are accepted.
Descendant execution changes advance root/thread last activity; retry and thread
create/fork/rewind count, heartbeat/render/replay do not. Use persisted timestamps
on reopen. Running paths remain visible regardless of age.

Recent, filters and folds never change a retained object's Stats. Stats never
changes row eligibility; preserve selection if value sorting moves rows. Replace
new values, labels and any required recovery boundary atomically. Remove the
20-root limit: paginate root summaries and keep compact running paths, reporting
loaded/available counts and coverage rather than silently truncating. Offline
Hub data reports cached coverage; older history may require the source agent.

## Filtering, sorting and controls

Two filters, combined with AND:

| Option | Rule |
| --- | --- |
| `--filter TEXT` | Case-insensitive literal substring across IDs, names, execution status and ACTIVITY summaries, including historical descendants. |
| `--active` | Active work only; omit idle placeholders. Offline unfinished work remains eligible. |

A match selects its root run and preserves running paths. Agent/thread matches
include their root runs or retain the matching empty object. Highlight and open
matching running paths once; later manual folds prevail. Historical matches keep
the root run without reopening completed paths; details expose matching IDs for
inspect. Query compact indexed metadata within Recent and report coverage.
Clearing text only clears that filter. Filters affect visibility, not Stats or
summary scope; show matched/eligible object counts, counting root runs in Execution view.

Three sort choices: `activity` (default: active first, then latest activity),
`cost`, `time` (descending displayed value, unknown last). Sort agents, threads,
or whole root run trees according to the view, across agents. Never reorder
Tree descendants. Placeholders follow real rows; full precision and stable IDs
break ties. Preserve selected identity; clock ticks do not change activity order.
Single-agent mode uses identical rules; its Agent header needs no sorting.
No query language, regex, separate status filter, reverse order or extra sort keys.

| Control | Action / proposed CLI |
| --- | --- |
| a / t / e | View / `--view agent\|thread\|execution`. |
| F4 | Text and Active filters / `--filter TEXT`, `--active`. |
| F5 | List/Tree in Execution view / `--view execution --tree`. |
| F6 | Sort / `--sort activity\|cost\|time`. |
| F7 | Recent / `--recent DURATION`. |
| F8 | Stats / `--since session\|TIMESTAMP\|DURATION\|all`. |
| Left / Right | Collapse/expand running paths; Left on a leaf/collapsed row selects its visible parent, never agent/thread. |
| Up / Down, PgUp / PgDn | Select/scroll. |
| Enter | Details. |
| q / Ctrl-C | Exit without affecting execution. |

View shortcuts are disabled while editing text. F5 is inactive outside Execution;
`--tree` requires explicit `--view execution`. `--once` prints one snapshot.

## Persistence and delivery

```text
executor -> transaction(records + usage + aggregates + cursor) -> commit
         -> canonical stream -> shared activity reader -> agent HTTP / Hub HTTP
         -> top: replace values and render
```

Current `_emit_event_locked` commits structural records/cursors in
`RunStore.write_transaction()` before canonical publication. Extend store
transitions, including early step completion during control admission, within
this boundary. The executor supplies session/attempt identity; execution owns
aggregation. A genuine begin/end contributes once, even with duplicate events;
retry reusing a StepRef gets a new attempt identity.

| Persisted data | Purpose |
| --- | --- |
| Executor session, start/end/checkpoint | Default Stats, Uptime and recovery. |
| Attempt usage with ownership, timestamps, counts/cost and coverage | Preserve consumption and attribution when retry deletes execution records. |
| Agent/thread/run/step totals and minute count/cost buckets by session | Shared inclusive Stats for session/all or custom starts. |
| Run/step attempt intervals, closed totals and open anchors | Duration without nested double counting or per-redraw writes. |
| Activity index: parent, status, summary, last activity | Recent root runs, running paths and historical text matches. |

Write changes proportional to the affected ownership path, not history or viewers.
Use minute buckets for whole minutes, indexed facts for boundary minutes, and
attempt intervals for clipped TIME. Share query state and update affected totals;
never aggregate full step records per refresh/subscriber or copy outputs into
usage facts. Close crash-interrupted intervals at the last durable checkpoint
and mark coverage incomplete; do not count downtime or invent provider charges.

Publish absolute values/revisions at the same committed boundary as activity.
Hub/top replace them idempotently rather than recounting events. Preserve the
[agent subscription](local-subscriptions.md) and [Hub subscription](team-observation.md)
recovery contracts. Aggregate writes participate in the records transaction;
HTTP/SSE consumers and historical scans stay off the execution path. The exporter
uses Hub HTTP; the agent does not access the Hub backend directly.

One versioned, resumable migration backfills available records and deduplicates
against concurrent live writes. Normal startup reads saved totals/indexes.
Deleted legacy attempts and unknown session starts remain incomplete. Both HTTP
sources expose the shared reader and coverage; wider Stats/Recent may need an
atomic snapshot before resuming its suffix. View/layout changes and cached filters
reuse delivery. Exclude full prompts/outputs and token deltas, coalesce updates,
and redraw at most twice per second. CLI owns formatting, selection and sorting.

## Touchpoints and acceptance

Likely files: new `execution/activity.py` and `execution/statistics.py`;
`execution/store.py`, `records.py`, executor lifecycle/persistence hooks and
subscription adapters; `teaming/exporter.py`, `teaming/records.py`, HTTP adapters;
`cli/common/activity.py`, `cli/toolang/commands/top.py` and focused tests.
Reuse execution summary helpers, Rich and prompt_toolkit.

| Scenario | Pass condition |
| --- | --- |
| Sequential/parallel flow, async launch and completion | Run/step Tree nodes have their own IDs/statistics and recorded parentage; only required completed ancestors remain; one terminal root result. |
| Views, layout, resize and single-agent mode | RUN repeats root ownership; STEP contains every row's full record reference, including the root; nested step inspect needs no indentation context; statistics remain row-scoped; Agent/Thread show root-run counts. |
| Historical matches, Active and sorting | Matches remain inspectable without reopening finished paths; pending/stale stay marked; sorting preserves trees. |
| Stats/Recent, aging, restarts and pagination | Independent ranges, correct TIME*, no lost active work, no statistic changes from visibility. |
| Boundary calls, overlapping runs, unknown cost | Correct Stats/Total, duration clipping and coverage markers; no nested/global double counting. |
| Mid-run attach, retry, rewind, fork and duplicates | Recovered totals equal committed consumption without loss or duplicates. |
| Rollback, crash after commit and interrupted migration | Atomic writes, resumable deduplicated backfill, no normal-startup full history scan. |
| Offline agent, Hub reconnect and incomplete recovery | No invented idle/failure/zero; stale time freezes and confirmed running paths recover. |
| Multiple subscribers, large history and both HTTP sources | Shared compact reads; equivalent agent/Hub data without per-client history aggregation. |

Open questions before implementation:

- Model summary source: tool steps have explicit summaries; model steps do not.
  Define a summary field or a labeled existing-content preview.
- Specify compact HTTP frames, cursor adaptation, history pagination and projection
  limits over persisted Stats. Large running paths must expose incomplete coverage.

Risks are migration size, missing legacy usage/model summaries, and atomic aggregate
maintenance; acceptance must cover them before implementation handoff.
