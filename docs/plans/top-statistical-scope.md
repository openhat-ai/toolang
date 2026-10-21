# Top statistical scope

Status: approved for implementation by the user on 2026-10-21, including the
statistical scope and systematic implementation requirements below. This PR
contains the definition only; implementation is a separate change.

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
agents: 2 resident, 0 visiting, 0 roaming                                              right now
usage: 191 model calls, 198 tool calls, 100k input, 80k cached, 10k output, $1.0000      last 4h
runs: 0 active, 0 succeeded, 0 failed, 0 canceled                                        last 5m
AGENT THREAD RUN MODEL TOOL INPUT CACHED OUTPUT SPEND TIME+ STATUS
```

Render `agents:`, `usage:` and `runs:` in normal intensity, without dim or bold.
The right column contains only `right now` or `last <window>`, in dim;
retain the existing numeric coloring and width-dependent wrapping rules.

The agents line counts scoped source identities by placement. Remove its
`running` and `total` prefix: "right now" describes membership at observation
time, not whether every member has a running instance.

The usage line aggregates that scope with the independent Period setting (`4h`
in the example; preserve the `1h` default). The runs line and table share the
same scope and Recent setting: current active
root Runs of any age plus terminal roots completed within Recent. Child Runs
remain part of their owning tree, without inflating root counts. Loaded pages,
folds and selection never change summary totals. Keep row consumption as
lifetime values; changing Period does not filter rows.

Omit the aggregate `N completed:` from the runs line; show active, succeeded,
failed and canceled counts directly. Keep the completion total in JSON and
paging logic. The usage line includes model/tool invocation counts, tokens and
spend.

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

The separately approved [compact instance IDs](https://github.com/openhat-ai/toolang/blob/f6500f42/docs/plans/compact-instance-ids.md)
definition covers generation, all CLI displays and selection. It replaces the
earlier Top-only label proposal and is implemented in a separate PR.

## Implementation boundaries

Review of [PR #893](https://github.com/openhat-ai/toolang/pull/893) and
[PR #894](https://github.com/openhat-ai/toolang/pull/894) identified reusable
boundaries, but neither implementation is a dependency or should merge as-is.
#893 infers zero consumption from HTTP status; #894 retains departed sources
and does not discover stopped resident homes. Both policies conflict with this
definition. Reimplement around the following owners:

| Owner | Responsibility | Direct boundary tests |
| --- | --- | --- |
| `up.observation` | Validate registration identity and launch generation; expose runtime facts without deciding Top membership. | Disappearance/replacement during reads and probes; malformed or unreadable current registrations. |
| New `up.scope` | Compose materialized resident homes and current registrations; deduplicate identities; retain unknown coverage on discovery failure. | All placements; stopped residents; single-agent selection; removal, replacement, and discovery failure/recovery. |
| `up.activity.ActivityReader` | Read endpoints, history and pages; retain established-history knowledge across workers and resident rebindings. | Endpoint departure versus current failure; history missing, created during a read, lost, corrupt or unreadable; pinned pages. |
| `stats` | Attribute calls, usage and completion facts to their windows; propagate unknown quantities. | Known calls with unknown usage; provider-reported usage on failed calls; period boundaries independent of table pages. |
| Model adapters and execution | Preserve actual errors, invocation facts and reported usage through persistence. | Rejected calls with/without reported usage, including streaming; no invented zero consumption. |
| `up.top` | Schedule bounded independent jobs, reconcile the complete scope, and reject obsolete results. | Removal during pending success/failure; replacement; window changes; nonblocking refresh and cancellation. |
| `up.schemas` / `cli.common.top_view` | Derive coverage and totals / format pure snapshot values. Neither performs discovery or reads. | Shared roster across summaries/table/JSON; nullable coverage; layout and styles at narrow/wide widths. |

Keep source lifecycle policy out of Top's scheduler. A resident departure drops
only its live binding; visiting/roaming departure drops the complete source,
including cached results and errors. Rebinding the same resident preserves the
reader's knowledge of established history. Observation must not mutate runtime
registrations, create stores, or acquire execution leases.

A provider rejection remains a failed model call. Preserve available provider
usage and errors; absent consumption remains unknown. An invocation count can
be known while token consumption is unknown. Never infer zero tokens or cost
from HTTP status, and never use an accounting failure to remove a source from
scope. Endpoint failures from a superseded launch are obsolete; errors from a
current binding remain observable. Scope removal is determined by discovery,
not HTTP codes.

Test each owning module directly; retain only scheduling/projection integration
in observer tests. Use deterministic offline fixtures and events for races,
without sleeps. Avoid mock-only tests that merely repeat implementation calls.
Update current design/help text and generate the user-visible changelog entry.

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
6. Rejected model calls preserve errors and any reported usage through adapter,
   execution persistence and Stats. Missing usage stays unknown while known
   invocation counts remain available; HTTP status never fabricates zero usage.
7. Direct module tests cover the ownership table above. Observer tests exercise
   composition and scheduling rather than duplicating reader/registration tests.
8. At wide and narrow widths, verify agents, usage, runs, table order; normal
   left-hand names with colons; dim time ranges only at the right; placement
   counts only for agents; and no aggregate completed count in the runs text.
   Runs and table share Recent while usage keeps Period. JSON retains its
   keys/types, including completion totals, and exposes the scoped roster.

## Risks

Visiting/roaming totals can fall on departure before their facts leave a window;
this is intentional because the displayed agent scope shrinks at the same time.
Resident home discovery must not mistake an I/O failure for removal. Adding
stopped homes increases source-read work, so retain independent bounded workers
and nonblocking UI behavior.

Open questions: none.
