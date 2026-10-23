# Agent observation across `info`, `list`, `hub`, and `top`

Status: Proposed on 2026-10-23. Command semantics, Watcher ownership, and
host/guest cleanup requirements were specified by the user; implementation
details await confirmation. Definition only. Audit baseline: `e88d0539`.

## Goal and scope

Use one membership/status contract and recover registrations left by terminated
processes. Reuse existing discovery, Watchers, identity checks, and shared-file
primitives. Change observation, registration withdrawal, required sandbox mounts,
and command integration. Preserve command names/flags, execution-store formats,
Run/usage semantics, and hub-service observation. Add no daemon, heartbeat,
expiry timer, history index, or duplicate scanner.

## Command semantics and current gaps

| Command | Required membership and result | Current gap |
| --- | --- | --- |
| `list` | All created resident homes in this root, including inactive residents; show running status. | Membership matches; probing, readiness, and repair policy are mixed. |
| `hub` | Those residents plus active visiting/roaming instances; show running status and the matching launch's Talk driver selection. | Active-registry-only discovery omits inactive residents; lifecycle phases are collapsed. |
| `top` | Same collection as hub; count agents, sum usage in `--period`, show all active root Runs regardless of age and completed root Runs in the independent `--recent` window. | Read-only discovery retains finished nonresidents; unregistered resident phases are synthesized. |
| `info AGENT` | Resolve a name, URL, ref, path, or exact instance to one home; prepare and show placement, identity, Setup, State, and running status. | Existing-home/live-resource/saved-count branches bypass the Setup/State Watchers. |

Count full instance addresses, not unique short names. Resident history remains
in scope after shutdown. A departing visiting/roaming instance loses its complete
contribution to agents/usage/runs at the next discovery refresh, even before its
usage/completion windows expire. Preserve optional resident-scoped top.
Explicit info can inspect a known nonresident home outside collection membership.

## Observation ownership

Generalize/rename existing `ScopeReader` to `RuntimeWatcher`.
`refresh()` returns a `RuntimeInventory`; `current()` returns its latest
publication. Consolidate current discovery/probe/repair paths rather than adding
a facade over competing implementations. Keep `TopObserver` responsible for
Run/usage scheduling.

Expose `runtime_status(layout)` from the same module. Info calls it for its
selected home without constructing a watcher/inventory or enumerating peers.
It shares per-instance probing and conditional repair with RuntimeWatcher.
Registry-lock recovery may inspect that lock's recorded owner independently;
this does not require a collection scan.

Keep RuntimeInventory and its existing bindings/snapshot. Unify AgentStatus and
RuntimeInstance lifecycle classification; retain canonical addresses and external
wire identities. Remove duplicate scans, obsolete identity-label fallbacks where
unused, and descriptions restricting shared observation to top or hub.

## Status, membership, and statistics

A registration is a discovery candidate. Verify process identity using PID plus
creation time, or verify the sandbox resource identity; guest PIDs have no host
meaning. Read phase, launch, endpoint, and drivers from the same status snapshot.
Basic observation does not request HTTP readiness; startup/attachment retains
explicit readiness checks. Cache changes freshness, never the classification rule.

| Verified lifecycle evidence | Phase | Active-root observation |
| --- | --- | --- |
| Never launched or confirmed finished | stopped, or failed for a matching failure record | Known zero; retain persisted usage/completions. |
| Live preparation owner | preparing | Use available execution evidence; the phase alone must not fabricate a live-data sample. |
| Live workload during startup | starting | As above; missing endpoint alone does not prove zero active Runs. |
| Matching live running workload | running | Use the bound API or existing embedded execution-store path. |
| Unverifiable identity/liveness or unreadable status | unknown, with a diagnostic | Unknown unless independent valid execution evidence resolves it. |

Render stopped as "not running". Terminal workload evidence proves no physical
active Runs; it does not rewrite unfinished persisted Runs into terminal Runs.
Update the existing live-observation predicate and top's idle/stale handling from
this table, replacing string checks for "not running". Missing driver metadata
changes only the driver field. Show Talk enabled/disabled only for a matching
running launch with valid driver metadata; otherwise show "-".

