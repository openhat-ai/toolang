# Agent observation across `info`, `list`, `hub`, and `top`

Status: Proposed on 2026-10-23. The user specified the command semantics,
Watcher ownership, and host/guest cleanup requirements below; implementation
details await review. Definition only; no product code changes.
Audit baseline: `origin/main` at `e88d0539`.

## Goal and scope

Give all four commands one understandable agent-membership and running-status
contract. Reuse existing discovery, Watchers, identity checks, and shared-file
primitives. Success means consistent observations, useful prepared agent details,
and recovery after a process exits without withdrawing its registration.

Scope includes agent discovery, lifecycle observation, registration withdrawal,
the required sandbox mount, and command integration. Preserve command names,
flags, execution-store formats, Run/usage semantics, and hub-service observation.
Do not add a daemon, heartbeat, expiry timer, history index, or duplicate scanner.

## Command semantics

| Command | Membership | Result |
| --- | --- | --- |
| `list` | Created resident homes in the selected Toolang root, including inactive residents. | Resident agents and their running status. |
| `hub` | Those residents plus active visiting/roaming instances registered with this root. | Running status and the matching launch's Talk driver selection. |
| `top` | The same membership as `hub`. | Agent counts, usage in `--period`, all active root Runs regardless of age, and completed root Runs in the independent `--recent` window. |
| `info AGENT` | One home resolved from a name, URL, ref, path, or exact instance. | Placement, identity/home, prepared Agent Setup and Agent State summaries, and running status. |

Counts distinguish full instance addresses, not unique short names. Stopped
residents retain their usage/history. When a visiting/roaming instance departs,
its counts, usage, active Runs, and recent Runs leave the collection together,
even if the usage/completion windows have not expired. Explicit `info` can
inspect a known nonresident home without including it in collection commands.
Keep the existing optional resident-scoped `top` behavior.

## Current implementation and gaps

| Area | Current implementation | Assessment |
| --- | --- | --- |
| `list` | `agent_layouts()` → `AgentProcess.list()` → `status()`. | Resident membership matches. The status implementation mixes probing, readiness, and repair. |
| `hub` | `runtime_snapshot()` → `runtime_inventory()`, active references only. | Omits inactive residents. Reduces starting/preparing to unknown and actual failure to not running. |
| `top` | `ScopeReader` combines resident homes and registered instances; `TopObserver` owns collection scheduling. | Scope and departure aggregation mostly match. Read-only inventory retains known-finished nonresidents; unregistered resident status is synthesized rather than inspected. |
| `info` | Existing-home selection, direct process status, live resource HTTP requests or saved offline counts/State. | Does not prepare fresh sources or use the Setup/State Watchers. Missing offline values, omitted placement, redundant identity, and repeated runtime reads do not meet the specified detail contract. |
| Cleanup | Normal workload exit, sandbox management, status reconciliation, and inventory filtering each handle part of retirement. | Generation checks exist, but cleanup policy is fragmented; unreleased sandbox references can keep dead instances registered. Guest exit does not withdraw the host reference. |

Six isolated reproductions confirmed: a never-started resident is absent from
hub; failed unregistered residents lose their phase in top; top retains a
finished visitor's registration; a dead visitor with a recovery reference remains
registered after both views; cached and uncached observations disagree on a
starting launch; corrupt runtime JSON becomes Agent failure. The existing 326
focused tests pass, so several mismatches are established behavior rather than
uncovered implementation accidents.

Relevant owners:

- [Resident discovery](../../src/toolang/up/discovery.py),
  [single-instance status](../../src/toolang/up/process.py),
  [runtime observation](../../src/toolang/up/observation.py), and
  [scope composition](../../src/toolang/up/scope.py).
- [SetupWatcher](../../src/toolang/setup/watcher.py),
  [StateWatcher](../../src/toolang/state/watcher.py), and
  [agent commands](../../src/toolang/cli/toolang/commands/agent.py).
- [Registration](../../src/toolang/up/registry.py),
  [workload lifecycle](../../src/toolang/up/workload.py),
  [server shutdown](../../src/toolang/up/server.py),
  [sandbox hosting](../../src/toolang/up/sandbox.py), and
  [shared-file locking](../../src/toolang/common/files.py).

## Simple design rules

### 1. One observation owner, explicit scopes

Rename/generalize the existing `ScopeReader` to `RuntimeWatcher`, consolidating
the discovery/probe/repair pipeline rather than wrapping competing paths with
another facade. Its `refresh()` returns a `RuntimeInventory`; `current()` returns
the latest publication.

Expose `runtime_status(layout)` from the same module for one selected home. It
uses the watcher's per-instance observation and repair implementation without
discovery or inventory construction. Info calls this function directly: it needs
neither a `RuntimeWatcher` instance nor a `RuntimeInventory`.

