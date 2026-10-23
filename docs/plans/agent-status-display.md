# Agent status across `info`, `list`, and `hub`

Status: Proposed on 2026-10-23; awaiting human confirmation. Definition only.
Audit baseline: `origin/main` at `21fe16cd`; PR #931 and its uncommitted follow-up
are proposals, not the current implementation.

## Goal and scope

Make agent identity, lifecycle state, and resource provenance understandable across
`too info AGENT`, `too list`, and `too hub` (`too` aliases `toolang`). A given
workload must receive the same lifecycle classification from the same evidence,
while each command retains its own membership scope.

Success means a never-started resident remains useful to inspect, retained
instances remain inspectable without source contents, and unavailable resources
or failed inspection never masquerade as workload failure or confirmed shutdown.
Change observation and these CLI projections only. Keep addressing, lifecycle
operations, storage formats, Top/statistics scope, and resource APIs compatible.
No new command, flag, status service, persistent snapshot format, or polling loop.

## Verified implementation

| Surface | Membership and data path | Current presentation |
| --- | --- | --- |
| `list` | `agent_layouts(root)` scans named resident home directories, including homes without `agent.too`; `AgentProcess.list()` probes each with at most eight workers. | `AGENT STATUS SANDBOX PORT WEBUI`; only `stopped` becomes `not running`. Other phases remain visible; diagnostic messages are omitted. |
| `info` | `select_agent_instance()` binds a known home. `AgentProcess.status()` supplies lifecycle; separate reads supply runtime metadata and process/container identity. Running resources come from five API requests; offline resources come from saved counts and prepared State. | Detailed identity, resources, status and running-only drivers. Resource-fetch errors abort the entire view. Missing saved counts become `-`; saved values have no provenance label. |
| `hub` | `runtime_snapshot()` / `runtime_inventory()` follow active registrations for all placements, validate ownership/generations, batch Docker probes, and cap independent probes at eight. They do not enumerate installed resident homes or historical roots. | Headerless tree with status and recorded Talk selection. Agent states are reduced to `running`, `not running`, or `unknown`; the renderer prints unknown as `-`. Diagnostics follow the summary. |

The hub service row is a separate observation: `HubProcess.current()`, `ready()`,
and `version()` establish process identity, readiness and server version. The
renderer currently converts every non-ready hub state to `not running`.
The tree describes root-local discovery, not hub connection or messaging presence.

Relevant paths:

- [Agent commands](../../src/toolang/cli/toolang/commands/agent.py),
  [process inspection](../../src/toolang/up/process.py), and
  [resident discovery](../../src/toolang/up/discovery.py).
- [Instance observation](../../src/toolang/up/observation.py),
  [observation vocabulary](../../src/toolang/up/types.py),
  [hub command](../../src/toolang/cli/toolang/commands/hub.py), and
  [hub renderer](../../src/toolang/cli/toolang/commands/hub_status.py).

### Why behavior changed