Carry an explicit `membership_complete` flag in the existing inventory snapshot,
separate from diagnostic text. It is false for incomplete enumeration or
unresolved nonresident activity/membership. A resident's unknown status does not
make its known home disappear; its agent count remains known while running/live
counts may be unknown. Driver warnings and deferred cleanup do not invalidate
known membership or totals.

Preserve only bindings whose membership cannot be resolved. Successful enumeration
proves an absent registration departed; one corrupt peer must not freeze unrelated
departures. Exclude confirmed-finished nonresidents even when deletion is deferred.
Drop their cached usage/runs and reject obsolete in-flight results together.

## Prepared details in info

Resolve/select the home before preparation. Keep exact-instance and current-root
latest-retained selection; a fresh path uses the existing source preparation home,
and a fresh URL/ref uses its stable visiting home. Preserve that selected layout
when materializing inputs. Inspection must not call the execution allocator,
publish a preparing workload, or change execution-history command selection.

Refresh `SetupWatcher(validate_defaults=False)` and `StateWatcher` once.
Read tools/models through Agent Setup's public methods and caps/chores/workspaces
from Agent State. Apply `--catalog` to inspection Setup. No custom scans,
configuration merging, direct State preparation, watch loops, executor, or Run
database. Use this path whether or not the selected workload is running; label
the block as prepared configuration, not effective execution-time overrides.

Source preparation must not fail or withdraw a launch it does not own. Move
runtime failure handling into the owning execution call site, supplying its
expected launch/operation identity. Info must not inherit
materialize_roaming_program's current fail_preparation side effect.

Add Placement; put the full address in the title and remove the duplicate Agent
row. On preparation rejection, still obtain single-home runtime status and show
known identity/status plus preparation diagnostics with nonzero exit. Label any
Watcher last-valid publication explicitly. Do not substitute startup counts.

## Shared files and mounts

Keep the current layout and registration payload:

```text
TOOLANG_ROOT/.runtime/active/
    register.lock
    <address>.json              # {version, status: <host path>, launch_id}

AGENT_HOME/.runtime/
    status.json                 # Authoritative control/workload state
    status.json.lock
```

register.lock serializes this root's reference publication/withdrawal.
status.json.lock serializes one home's authoritative state. Both use the existing
shared-filesystem lock primitive. A lock claim exists while owned and is removed
on normal release; acquisition may briefly create the primitive's existing
.toolang-lock-* temporary file. No separate owner file or persistent index.

Mount the existing home and additionally mount the selected root's active
directory read/write at an explicit guest control path. Pass the reference name,
canonical host status path, launch_id, and mounted registry location as launch
metadata. Do not reconstruct host paths from guest paths. Mounting a single
reference file is insufficient for unlinking its directory entry. Directory
access grants registry write access; protocol checks are not filesystem-enforced
per-agent isolation.

Host launch commands publish before Server startup; embedded scripts publish in
their foreground workload scope. Servers, including guests, withdraw their own
launch on final exit, on success or failure. Host stop/release and later host
observation provide fallback cleanup. Guest withdrawal leaves sandbox resource
release to host lifecycle management.

## Atomic retirement

There are two operations: an owner finalizes its own launch; an observer repairs
a launch from external evidence. Both use registry primitives under the same
guards. Never infer ownership of a selected launch merely from its home.

1. Read one instance entry and its exact reference; probe outside locks.
2. For observer repair, acquire the registry guard, then the status guard,
   nonblocking. Under both, reread the authoritative entry and reference. Require
   the entry to equal the snapshot used for the death/finish decision, including
   resource and management-owner bindings, and the reference to match its host
   status path/launch. launch_id equality alone is insufficient.
3. If evidence changed, make no repair or withdrawal. Discard the obsolete result;
   reread once without repair, then return unknown if it changes again. Never
   report a definitive departure using the rejected probe.