`list` selects residents; hub and root top select residents plus active
nonresidents. Probe resident homes even without
registrations. A registration is a discovery candidate, not proof of activity.
Preparing/starting instances with verified live owners are active candidates too.

Keep `RuntimeInventory` as the result/binding container rather than adding a
second equivalent snapshot type. Reconcile `AgentStatus` and `RuntimeInstance`
around one lifecycle classification. Keep `TopObserver`: its responsibility is
Run/usage scheduling, not a second instance observer. Fold away redundant
`ScopeReader` logic and duplicate registry scans as callers migrate.

### 2. One lifecycle classification from the same evidence

Use preparing, starting, running, stopped, failed, and unknown. Render stopped
as "not running". A matching failure record means failed; an inspection failure
means unknown with a diagnostic. Driver metadata affects driver information only.
Read status, launch identity, endpoint, and drivers from one validated generation.

Basic observation verifies workload identity/liveness; readiness requests belong
to startup/attachment. Cache reuse may delay freshness within the existing bound,
but must not switch readiness policy. Retain bounded probes, eight-worker
batching, Docker batching, and generation rechecks after slow work.

A discovery failure cannot prove departure. Preserve uncertain bindings with
diagnostics; an unrelated damaged peer must not prevent a confirmed departure.
Diagnostic text alone must not control membership or turn known totals unknown.
Exclude a confirmed-dead nonresident immediately even if durable cleanup must be
retried; retain a resident row with its observed phase.

### 3. Prepare details through the existing Watchers

For info, resolve/materialize the selected home through the existing source
owner, then refresh `SetupWatcher` and `StateWatcher` once. Derive resource
summaries from those returned objects and their public methods: tools/models
from Agent Setup; caps, chores, and workspaces from Agent State. No CLI source
scanning, independent configuration merging, direct State preparation, or saved
startup-count substitute. No watcher loops, executor, or execution database are
needed. Use `validate_defaults=False` for inspection.

Keep exact-instance and latest-retained selection when applicable; a fresh
path/ref/URL may prepare its source home. Inspection must not allocate/register
a running workload or become visible as an active nonresident. Do not change
the source-free contracts of execution-history commands.

Use this static-detail path whether or not an instance is running; execution-time
overrides are not silently presented as current authored Setup/State. Add
Placement and preserve the full nonresident address in the title, removing the
duplicate Agent row. Keep `--catalog` as the inspection Setup input.

If preparation fails, still observe/report the known identity and running status,
then report preparation diagnostics and a nonzero exit. Watcher last-valid
publications must be labelled as such when refresh was rejected, never passed off
as a successful current preparation.

### 4. Registration is shared across processes and namespaces

Keep the current reference payload: format, canonical host status path, and
`launch_id`. The publisher need not be the eventual cleaner:

| Path | Publisher | Normal withdrawal | Recovery |
| --- | --- | --- | --- |
| Host-launched Server | Host launch command, before workload startup. | Server on final exit, or host stop/release. | A later host observer. |
| Direct host serve | Serving process. | Serving process. | A later host observer. |
| Embedded script | Script CLI, including its preparation stage. | Foreground workload scope. | A later host observer. |
| Sandbox Server | Host launch command. | Guest Server on final exit, or host stop/release. | A later host observer using sandbox identity/liveness. |

Route every publication/withdrawal through `up.registry`. Preserve host
status paths as identity values; pass the guest its mounted registry location and
the expected host reference explicitly. Never reconstruct host paths from guest
layout paths or interpret guest PIDs in the host namespace.

To keep the flat registry format, mount only the selected root's active-registry
directory read/write at an explicit guest control path, not the whole Toolang
root. Directory access is required for unlinking entries; binding just the JSON
file is insufficient. This grants directory write access, not filesystem-enforced
per-agent isolation; protocol operations target only their expected reference.

All publishers and cleaners must use the same shared-filesystem guard. Existing
registry locking uses plain `flock`, which is insufficient across Docker
Desktop kernels. Reuse `file_write_lock(shared_owner=...)` and its namespace-aware
dead-owner rules; do not invent another lock protocol. Never hold the guard
during probing, preparation, HTTP, or sandbox operations. Preserve lock ordering.

Under the guard, reread and delete only the expected status-path/launch pair.
Repeated cleanup succeeds harmlessly; delayed cleanup cannot delete a replacement
launch. A mismatched old reference may be retired without editing a newer status
record. Mount construction and Docker guest environment forwarding must carry
the binding; graceful guest exit must use it on both success and failure.

### 5. Repair discovery, preserve resources and history

Normal exit withdraws promptly. `RuntimeWatcher` and `runtime_status(layout)`
perform bounded, best-effort repair for their observed candidates. Info performs
its single-home observation even when static preparation fails.