- [#828](https://github.com/openhat-ai/toolang/pull/828) originally combined installed
  residents and registered instances in the hub snapshot.
- [#883](https://github.com/openhat-ai/toolang/pull/883) explicitly required retained
  `info`/`inspect` without readable sources, refetching, preparation, or runtime
  creation. [#884](https://github.com/openhat-ai/toolang/pull/884) implemented saved
  resource summaries and changed the shared inventory to active registrations
  only. This also removed never-started residents from hub and left their offline
  `info` resources unavailable when no summary exists.
- [#919](https://github.com/openhat-ai/toolang/pull/919) made `info` a home command
  and added the full address as an `Agent` row to distinguish instances. That row
  duplicates the title for residents, but carries an instance code for nonresidents.
- [#897](https://github.com/openhat-ai/toolang/pull/897) gave Top its own resident-plus-
  active-instance scope and read-only inventory access. Its scope must not be
  inferred from the hub tree or changed by this work.

### Problems to address

The first three cases below were reproduced in isolated temporary homes.

1. Membership is implicit. An installed `gus` can appear in `list` while hub says
   `no agents`. Its totals count instances, including repeated agent names.
2. Phase and observation quality are conflated. Preparing/starting agents become
   unknown/partial in hub; unreadable status becomes `failed` in `AgentProcess`;
   a starting or unavailable hub becomes `not running` in rendering.
3. Probe policy differs: a live record marked `starting` can become `running`
   through an incidental health request with `cache=False`, while hub's cached
   probe retains `starting` and then projects it as unknown. Cache choice changes
   classification rather than only cost.
4. `info` couples basic status to optional resource inspection. It also rereads
   metadata after the status probe, and its resource client has no launch binding;
   restart races can mix observations. Five sequential resource requests each
   default to 30 seconds. These are not one atomic resource-generation snapshot.
5. Tests and comments can preserve obsolete intent: snapshot renderer fixtures
   still include stopped residents, while integration tests explicitly exclude
   unregistered residents. Product behavior must be established from actual callers.

## Recommended contract

### Membership and identity

| Command | Question answered | Proposed membership |
| --- | --- | --- |
| `list` | Which resident agents are installed here, and what is their state? | Keep the selected root's resident home discovery; show malformed homes with diagnostics rather than silently hiding them. No visiting/roaming history scan. |
| `hub` | What is the hub service state, and which runtime instances are currently discovered here? | Keep active registrations across placements, including unresolved entries with recoverable identity. Exclude released historical instances and departed registrations; absence does not prove shutdown. |
| `info` | What does this selected agent/home identify, and what can be observed about it? | Keep existing home selection, including retained paths and last-created roaming selection. Never allocate a home, refetch an agent, or refresh historical project configuration. |

Preserve complete nonresident addresses in the `info` title; keep the current
resident title styling. Remove the duplicate `Agent` row. Keep the existing
Root/Home meanings. A canonical instance code must survive wrapping unchanged.
`hub` summaries say `instances`, and its empty leaf says `no active instances`.
List summaries continue to say `agents`. Tree grouping remains independent of
whether hub is running or an agent has Talk enabled.

### Lifecycle facts and presentation

Use the existing lifecycle vocabulary; do not add a new state machine.

| Evidence for the selected workload | Shared displayed status |
| --- | --- |
| Existing resident home with no launch, or verified stopped workload | `not running` (`stopped` internally) |
| Verified preparation owner | `preparing` |
| Verified live owner whose launch has not published readiness | `starting` |
| Verified live owner with a matching published running launch | `running` |
| Matching workload failure record | `failed` |
| Unverifiable identity/liveness, corrupt or unreadable observation | `unknown`, with a diagnostic |

Known transitions are normal observations, not partial failures. Missing/invalid
driver metadata affects that field only; it does not change a known running phase.
Only a verified running launch can contribute to running/Talk-enabled totals.
Read drivers from that launch, never from current config or an older launch.
The Talk column displays `enabled`, `disabled`, or `-` under the existing driver rules.
Use `-` for absent fields; spell out `unknown` for an uncertain lifecycle state.

Default lifecycle observation performs identity/liveness checks without per-agent
HTTP readiness probes. `cache` changes probe reuse only. Explicit startup/attach
readiness checks keep their existing health policy. Resource API failure is a
separate diagnostic and must not overwrite a verified lifecycle phase.

For the hub service itself, retain its distinct readiness vocabulary and show
`not running`, `starting`, `running`, `unavailable`, or `unknown` as observed.
Version lookup failure changes the version to `unknown`, not the service state.
Retain existing bounded hub HTTP checks, identity protection and terminal layout.

### Resources in `info`

| Selected home | Resource source and label |
| --- | --- |
| Verified running, any placement | Actual runtime resources; `Resources: live`. |
| Resident without a verified running owner | Current local configuration; `Resources: configured (local)`. |
| Nonresident without a verified running owner | Existing saved summaries/State only; `Resources: saved`. |
| No usable source for the applicable view | Missing values are `-`; use `Resources: unavailable` when the entire block is unavailable. |

This is an explicit resident configuration preview and retains the historical
instance contract for visiting/roaming homes. Local configuration is not a
promise about a future guest environment. Preserve current effective/allowed
resource count semantics; do not introduce new total/ready count categories.
Saved values may be stale; do not describe startup counts as a current or atomic
runtime snapshot. Retain available saved fields when other fields are missing.

Use existing inspection entry points for resident configuration:
`load_setup(validate_defaults=False)`, `inspect_agent_caps`, `load_chores`, and
`ConfiguredWorkspaces`. Count without preparing/publishing a complete Agent State,
starting watch loops/executors, or requiring default/Talk/compaction models to be
available. Existing rebuildable inspection caches may be written.
Keep `--catalog` for this local configuration view; reject it for live/saved views
instead of silently ignoring it. Current source resources for nonresidents remain
available through existing source-oriented resource commands.

Render known identity/status even when the resource block fails, with unavailable
values and a concise resource diagnostic. Unexpected inspection failures keep
`info`'s nonzero exit; an expected missing saved summary remains successful.
Retain `list`/hub's best-effort row/partial reporting and existing exit behavior.
Use a two-second timeout per runtime resource request at the `info` call site;
do not alter execution-client timeouts or add an aggregate API endpoint.

### Ownership and implementation shape

- Extend/reuse `AgentStatus` for canonical phase, observed generation, recorded
  drivers and diagnostic facts. `up.process` owns one-workload classification;
  `up.observation` owns discovery, deduplication and bounded batch probing.
  CLI code owns labels, field order, count units and formatting.
- Preserve the existing `RuntimeInstance` classification used by Top/activity.
  Carry the canonical observation alongside existing source bindings when hub
  needs it; do not globally reinterpret that projection or change other consumers.
- Bind a detail view to one validated status entry/launch. Derive metadata and
  runtime identity from that entry, rather than independent rereads. Recheck the
  launch after optional HTTP reads; discard obsolete resource results and report
  replacement instead of combining generations. No atomicity claim across the
  five resource revisions is introduced.
- Preserve existing generation-fenced dead-owner reconciliation and registration
  cleanup. Making every status read filesystem-pure is separate work. Add no
  source preparation, historical projection updates, or new recovery behavior.

## Implementation touchpoints and acceptance

Likely files: the linked command/observation modules; existing
`cli/common/client.py` for inspection timeout selection; resource inspection
entry points remain package-owned. Update current CLI/hosting/messaging design
only when the approved implementation lands. Generate its changelog through
`too aid.too update_changelog` and require maintainer verification before merge.

| Scenario | Pass condition |
| --- | --- |
| Installed resident never started | List includes it as not running; info shows configured resources without publishing Agent State; hub's empty active roster is explicit. |
| All lifecycle phases | `info`, `list`, and `hub` give the shared classification from identical evidence; starting/preparing do not become unknown or partial. The service's non-ready states remain distinct. |
| Unavailable configured defaults | Missing credentials or unavailable default/Talk/compaction refs preserve the resident overview and computed resource counts. |
| Retained instance with missing/edited source | Exact and last-created selectors keep their selected home and saved view; no refetch, allocation, configuration projection update, or source requirement. |
| Observation/resource errors | Corrupt status is unknown with a reason; a recorded workload failure remains failed. Resource errors preserve identity/status, report their own diagnostic, and respect timeouts/exit policy. |
| Same-name instances and drivers | Complete addresses distinguish rows; instance/agent count units are correct; stale or invalid driver data cannot enable Talk or change lifecycle. |
| Replacement and discovery races | Departed/replaced generations cannot supply current metadata, resources or counts; missing registry entries do not prove death; peer failures remain isolated. |
| Cost and compatibility | At most eight independent probes and batched Docker checks remain; no per-agent health requests for basic observation. Existing Top/read-only scope, lifecycle/API contracts, terminal widths, colors and ASCII/plain output remain intact. |

Extend existing `test_agent_info.py`, `test_process.py`,
`test_runtime_observation.py`, `test_hub_commands.py`, `test_hub_status.py`, and
the local CLI integration tests. Use real inspection paths with isolated plugin
and network seams. Run the repository's default checks for implementation.

## Risks and decisions requiring confirmation

The resident configured view intentionally qualifies #883's uniform saved-view
rule; retained nonresident inspection preserves it. Explicit provenance is needed
because local and guest availability can differ. Clearer hub words, count units,
and info title/provenance output affect human-output parsers.