4. Otherwise conditionally mark an active orphan stopped, preserving recorded
   failure, then withdraw the matching reference while still holding both guards.
   Retain resource recovery references, homes, State, and execution history.
5. If locks are busy but the reread still matches the death evidence, exclude the
   confirmed-finished nonresident and defer writes. A changed snapshot requires
   re-observation instead. Report cleanup deferral separately from membership.

Acquire registry before status whenever both are needed; code holding a status
guard must release it before acquiring registry. Once either is held, other lock
attempts are nonblocking and no probe, HTTP, preparation, or sandbox action runs.

A terminal launch cannot become active again. Server binding and publication of
starting/running must check under the status guard that the same launch remains
in preparing/starting/running before admitting execution. A delayed child must
abort if an observer retired its launch; a new start requires a new launch_id.
This covers both orderings: Server updates before repair, or retirement wins first.

Owner finalization verifies its own launch/workload identity, marks it terminal,
then withdraws only its matching reference under the ordered guards. An old owner
cannot finalize a replacement. If a reference already points to an obsolete
launch, retire that reference conditionally under the guards without changing the
new launch's state. Do not delete unreadable/unverifiable entries as though dead.

## Shared-lock recovery

Upgrade register.lock from plain flock to existing
`file_write_lock(shared_owner=..., owner_dead=...)`.
A claim records location, PID/creation time, and acquisition time. A guest claim
also requires launch_id, canonical control identity, and sandbox/ref; a host claim
may precede launch allocation. Reuse the existing owner vocabulary and capture
it before deletion. It must identify the owner after registration/status removal.
Acquisition time is diagnostic, not an expiry deadline.

The registry lock helper owns recovery; Watcher/CLI callers do not implement
their own reapers:

| Reaper | Claim it may recover |
| --- | --- |
| Host | Host owner proven dead by PID/creation time; guest owner only after its identity-bound sandbox is proven absent. |
| Guest | Dead guest owner in its own exact launch/resource namespace; never a host or another guest namespace. |

Keep these existing namespace restrictions: native flock coordinates competing
reapers within a kernel; a host may reap a guest claim only when that guest
namespace is gone. Live or unverifiable owners remain busy. Never reclaim by
age. A host may recover a legacy empty register.lock only through the primitive's
native reaper lock; a guest must not recover an empty legacy guard.

Read the claim and resolve owner liveness before acquiring either mutation guard.
Reuse a matching, verified probe when available; otherwise allow one bounded
recovery probe even if that owner is absent from the inventory. Pass only a
decision matching the exact observed claim to the primitive's conditional reaper.
A replacement claim cannot be reaped using the previous owner's evidence.

Observers try guards without waiting and defer busy work. Registry publication
has a two-second total acquisition/recovery deadline, shortened by any remaining
startup deadline; unresolved contention is an explicit busy failure. Within one
observation refresh, allow one additional recovery probe with a two-second
timeout, shared across that refresh's repairs. Other repair attempts defer.
Guest exit may defer an unverifiable host-owned claim to a later host observer.

This handles the critical failure point: a guest deletes its registration and is
killed before releasing register.lock; a host later recovers the claim from its
embedded identity without the deleted registration.

## Cost and responsibility boundaries

`top` keeps its existing five-second background discovery cadence and one
discovery job in flight. Render and Run/usage ticks do not trigger discovery or
cleanup. Reuse each sweep's status snapshots, eight-worker bounded liveness
probes, and Docker batch results. Ordinary retirement adds no second scan or
probe; the exceptional lock-recovery probe follows the explicit budget above.
Compute membership from all observations, then attempt at most eight stale
repairs per collection refresh. A long-lived watcher rotates candidates so a
busy home cannot starve the rest; never reuse old probe evidence on the next
refresh. Budget exhaustion defers writes, not confirmed departures or inventory
publication. Info has only its one selected repair candidate.

Write only changed records; cleanup never blocks the UI or prolongs a safely
confirmed departure's statistical contribution. Collection commands never
prepare sources or refresh Setup/State.

