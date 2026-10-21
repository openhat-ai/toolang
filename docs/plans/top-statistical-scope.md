# Top statistical scope

Status: proposed for approval. Scope choices were confirmed in discussion on
2026-10-21; this document defines the implementation boundary.

## Goal

Make agent counts, Run summaries, usage summaries and table eligibility describe
one discoverable set of agent homes. Resident history must remain observable
without a running instance. Reopening Top must not change that set when the
homes and registrations are unchanged.

This replaces the membership, departed-source retention and summary layout in
[Top recent activity](top-recent-activity.md). Preserve the existing
[window, accounting, paging and presentation contracts](../design/top.md)
except for the explicit changes below.

## Membership and lifecycle

| Placement | Included in Top's scope |
| --- | --- |
| Resident | Every materialized resident home under the selected root, whether registered, stopped or never started; also retain any currently registered resident whose home cannot be read. |
| Visiting / roaming | Only identities in the current validated runtime inventory; their registrations supply the paths to their homes and data. |

Reuse `up.discovery.agent_layouts` for resident homes and
`up.observation.runtime_inventory` for registrations and runtime facts. Deduplicate
by the existing full source identity. `too alice top` includes only that resident
home and its runtime binding, including when it has no instance.

- Resident instance departure removes its live binding but keeps its home,
  completions and usage in scope. Removing an unregistered resident home removes
  that source at the next discovery refresh.
- Visiting/roaming departure removes the source, its rows and its contributions
  to both summaries at the next successful discovery refresh. Remove the old
  eight-hour session retention policy. Late worker results, including errors,
  cannot republish a source outside the current scope.
- Rediscovery replaces the binding for the same identity without duplicating
  history. A newly registered visiting/roaming source contributes all available
  facts in the selected windows, including facts from an earlier registration.
- Preserve registration-generation fencing. A discovery failure is unknown
  coverage, not an empty roster or proof that a source departed.
- A never-initialized inactive home contributes known zero. A previously observed
  store that disappears, or an unreadable/corrupt existing store, stays
  unavailable. Observation creates no homes, databases, processes or leases.

Do not add a persistent source catalog, scan departed visiting/roaming homes,
search arbitrary source directories, change retention on disk, or change the
Hub's runtime-only inventory.

## Summaries and table

Use this order, with left-hand names and right-aligned time ranges:

```text
agents: 2 resident, 0 visiting, 0 roaming                                                   right now
stats: 191 model calls, 198 tool calls, 100k input, 80k cached, 10k output, $1.0000    in the last 4h
runs: 0 active, 0 succeeded, 0 failed, 0 canceled                                      in the last 5m
AGENT THREAD RUN MODEL TOOL INPUT CACHED OUTPUT SPEND TIME+ STATUS
```

Render `agents:`, `stats:` and `runs:` in normal intensity, without dim or bold.
The right column contains only `right now` or `in the last <window>`, in dim;
retain the existing numeric coloring and width-dependent wrapping rules.

The agents line counts scoped source identities by placement. Remove its
`running` and `total` prefix: "right now" describes membership at observation
time, not whether every member has a running instance.

The stats line aggregates that scope with the independent Period setting (`4h`
in the example; preserve the `1h` default). The runs line and table share the
same scope and Recent setting: current active
root Runs of any age plus terminal roots completed within Recent. Child Runs
remain part of their owning tree, without inflating root counts. Loaded pages,
folds and selection never change summary totals. Keep row consumption as
lifetime values; changing Period does not filter rows.

Omit the aggregate `N completed:` from the runs line; show active, succeeded,
failed and canceled counts directly. Keep the completion total in JSON and
paging logic. "Usage" includes model/tool invocation counts, tokens and spend;
the displayed line name remains `stats:`.

Keep active-root proof and unknown-coverage rules: instance departure never
invents a terminal Run status, and unreadable live/history state never becomes
zero activity. A readable local source with no unfinished roots can prove idle.
Run and usage windows retain their existing timestamp attribution and boundaries.

Keep CLI flags, presets, shortcuts and JSON field names/types. Top's JSON
`inventory` becomes its complete scoped roster, including unregistered resident
homes. `summary.total` counts that roster; `summary.running` continues to count
confirmed running instances within it, though neither appears in the agents
line. Summary placement counts, source results and table projection use the same
scoped roster. Incomplete coverage remains nullable with diagnostics.

## Implementation boundaries

Build on the reader separation in
[PR #894](https://github.com/openhat-ai/toolang/pull/894).

- Add `up.scope` to compose home discovery and runtime inventory into Top's
  scoped bindings and runtime states. Keep filesystem membership policy here,
  rather than in the scheduler or presenter.
- Keep endpoint/history reads and established-history state in `up.activity`;
  preserve source accounting in `stats.recent`.
- `up.top` schedules reads for scoped bindings, reconciles removal/rebinding,
  and rejects obsolete worker results. `up.schemas` derives summary coverage
  and counts from that roster without performing discovery or I/O.
- `cli.common.top_view` renders the named summaries, dim time ranges and reduced
  runs line. Update current design/help text and generate the user-visible
  changelog entry when implementing.

Likely tests: `tests/unit/up/test_scope.py`, reader and observer unit tests,
`tests/unit/cli/test_top.py`, and Top snapshot/terminal integration tests.

## Acceptance checks

1. Two resident homes without registrations appear as two residents in a fresh
   Top and after reopening; readable historical Runs and usage are included.
   A never-started home reports zero without creating a database.
2. Resident startup, shutdown and launch replacement preserve one source and
   its window contributions; unavailable coverage retains the existing errors.
3. Registered visiting/roaming sources contribute to every view. After
   deregistration, the next successful discovery removes their counts, rows,
   usage and completions together, even while those facts are inside a window.
   Pending successes or failures cannot restore them.
4. Identical homes, registrations, records and observation time produce the same
   scoped results in an ongoing observer and a fresh observer. Rediscovery never
   duplicates data. Single-resident mode excludes the other placements.
5. Home enumeration and registration errors retain unknown coverage; corrupt
   databases and lost established history remain unavailable. No alternate
   source catalog or observation side effects are created.
6. At wide and narrow widths, verify agents, stats, runs, table order; normal
   left-hand names with colons; dim time ranges only at the right; placement
   counts only for agents; and no aggregate completed count in the runs text.
   Runs and table share Recent while usage keeps Period. JSON retains its
   keys/types, including completion totals, and exposes the scoped roster.

## Risks and approval

Visiting/roaming totals can fall on departure before their facts leave a window;
this is intentional because the displayed agent scope shrinks at the same time.
Resident home discovery must not mistake an I/O failure for removal. Adding
stopped homes increases source-read work, so retain independent bounded workers
and nonblocking UI behavior.

Open questions: none. Human approval of this definition is required before
implementation; this change contains documentation only.