Retire only a reference proven obsolete, or a workload proven finished with no
live management/preparation owner. Missing source files, health-request failure,
timeout, permission error, or an unverifiable owner are not proof of death.
Recheck generation before repairing authoritative status or withdrawing a reference.

Keep cleanup decisions in the observation owner, reference compare/delete in
registry, and process/container release in sandbox lifecycle management. Removing
an expired active reference must not require deleting recovery references,
containers, homes, prepared State, or Run/usage history. Failure to acquire a
cleanup guard defers the write to a later observation; it does not invent activity.

`top` reuses the existing background discovery job and five-second discovery
cadence, with only one discovery job in flight. Rendering and Run/usage polling
do not trigger registry scans or repairs. Each sweep reuses its validated
snapshots and liveness results to identify stale candidates; cleanup adds no
second discovery, HTTP request, or sandbox probe. Acquire repair guards
nonblocking, mutate only changed records, and retry deferred cleanup on later
discovery refreshes. Keep UI and stats reads independent of repair completion;
exclude confirmed-departed instances even when their reference cannot yet be
removed. Collection observation never refreshes Setup/State or prepares sources.

## Implementation touchpoints and acceptance

Likely changes: `up.scope`/the renamed watcher, `up.observation`,
`up.process`, `up.types`, `up.registry`, workload/server finalization,
sandbox mount/binding assembly, and the four command consumers. Keep parsing,
Setup/State preparation, and stats calculations with their existing owners.

Clean names while consolidating: remove descriptions restricting shared
observation to top or hub, tests claiming failed residents remain while asserting an empty
roster, and obsolete identity-label fallbacks where no current caller requires
them. Use canonical addresses. Preserve external wire identities and documented
compatibility rather than renaming public fields incidentally.

| Scenario | Pass condition |
| --- | --- |
| Never-started, stopped, and failed residents | All collection commands retain them; lifecycle observations agree; stopped history remains available to top. |
| Active visiting/roaming membership | Include live preparing/starting/running instances. On confirmed departure, remove their complete statistical contribution at the next discovery refresh. |
| Identical evidence and degraded peers | All views classify consistently; cache does not select health policy; unknown differs from failure; one bad peer cannot freeze confirmed departures. |
| Watcher-based info | Names, refs, URLs, paths, and exact instances resolve correctly; changed inputs reach Watcher results; no custom scanners, active registration, executor, or Run database is created. |
| Single-home runtime status | Info obtains status and repairs only its selected binding through the public module function; constructing a RuntimeWatcher, enumerating residents/registry, or constructing a RuntimeInventory is forbidden. |
| Preparation rejection | Known identity/status survives with diagnostics; last-valid State is explicit; unavailable default credentials do not block inspection solely through execution validation. |
| Normal host/guest exit and forced death | Both can withdraw their own reference; missing finalization is repaired by a later host info/list/hub/top observer without releasing unrelated resources. |
| Concurrent cleanup and restart | Real subprocess barriers exercise two cleaners, old/new launches, and stale references. Neither cleanup nor status repair changes the replacement generation. |
| Mounted guest cleanup | Verify directory mounting, explicit host/guest binding, shared guard use, read/write permissions, and host-visible deletion; a container restart cannot let an old guest remove the new launch. |
| Bounded cost and retained data | Busy cleanup locks defer without hanging the UI; no periodic historical nonresident scan; cleanup preserves homes, State, recovery references, and Run history. |
| Refresh cost in `top` | Repeated rendering and Run/usage ticks launch no discovery or cleanup; at most one discovery runs at the existing cadence. Unchanged records cause no writes; cleanup adds no probes; lock contention does not block the UI or retain confirmed-departed statistical contributions. |

Extend the existing process, registry, runtime-observation, scope, top-observer,
agent-info, hub, mount, and sandbox lifecycle tests. Keep deterministic tests
offline; verify the real host/guest mount and locking boundary in the opt-in
Docker lifecycle suite and report platform coverage honestly. Run default code
checks for implementation; update current designs and generate its changelog via
`too aid.too update_changelog` before implementation handoff.

## Historical change and risk

[#884](https://github.com/openhat-ai/toolang/pull/884) followed
[#883](https://github.com/openhat-ai/toolang/pull/883)'s retained, source-free
inspection contract and removed resident discovery from the shared inventory.
[#919](https://github.com/openhat-ai/toolang/pull/919) made info an existing-home
query; [#897](https://github.com/openhat-ai/toolang/pull/897) required read-only top
discovery. The user-specified semantics here intentionally replace those rules
for these observation commands; history commands remain separate.

The deliberate compatibility changes are prepared info instead of live/saved
resource switching, inactive residents in hub, and top's best-effort registration
repair. Writable registry-directory exposure and actual shared-filesystem locking
are part of the sandbox contract and require explicit verification. No product
implementation or approval of the earlier info-only patch is implied.