RuntimeWatcher owns classification, membership, and repair decisions.
up.registry owns publication, ordered compare/withdrawal, and registry guard
recovery using existing up.instance/common.files primitives. Workload/server
lifecycle owns launch transitions and finalization; sandbox hosting owns mounts
and process/container release. Keep parsing, static Watchers, and stats with their
existing packages.

## Implementation touchpoints and acceptance

Main paths: [scope](../../src/toolang/up/scope.py) / the renamed watcher,
[observation](../../src/toolang/up/observation.py),
[types](../../src/toolang/up/types.py),
[process](../../src/toolang/up/process.py),
[registry](../../src/toolang/up/registry.py),
[instance files](../../src/toolang/up/instance.py),
[workload](../../src/toolang/up/workload.py),
[server](../../src/toolang/up/server.py), and
[sandbox](../../src/toolang/up/sandbox.py).
Info also touches [source preparation](../../src/toolang/up/source.py),
[CLI selection](../../src/toolang/cli/common/execution.py),
[agent commands](../../src/toolang/cli/toolang/commands/agent.py),
[SetupWatcher](../../src/toolang/setup/watcher.py), and
[StateWatcher](../../src/toolang/state/watcher.py).
Update existing top schemas/activity handling for phase and coverage rules.

| Scenario | Pass condition |
| --- | --- |
| Residents and departures | Inactive/failed residents remain; confirmed nonresident departure removes agents/usage/runs together, including after forced death. |
| Phases and coverage | Terminal workloads give zero active roots without rewriting unfinished Runs; startup/unknown does not fabricate zero. Resident uncertainty, bad driver metadata, and deferred cleanup affect only relevant totals. |
| Partial discovery | A damaged peer does not retain a confirmed-departed peer; incomplete enumeration preserves only unresolved coverage. |
| Single-home info | All selectors resolve/prepare through Watchers; no inventory scan, registration, executor, or Run database. Rejected preparation preserves known runtime status and never fails/retires the selected launch. |
| Same-launch binding race | With subprocess barriers, Server binding before repair invalidates the old probe; repair first prevents the delayed Server from entering execution. |
| Owner exit and replacement | Normal host/guest exit withdraws the expected launch; old-owner exit and concurrent cleaners cannot change a replacement or its reference. |
| Orphaned shared claim | Kill a guard owner after withdrawal but before unlock; recover without its registration/status. A live, unverifiable, or replacement claim is not reaped; publishers fail busy within the deadline. |
| Guest mount | Guest withdrawal is host-visible; paths/permissions and shared locking work across the real host/guest boundary. |
| Refresh cost and retained data | One discovery at the existing cadence; no rendering probes or unchanged writes; at most eight repair attempts and one bounded exceptional recovery probe per collection refresh. More than eight stale entries leave scope immediately; rotating attempts progress past busy homes. Cleanup preserves State, recovery references, and history. |

Extend the existing process, registry, runtime-observation, scope/top-observer,
agent-info, hub, mount, shared-files, and sandbox lifecycle tests. Keep the default
suite offline; exercise the actual mount/kernel boundary in opt-in Docker tests
and report platform coverage. Run default checks for implementation; update
current designs and generate its changelog through
`too aid.too update_changelog`.

## Compatibility and remaining review

These semantics intentionally replace retained source-free info from
[#883](https://github.com/openhat-ai/toolang/pull/883) /
[#884](https://github.com/openhat-ai/toolang/pull/884), info's existing-home-only
behavior from [#919](https://github.com/openhat-ai/toolang/pull/919), and read-only
top discovery from [#897](https://github.com/openhat-ai/toolang/pull/897).
Execution-history commands retain their separate contracts.

Implementation review must verify nested lock ordering and startup rejection
before execution, shared-mount hard-link/ownership behavior, and namespace-aware
claim recovery on supported macOS/Linux hosts. Writable registry-directory access
remains an explicit sandbox capability. No product implementation or approval
of the earlier info-only patch is implied.
