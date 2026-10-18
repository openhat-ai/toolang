# Top recent activity

Status: approved by the human on 2026-10-18. Definition only; implementation
requires a separate change. The implementation scope was amended by the human
to defer Details content: only its open/close placeholder is required now.

## Goal and scope

Make `too top` a dashboard of current and recent execution: a switchable rolling
usage summary and one table of live trees and recently completed root Runs.
Success means useful observation without `stats.db`, active-root contributions,
accurate current activity, and recovery of recent completions after reopening.
Historical exploration has a separate Stats plan and approval.

This definition replaces the older [Top activity](top-activity.md) and
[Top dashboard](top-live-layout.md) directions. Follow the implemented
[LiveTree](executor-live-tree.md), the approved
[statistics definition in PR #843](https://github.com/openhat-ai/toolang/pull/843)
and its [implementation in PR #844](https://github.com/openhat-ai/toolang/pull/844).
In scope: observed local resident/visiting/roaming instances, recent source
queries, TUI and snapshots.
Out of scope: historical analysis, remote discovery, new Hub infrastructure,
execution controls and retention changes. This PR contains only the Top plan;
historical Stats has a separate definition and implementation scope.

## Command and dependencies

- `too top` observes the selected root's local instance inventory, including
  resident, visiting and roaming placements; `too alice top` selects alice.
  Reuse `up.observation.runtime_snapshot` and its validated runtime registrations,
  refreshed every five seconds, rather than resident-only `AgentProcess.list`.
  Count unique instance identities using the existing inventory contract; use
  `RuntimeInstance.label` to distinguish same-name placements/instances. Never
  scan arbitrary source directories, start agents or retain server leases.
- Query discovered readable source stores even when execution is offline. Retain
  known source locations for the current observation's windows after a runtime
  disappears; expose unavailable coverage if its store can no longer be read.
  Reopening recovers recent completions from discoverable stores. Do not promise
  discovery of arbitrary detached temporary histories.
- Options: `--period 1h|4h|8h` (default `1h`),
  `--recent 1m|5m|15m` (default `5m`), `--refresh SECONDS` (finite, positive,
  default `1`), `--once`, `--json`. Non-TTY and JSON output are single snapshots.
- Offline agents with readable source stores still contribute usage and completed
  roots. Live coverage is unavailable; missing stores are unavailable, not zero.

```text
Top -> LiveTree snapshots                  -> current execution
    -> toolang.stats recent source reader -> runs.db (read-only)
```

Top never opens/creates/initializes `stats.db`, consults receipts or wakes archival.
Build on PR #844's `toolang.stats`: add a source-only recent query entry point
inside that package for Period usage and Recent completion totals/status counts.
Its existing `StatsService` and `StatsReader` depend on the statistics store and
receipts; do not call that historical facade from Top. Reuse
`projection.project_step`, `types.Metrics`, source-owned codecs,
bounded source paging/ownership resolution, nullable
arithmetic, USD normalization and timestamp attribution. Query original source
records across archived and unarchived roots; source records remain under
PR #843's retention contract. CLI/TUI code owns no SQL, accounting or duplicate
statistics implementation. Execution never imports Stats. Hosting composes the
readers, with concrete paths/time resolved at call sites. Implementation builds
on PR #844; this definition can be merged independently.

## Windows and accounting

| Setting | Behavior |
| --- | --- |
| Period | One rolling `1h`, `4h` or `8h` summary; switching does not filter table rows. |
| Recent | Include roots completed in `1m`, `5m` or `15m`; independent of Period. |
| Active | Always retain current pending/running roots, regardless of age. |

Capture UTC second T. Usage uses `[T-W,T)`. Completed rows have terminal status
and `finished_at` in `[T-R,T]`: include the current second so a committed root
enters the table immediately after leaving LiveTree. Creation time and descendant
activity do not determine completion eligibility. Expire rows/usage as time moves,
even without execution events.

Include each timestamped persisted contribution in the window, including those
from roots started earlier or still active. Never add entire LiveTree totals to
window values. Preserve PR #843's settlement: terminal invocation facts are
attributed to Step start and tokens/cost to model Step completion.
Unfinished calls appear immediately in live counters, then enter
window counters when their terminal facts persist. Do not invent unfinished
usage or charges. Explain this distinction in Help/Details.

Run rows show lifetime consumption, independent of Period: LiveTree for active
Runs, captured Run Noted for terminal roots. Parent Run totals include descendants;
never sum visible tree rows. Step rows have only their own facts; flow Steps have
no descendant usage totals. TIME+ is the node's own duration, not summed children.
Selection, folds and Recent never change scope-wide usage.

## Header and unified table

The first line states scope as `too top` or `too alice top`, with the clock flush
right on the same right edge as the summary titles. Below it place three
sentence-style summaries in order: agents, runs, stats. Metrics start at the
left; their titles (`agents`, `runs last 5m`, `stats last 1h`) sit without colons
in a fixed-width column at the far right, flush right and top-aligned with the
first metric line. Wrap continuation metrics under the left metric column.
Emphasize numbers and subdue labels. Use comma-separated values without boxes
or a statistics table heading.

Keep every field in each group visible in a fixed order, including all three
agent placements and all three terminal states. A complete empty inventory,
completed-root selection or usage window displays `0` for counts/tokens and
`$0.00` for spend. Missing provider usage/pricing, loading and unavailable sources
remain unknown (`-`, with the existing Updating/coverage indication); never
convert unknown or incomplete observations to zero or hide their field labels.

| Group | Meaning and fields |
| --- | --- |
| `agents` | Instance inventory counts by resident, visiting and roaming placement. |
| `runs last 5m` | Current active roots, then completed roots in Recent and their terminal-status breakdown; the window applies only to completions. |
| `stats last 1h` | Period usage: model calls, tool calls, input/cached/output tokens and spend; omit started roots. |

One body table follows, with one column heading and no Active/Completed
subheaders, separators or empty group placeholders. Active roots sort first in
admission order, sorted by creation time then source/root identity; terminal
roots follow by completion time descending then source/root identity.
Illustrative values and IDs below are sample data.

```text
too top                                                                                         14:32:10

2 resident, 3 visiting, 1 roaming                                                                 agents
3 active, 4 completed: 2 succeeded, 1 failed, 1 canceled                                    runs last 5m
76 model calls, 188 tool calls, 512k input, 360k cached, 54k output, $2.46 spend           stats last 1h

AGENT RUN          MODEL TOOL INPUT CACHED OUTPUT SPEND  TIME+ STATUS
alice run_7p2m9k4w    12   18   42k    26k   3.1k $0.38  2m14s flow:review_project
alice run_7p2m9k4w     -    -     -      -      -     -   1m8s └─ map agic:review_file
alice run_7p2m9k4w     3    4   11k     8k    800 $0.09    38s    ├─ agic:review_file · item 3/8
alice run_7p2m9k4w     1    0     -      -      -     -    12s    │  └─ openai/gpt-5
alice run_7p2m9k4w     2    3    7k     4k    500 $0.06    31s    └─ agic:review_file · item 4/8
alice run_7p2m9k4w     0    1     -      -      -     -     9s       └─ shell.exec
bob   run_7p2m3c8n     5    9   18k     9k   1.3k $0.16   8m2s flow:
bob   run_7p2m3c8n     -    -     -      -      -     -    24s └─ await worker
bob   run_7p2m5t6x     0    0     -      -      -     -       - pending · agic:
alice run_7p2m1b7z     8   12   28k    17k   2.4k $0.24     45s failed
bob   run_7p2m4f9s     4    3   13k     8k    900 $0.11     58s succeeded
alice run_7p2m8g2y     3    5   10k     6k    600 $0.08    2m8s canceled
bob   run_7p2m5n3v     2    4    7k     4k    400 $0.05     32s succeeded

F1 Help   F3 Details   F5 List   F7 Runs(15m)   F8 Stats(4h)   F10 Quit
```

- Agent placement counts follow the discovered instance inventory, including
  idle instances; they are not counts of agents with active work. Inventory
  admission/removal changes these counts. Do not duplicate online/active-agent
  counts elsewhere in the header; detailed liveness belongs in Details.
- `active` counts current pending/running physical roots, with identity scoped
  by source instance. Child Runs never add another root. Derive it from complete
  live snapshots before pagination/folding and update on admission/completion,
  even while older usage is loading. Recent/Period never restrict active roots.
- The `runs` duration applies only to completed roots, never its current active
  count. Recent counts cover the entire eligible completed-root set before pagination;
  Completed equals the three terminal-status counts. Recent expiry/completion
  updates the group even when no terminal row is currently loaded. Keep each
  group's range/coverage independent and mark partial coverage explicitly.
- RUN always displays the full owning root run_id, repeated on every descendant
  row. Do not display REF or child Run/Step identifiers in the list; retain exact
  node identity internally for selection and expose it in Details only.
- STATUS holds the tree branches, node name and current activity/terminal state;
  use one formatter across Runs and Steps, with no separate NAME column. Keep
  branches out of ID/numeric columns. Always retain MODEL and TOOL call-count
  columns alongside INPUT, CACHED and OUTPUT token columns, using compact values;
  exact values remain in Details. These are node lifetime metrics, independent
  of Period. Preserve unknown/approximate-cost metadata in Details.
- Design one layout for normal terminal widths, keeping the same columns and
  full headings. Numeric columns have stable widths; STATUS uses the remaining
  space when resized. Keep rows to one line and clip long STATUS text at display-cell
  boundaries, with
  complete references/text in Details. Never abbreviate the root run_id.
  Use a vertical table viewport and restore terminal state on exit.

## STATUS and Step presentation

An active Run displays its runnable as `agic:<name>` or `flow:<name>`. Unnamed runnables
display exactly `agic:` or `flow:`; add no `_`, role, line number or unnamed label.
This is display-only: use the existing language-owned parser to format captured
references, preserve full stored identity in Details and change no CLI resolution.
Use the same runnable formatter for flow Step targets.

Active Steps display their own model/tool/flow operation from the table below,
with a tree branch and applicable occurrence. Pending Runs display
`pending · <runnable>`. Finished Runs display only `succeeded`, `failed` or
`canceled` in the list: no runnable name, result or error preview. Those belong
in Details. A retained terminal Step may show its state and operation so the live
child path remains understandable. Color reinforces the textual distinction;
do not add a separate execution-state or NAME column.

| Live node | STATUS |
| --- | --- |
| Invoked model Step | `provider/model`; no `model:` prefix or fabricated prompt summary. |
| Ordinary invoked tool Step | `toolset.tool`; existing summaries belong in Details. |
| Model/tool before invocation | `preparing provider/model` / `preparing toolset.tool`; runtime operations also retain `preparing` until invoked. |
| Shared flow/runtime operation | `run <runnable>`, `async run <runnable>`, `spawn <runnable>`, `exec <runnable>` or `await <handle/target>`, independent of source kind. |
| Other flow operation | Authored operation plus available target/name, e.g. `map agic:review_file`, `keep agic:accept` or `repeat`. |
| Loop occurrence | Append recorded `item 3/8`, `lane 2/4` or `iteration 3`; these are positions, not completed counts. |

Normalize native flow Steps and recognized runtime Tool Steps before formatting.
Extract shared identity recognition from the existing progress presenter
(`cli/common/execution_progress/operations.py`) into an execution-owned pure
helper; preserve progress behavior and keep its UI types out of Top.
Recognize runtime operations only when `plugin == "runtime"` and the canonical
tool identity matches. Runtime currently exposes run/spawn/await/exec, not
collection operations. An agic calling a flow shows that flow's collection Steps
inside the owned child Run. Runtime-only tools such as `runtime.chdir` and
`runtime.compact` retain `toolset.tool`; never infer an operation from summary prose.

Keep source kind, canonical tool identity, invocation state and actual parentage
in Details. A normalized runtime operation remains one tool call for accounting;
the equivalent native flow Step does not become a tool call. Await/exec/spawn
targets are references, not new ownership edges. Decode captured tool names with
`base.utils.tools.decode_tool_name` and format their validated components with
`display_tool_name`; plugin identity is not a substitute for toolset identity.
Preserve undecodable names verbatim rather than guessing. Use the captured model
identity, not a configured alias. Width truncation may hide its suffix; Details
always shows the exact `provider/model` and canonical tool name.

LiveTree already carries `operation/name/plugin/target/summary/occurrence/invoked`.
Also carry `asynchronous` from the existing `FlowStepInfo`/`ToolStepInfo` into
`StepNode` (default false) so native and runtime async run remain distinguishable
without loading arguments. This is a bounded presentation fact, not a new record
format or accounting rule. Model/tool labels require no call/output materialization.
Do not derive completion progress from pruned nodes: current occurrence supports
positions and live branch counts, not a historical `completed/total` claim.

## Expanded and collapsed trees

Use the same rules for root Runs, child Runs and flow/runtime Step subtrees:

- Expanded rows show only their own runnable/operation and occurrence. Do not
  repeat child activity or hidden-branch counts when those children are visible.
- Collapsed rows follow the sole owned child path containing unfinished work to
  its deepest operation. Stop at a parallel Step (`kind == "par"`), even when
  only one lane is currently present, or any node with multiple unfinished owned
  child paths. Show that boundary's own label and `<N> active`, without choosing
  a representative lane. Terminal retained ancestors may be traversed, but never
  follow an await/spawn/exec target outside the owned tree.
- A collapsed Run keeps its own runnable, then appends the selected operation
  once. A collapsed Step displays the selected operation. Skip intermediate Run
  names and serial operations; append `[+]` only when descendants are hidden.
  Preserve the selected row's occurrence; it remains a position, not progress.
  A retained terminal Step keeps its own state/operation and reports only its
  hidden active-branch count, rather than assigning its state to a child activity.
- Projection changes STATUS only: RUN, counters, spend, duration and selected
  identity continue to belong to the original row. Details identifies the
  projected activity separately. Terminal Run labels remain state-only.

For the example above, folding roots changes only their STATUS:

```text
flow:review_project · map agic:review_file · 2 active [+]
flow: · await worker [+]
```

Folding only the first map child gives
`agic:review_file · openai/gpt-5 · item 3/8 [+]`; its visible sibling continues
to show `agic:review_file · item 4/8` with a separate `shell.exec` child row.

## Lifecycle and controls

A root completion replaces its tree with one terminal row, preserving root
selection; a finished selected descendant maps to its root. Read recent roots
from durable records on reopen. Keep no finished-tree archive in the executor,
and keep no output/error queries in the initial Details placeholder.

Up/Down selects. Left folds an expanded selected node; otherwise it selects the
visible parent. Right expands a selected folded node; otherwise it selects its
first visible child. Start in Tree with current paths expanded.
F5 toggles Tree/List: List folds all roots for presentation;
returning to Tree restores individual folds. Keep folds by exact node identity
within the source epoch; move hidden selection to its visible ancestor.
F7 cycles Recent `1m / 5m / 15m`; F8 cycles Period `1h / 4h / 8h`.
F3 toggles the Details placeholder (Enter is an alias), Esc closes it,
PgUp/PgDn pages the table, F1 opens Help, and F10 exits (q/Ctrl-C are aliases).
Details content, exact values, full references and output/error queries are
reserved for a separately discussed definition. The initial pane only states
that Details is not defined yet and how to close it; cap it at half the content
area. References to Details elsewhere in this plan describe that future
presentation, not acceptance requirements for this implementation.
Clock/durations tick
while fresh; disconnected live data is stale and frozen, not confirmed running.
The footer lists function keys only; keep conventional aliases in Help. F7/F8
labels show the next setting, not the current one: with Recent `5m` and Period
`1h`, show `Runs(15m)` and `Stats(4h)`. Header titles always show the current
windows. F3 shows Details/Close and F5 shows List/Tree for the next action.
CLI options remain `--recent` and `--period`.

## Observation, touchpoints and verification

Online hosting pins a read-only source snapshot and captures LiveTree under the
existing publication gate. Release the gate before scans/response construction.
Compose live trees and recent terminal roots at that boundary to avoid duplicate
or missing completion rows, and expose a read-only agent HTTP adapter independent
of historical Stats initialization. Offline local observation uses the same source
reader with live coverage unavailable. Do not create a new publication bus.

Frames carry source-instance identity/placement/epoch, component observation times
and coverage, selected Period/bounds and summary, Recent bounds/status counts,
live trees, and pageable completed roots with eligible count. Source statistics
refresh asynchronously; slow usage must not block live counters or input. Reject
obsolete Period/Recent responses and show Updating rather than labeling old values
with a new window. Epoch changes replace
live trees; disconnection never implies completion. Cross-agent reads are not an
atomic transaction, and incomplete scope totals must not masquerade as complete.

Use indexed Step start/finish reads for the selected window and an indexed
physical-root terminal completion query, including status counts across all
eligible roots. Read Run Noted without descendant Steps.
Page a fixed completed-root selection, exposing loaded/eligible counts as needed.
Add indexes only through normal Store schema ownership; profile a busy eight-hour
source interval. No accounting changes, persistent activity table or second writer.

Likely files: PR #844's `stats/query.py`, source SQL and pure projection/types,
extended with a source-only recent query interface; execution inspection/query schemas,
`live.py` and semantic Step projection; shared progress-operation recognition;
existing `up/observation.py` inventory integration;
Store-owned indexes; hosting/API composition; `cli/toolang/commands/top.py`, CLI
factory/routing and a focused presenter.

| Acceptance scenario | Pass condition |
| --- | --- |
| Missing/corrupt/initializing `stats.db` | Top works from available sources without opening/creating that database or starting archival; persisted aggregates come through `toolang.stats`. |
| Every Period, boundary timestamps, long-running roots and archived facts | Totals match a direct-source oracle, including settled active-root contributions and nullable/approximate quantities; unfinished consumption is not invented or counted twice. |
| Inventory changes, idle instances and parallel children | Placement counts follow inventory; active counts follow physical-root lifecycle and remain independent of folds/windows. |
| Native/runtime run, async run, spawn, await and exec | Equivalent labels preserve source kind, invocation state, ownership and tool counts; lookalikes/runtime-only tools keep tool identities. |
| Expanded/folded serial paths, one/many parallel lanes and retained async ancestors | No repeated visible child activity or representative lane; row identity/metrics and restored folds are stable; occurrence never claims completion progress. |
| Root/child/Step rows and Details | Full owning root ID stays in RUN; child IDs are Details-only; typed/unnamed runnable, provider/model and toolset.tool formatting is correct; finished Runs show terminal state only. |
| Both scopes, empty/idle data and normal-terminal resize | All columns and fixed header fields remain; right-side titles share the clock's edge; known empty is zero, unknown is explicit; no group subheaders or started-root metric. |
| Window/Tree/List/Details controls | Footer shows only Fn keys and the next setting through wraparound; header shows current windows; F3/Enter share Details; Period/folds do not change lifetime values. |
| Completion during refresh, current-second completion, reopen and idle expiry | Exactly one durable root row, stable selection and correct eligibility; Recent counts include unloaded rows and equal terminal-status subtotals. |
| Many roots, busy eight-hour stores, slow/partial/offline sources and epoch/query changes | Bounded indexed paging, fresh live input/counters, explicit coverage and no obsolete or falsely fresh replacements. |
| Single/all-agent snapshot/JSON, Unicode, paging and exit | Equivalent metrics, correct clipping, placeholder interaction and restored terminal state. |

Implementation requires acceptance tests and default repository verification.
For this definition, verify source contracts/links and run `git diff --check`.

## Risks and open questions

- PR #844 is an implementation dependency. Keep its historical facade and
  accounting rules intact when adding the source-only entry point.
- Eight-hour source scans can be expensive. Profile a busy source and verify
  indexed bounds, paging and nonblocking live interaction.
- Missing providers, disconnected runtimes and detached histories can leave
  incomplete coverage. Preserve the unknown/stale distinctions above.
- Completion publication can race with observation. Verify the pinned per-source
  boundary and current-second handoff; cross-source snapshots remain non-atomic.

No open product questions remain. The human approved this definition and its
defaults; implementation and the historical Stats plan remain separate work.
