# Unified agent instances

Status: feature definition for human review; no implementation before approval.
Replaces the observation-only approach in [draft #882](https://github.com/openhat-ai/toolang/pull/882).
Current behavior remains documented in [Layout](../design/layout.md),
[Hosting](../design/hosting.md), [Scripts](../design/scripts.md),
[CLI](../design/cli.md), and [Top](../design/top.md).

## Goal and scope

Give each executing instance a clear identity, agent root, and executor. Top
must discover active instances through one selected Toolang root and obtain live
activity from the shared Agent API, including concurrent host/guest executions
of one `.too` file. Preserve offline inspection, fork/rewind, and isolated replay.

Scope includes identity/layout, startup compatibility, ports/lifecycle,
discovery, instance selection, offline history, replay ownership, and API grouping.
Exclude agent-root deletion, background stale-file collection, general history
merging, new transports/services, language changes, command/flag removal, and
changes to HTTP API methods, paths, query/response schemas, or versions.

## Roots and identity

| Term | Meaning |
| --- | --- |
| `toolang_root` | The one selected global root; owns discovery and Hub data |
| `agent_root` | Placement-calculated root of an agent's materialized layout |
| `agent_home` | `<agent_root>/agents/<agent_name>`; program, configuration, State, and runtime data |
| `source_dir` | Original local `.too` file's real parent directory |

Use these terms explicitly, avoiding "agent directory". Resolve Toolang root
through the existing CLI/environment contract; never substitute an agent root
for global discovery or Hub ownership. Existing source/teaming policies remain.

| Placement | Agent name | Instance ID | Instance name | Agent root |
| --- | --- | --- | --- | --- |
| Resident | `gus` | None | `gus` | `<toolang_root>` |
| Visiting | `lex` | `v<source_id>` | `lex@v<source_id>` | `/tmp/lex@v<source_id>` |
| Roaming | `aid` | `r` followed by 32 hex characters | `aid@<instance_id>` | `<source_dir>/.toolang/aid@<instance_id>` |

Keep agent-name derivation. Visiting `source_id` retains the current 16 lowercase
hex SHA-256 prefix over canonical Toolang root, NUL, and canonical Source Ref
with existing alias normalization. Source contents, port, and sandbox do not
change it. Resident/visiting instance identity and root survive restarts.

`instance_id` is the source-scoped selector; `instance_name` identifies its
owner within a Toolang root. Keep existing `launch_id` as a private fresh UUID4
runtime-generation token for every placement. It fences callbacks, API requests,
status writes, and registration removal; it determines no agent root, instance
name, selection order, or `-i` value. Keep existing status/reference/profile
fields and launch headers, including generation-scoped bootstrap directories.
Pass generation and control ownership explicitly through launch/bootstrap;
a missing token or layout flag must not imply ownership or allocate another ID.

Make roaming instance IDs sortable and path-bound:

```text
instance_id = "r" + hex16(request_order_ns) + SHA256(canonical_source_path_utf8 + NUL + fresh_uuid4_bytes).hexdigest()[:16]
```

`hex16` is zero-padded lowercase hex; the full ID has 33 characters. Resolve the
original absolute `.too` path once on the host, after basic CLI/source validation
but before preparation; never hash a per-instance or guest-mapped path.

Under `<source_dir>/.toolang/.state/launch.lock`, assign
`request_order_ns = max(time_ns(), highest_retained_order_ns + 1)` and publish
initial identity/status before releasing this short source-cache claim. Use
validated published roaming identities, excluding hidden staging. No bootstrap,
State preparation, or database copying runs under it. This handles concurrent
requests/clock rollback within the cache without a separate persistent counter.
Preserve observed UTC request time for display; compare full IDs lexically
for selection. Completion, readiness, and mtime never change instance order.

Use `(toolang_root, instance_name)` as the runtime owner key. Within Top, use
full instance names for identity/labels, including its existing opaque JSON
`identity` string. Source hashes alone would merge parallel roaming executors.
Keep wire fields/types; the nullable `runtime_instance` field can carry an
instance ID without selecting a storage subdirectory or local status entry.

## Uniform storage and retention

Placement changes root calculation and policy inputs. Below agent root, all
placements use the same paths:

```text
<agent_root>/
  .state/root/
  .sandbox/<agent_name>/launches/<launch_id>/
  agents/<agent_name>/                 # agent_home
    agent.too
    config.toml
    psyches/  skills/  services/  prompts/  chores/
    .state/
    .runtime/
      status.json
      runs.db
      stats.db
      agent.log
      chat-input.jsonl
      logs/
      tools/
```

`AgentLayout` carries `instance_id` and derives `instance_name`; runtime path
builders do not branch on it. Remove the extra execution-instance runtime and
sandbox directory layers. All status readers/writers use the `default` entry.
Update identity, temporary-owner recovery, and finalization together; merely
clearing the old `AgentLayout.instance` directory flag is insufficient.

Keep status/reference envelope version 3. Add mandatory control bindings for
identity format version 1, instance ID/name, canonical host source, roaming
request order, and startup contract version 1. Host-owned bindings survive
cooperative guest workload merges. Temporary ownership uses saved controller
identity and lifetime policy, independently of paths. Preserve existing shared-file claims, atomic
replacement, launch fencing, and guest mounts of agent home/required inputs.

Stage a new roaming root beside its final path and publish it under the source
claim only after initial default status is complete, without replacing another
root. Hidden staging is not selectable. Materialize inputs/prepare State after
publication so failed attempts remain addressable; publish global references
only after local identity exists.

For `/work/demo/aid.too`, status paths include:

```text
/Users/bryan/.toolang/agents/gus/.runtime/status.json
/tmp/lex@v<source_id>/agents/lex/.runtime/status.json
/work/demo/.toolang/aid@<instance_id_a>/agents/aid/.runtime/status.json
/work/demo/.toolang/aid@<instance_id_b>/agents/aid/.runtime/status.json
```

Retain every published agent root/home after exit, stop, startup failure, and
failed replay. Release process/container/bootstrap resources and matching global
registrations; retain history, State, and successful settings. Ordinary roaming
instances share no status/store/tool room/prepared State. Replay copies described
below are independent snapshots, not shared writable history.

Preserve project configuration discovery, dotenv precedence, source links,
workspaces, watchers, and authorized edits. Shared project-file edits retain
`<source_dir>/.toolang/.state/root/edit.lock`, passed explicitly to projection/
editing callers. Per-instance guards cannot serialize the same project file.
This parent `.toolang` is project storage, not another Toolang root/agent root.

## Discovery and activity

Enumerate only `<toolang_root>/.runtime/instances/<instance_name>.json`:

```text
<toolang_root>/.runtime/instances/
  gus.json
  lex@v<source_id>.json
  aid@<instance_id_a>.json
  aid@<instance_id_b>.json
  register.lock
```

Retain lightweight references containing version, absolute local status path,
and `launch_id`. Local status is the lifecycle authority. Validate saved owner,
root, and generation. Share this reader across Top, runtime/Hub enumeration,
and port reservations; do not supplement it with resident/temporary/project
scans. `too list` retains installed-agent semantics. Scoped resident Top excludes
same-named visiting/roaming sources.

Publish preparing/starting instances and retain stopping entries while resources
remain active. Normal finalized stop/failure removes only the matching generation's
reference under the existing short registry lock. Never hold that lock through
startup/wait/shutdown. For an automatic guest exiting without a host controller,
stopped local status permits targeted removal by the next host registry read.
Retain bounded PID/creation-time or sandbox probes and unknown diagnostics;
add no sweeper/daemon or root deletion.

Global instance info files are disposable discovery metadata. Removing one
cannot lose history or prevent direct inspection, and does not prove workload
death. A live instance with a lost reference is undiscoverable to Top until host
control republishes it; history queries never recreate it.

Top membership/counts reflect active registrations, independently of active root
Run counts. A fresh session does not scan stopped roots for membership/history;
a running session retains observed historical sources for its existing windows.
Preserve pinned snapshots, completion pagination, generation fencing, and source
Stats independence. Missing coverage remains unknown; healthy executing scripts
provide live roots/rows instead of unknown activity from an absent listener.

## Executor reuse and runtime lifecycle

Resident/visiting reuse one compatible active executor per instance name.
Normalize complete effective startup bindings before attachment, including CLI,
environment, configuration, remembered values, and defaults. Persist contract
version 1 using canonical host-side values; compare without preparing State,
pulling images, allocating another listener, or rewriting inputs.

Compare source/root/input policy; sandbox selector/configuration; bind/advertised
endpoint; drivers; temporary workspace bindings/initial workdir; model catalog;
development artifact path/content digest; ceiling/default/limit/compact overrides;
executor logging; and effective Toolang startup options/dotenv source bindings.
Ignore unrelated shell/session variables. Fingerprint sensitive values; report
mismatched field names without secrets. Equal normalized explicit/implicit values
reuse; differences reject without mutating/restarting/duplicating the executor.
Do not freeze every watched file's contents. Existing command-specific driver
default differences remain conflicts under this strict rule; callers must align
`--enable` rather than silently adopting the running set.

Keep mutable retention separate from immutable executor bindings. Attachment
does not reset lifetime to cold-start defaults or promote it. Existing explicit
`start` promotion preserves automatic-to-persistent transition and rejects
foreground detachment. Keep leases/closing admission in `ServerLifecycle`.
Launcher output destinations/client presentation are not executor parameters;
Run input, thread selection, and request-scoped model options may differ.

Serialize competing starts through existing management ownership. Compatible
callers share the starting generation; reject incompatible/legacy unverifiable
contracts. Release short guards before waiting. Explicit controls attach using
saved bindings/generation, without cold-start defaults or a replacement owner.

Every new roaming source execution creates its own instance/executor and may use
different host/guest parameters. Use shared AgentCore, application factory, and
server lifecycle. Foreground host script/API run in one workload process/event
loop with one executor/RunStore; guest uses existing sandbox/API execution. No
Top-only app or separate observation endpoint. Preserve output, exit/errors,
signals, and driver gating; ordinary scripts enable no background drivers.

The foreground caller owns roaming lifetime. Completion/interruption closes
admission, cancels/joins owned work under existing shutdown policy, closes the
core once, and finalizes after resource release. Controls do not transfer lifetime
ownership. Detached/guest bootstrap receives explicit control/workload ownership.

| Placement | Automatic host-facing port |
| --- | --- |
| Resident | Last successful available port, otherwise `7002–7999` |
| Visiting | Last successful available port, otherwise OS-assigned |
| Roaming | Fresh OS-assigned allocation per new instance |

Preserve current explicit port precedence (resident CLI/environment/config;
other placements CLI), Hub port exclusion, and active reservations. An automatic
attachment resolves to the active endpoint before comparison; do not probe its
listener as a free port. Save successful settings only after readiness.

Closing a port probe does not reserve it. Host listeners retain the bound socket;
sandbox plugins publish the actual endpoint. Backends unable to reserve through
bootstrap get at most three automatic bind attempts, releasing failed resources
first. Retain instance ID/order but renew `launch_id` for each replacement
workload, fencing delayed callbacks. Explicit-port conflicts fail without retry.
Never advertise a tentative endpoint or execute after unrecoverable startup/bind
failure. OS allocation can reuse a port across non-overlapping instances.

## Selection and offline operations

Use existing targets with `--instance`, alias `-i`, on existing-instance commands:

| Target | Default | Explicit instance ID |
| --- | --- | --- |
| `gus` | Stable resident home | None; no additional resident ID |
| `https://toolang.ai/lex.too` | Stable URL-derived home | Matching `v<source_id>` |
| `/work/demo/aid.too` | Latest requested instance | Full `instance_id`, including `r` |

```sh
too gus info
too https://toolang.ai/lex.too info
too /work/demo/aid.too info
too /work/demo/aid.too inspect -i r18e0b5f817d00000e2c7a0f23d51a9c6 runs
```

Require the complete instance ID; reject missing prefixes, launch IDs, duplicate
options, or mismatched/unknown selectors without fallback. Reuse command parameter factories;
do not reinterpret authored input or runnable flags. Add no decorated source or
agent-root-directory target syntax.

`inspect` never reads global instance info. Calculate stable roots from target
bindings, or roaming root as `<source_dir>/.toolang/<agent_name>@<instance_id>`.
Without `-i`, select the maximum valid local instance ID for the canonical source,
including preparing/running/failed/stopped instances. Validate provenance to
separate different filenames normalizing to one agent name. Corrupt/ambiguous
metadata fails rather than guessing an older instance. No existing instance
means a diagnostic without materialization; source contents need not remain
readable for retained-history inspection.

Latest means last requested: A starts, B starts, A finishes, and B stays latest.
Touches, completion, and history edits cannot change it. Resolve once and pin
root, instance identity, and selected launch generation through each command's
store/API operations; a concurrent newer instance cannot retarget the command.

| Operation | Selected live owner | Selected exited owner |
| --- | --- | --- |
| `info`, `inspect` | Saved records/existing read APIs | Retained status/State/history |
| `steer`, `cancel` | Guarded attachment only | Report exited; no replacement |
| `fork`, `rewind` | Existing guarded API | Direct history edit |
| Roaming `rerun` | Snapshot and execute in fresh instance | Same isolated replay path |
| Resident/visiting `rerun` | Compatible shared executor | New generation in stable home |

Historical reads must bypass `_local_resources` rematerialization, State rebuild,
and visiting refetch. Preserve source-authoring command policies separately.
Queries create no executor/root/registration. New roaming script, `chat`, and
`serve` entrypoints create fresh instances; `-i` cannot revive a retained root.

Offline writes require conclusively stopped native/sandbox ownership, verified
from local saved references; missing global info is insufficient. Claim existing
local management ownership, recheck generation/resources/operations, then call
`ThreadManager` with its SQLite transactions. Unknown/starting/stopping state
rejects offline writes. Preserve anchor, idle, expected-head, and durable-control
semantics without forging terminal Run/Step facts or constructing AgentCore.
Resident/visiting startup honors the same maintenance claim.

## Isolated replay and continuation

Roaming `rerun` selects a source instance; destination identity/root/port/executor
are always new. Take a consistent full `runs.db` snapshot using SQLite backup,
not raw database/WAL copying. Resolve terminal source Run and control head in
that sealed snapshot, preserving existing selected-thread terminal/idle rules.
Live-source changes after capture cannot change it. Copying complete execution
history retains typed/content references, compaction horizons, thread views, and
allocator high-water marks without a federated-store layer.

Seed the destination with that snapshot and required immutable State objects.
Prepare current project Setup/State and use existing rerun resolution, captured
input/model/runnable semantics, and current CLI overrides. Revalidate workdir/
authority under current grants. Do not copy dotenv/bootstrap credentials,
mutable tool/workspace data, lifecycle records, or `stats.db`. Source data stays
unchanged; copied coordination receipts confer no active subscription/lease.

Persist imported Run ownership plus a replay receipt linking source root,
instance, Run/thread control head, request ID, and accepted destination Run.
Mark all copied Runs/children imported, retaining original owners through
repeated replay. Record acceptance/provenance atomically through existing request
IDs. Imported facts remain inspectable, not live work or steer/cancel targets;
exclude them from recovery/driver activation and emit no fresh lifecycle events
for them. Their historical state is not rewritten merely because they were copied.

Recent activity/completions/usage and archival Stats exclude imported facts
from destination counters; new work counts once under its new owner. Do not
infer ownership from timestamps, row order, or ID coincidence. Add execution-owned
metadata tables in RunStore schema 63; provide atomic additive 62-to-63 migration
preserving facts/controls/allocators and read-only support for both versions.
Schema 62 has no imports; other unsupported versions fail without changes.
Guest bootstrap verifies required runtime/store support before opening a store.

Install the seed only after migration, ownership, and receipt preparation commit;
no workload may open a partial seed. Failure/cancellation retains a failed
destination root and intact source. This is snapshot-based replay, not general
store merging. `fork --chat`/`rewind --chat` first commit their edit; roaming then
snapshots the edited view into a fresh Chat instance and reports its ID.
Chat failure does not undo that edit. Resident/visiting continue in stable homes.

## API and implementation touchpoints

Rename OpenAPI group `top` to `activity`, including internal router vocabulary.
Keep `GET /api/v0/top/snapshot` and all existing method/path/query/schema/version
contracts. Groups `agent`, `stats`, `runs`, `threads`, `caps`, and `chores` remain;
`agent` already exists without an `/agent` URL prefix. Add no new prefix.

| Area | Likely files and responsibility |
| --- | --- |
| Identity/inputs | [layout](../../src/toolang/common/layout.py), [source](../../src/toolang/up/source.py), config-source and projection callers; explicit instance/generation/guard inputs |
| Acquisition/hosting | [agent_server](../../src/toolang/cli/common/agent_server.py), [sandbox](../../src/toolang/up/sandbox.py), [server](../../src/toolang/up/server.py), core and sandbox plugins; compatibility, owned async lifespan, binding/recovery |
| Discovery | [instance](../../src/toolang/up/instance.py), [process](../../src/toolang/up/process.py), [observation](../../src/toolang/up/observation.py), [Top](../../src/toolang/up/top.py); default status entry and full owner keys |
| CLI | [routing](../../src/toolang/cli/toolang/routing.py), common parameters/context/execution, script and thread commands; selection mode before materialization |
| History/replay | [ThreadManager](../../src/toolang/execution/threads.py), [RunStore](../../src/toolang/execution/store.py), execution-owned snapshot/provenance helpers, Stats readers/service; migration and ownership |
| API/docs | App/router imports and activity tag; update current designs/changelog only with implementation |

Keep parsing/schema packages independent of runtime services. Reuse factories
and entrypoints. Generate implementation changelog through `too aid.too update_changelog`.

## Acceptance and verification

Use offline deterministic gates, temporary roots, and fake sandbox plugins.

| Scenario | Pass condition |
| --- | --- |
| Identity/layout | All relative paths match; no nested execution layer; canonical path hashing, sortable IDs, clock rollback, and guest identity mapping are correct |
| Parallel roaming | One source with distinct host/guest parameters yields independent stores, full Top owner keys/rows, and cancellation ownership |
| Startup reuse | Equal explicit/inferred bindings reuse; implicit differences reject without mutation; command driver defaults and mutable promotion/leases follow their contracts |
| Generation races | Competing starts share one compatible owner; old readiness/requests/writes/removal cannot affect a replacement |
| Lifecycle/ports | One host workload executor; output/errors/signals preserved; socket reservation, actual endpoint publication, bounded automatic retries, and explicit failure work |
| Discovery/retention | Only global active references supply Top; scoped names do not merge; index removal loses no history; all published roots survive terminal states |
| Selection races | `-i`/`--instance` calculate roots without global info; latest includes failure/exit; pinned selection survives newer launches, touches, and edits |
| Read-only/partial state | Queries create no State/store/root/registration or refetch; partial staging stays invisible; malformed/unknown bindings never fall back |
| Offline edits | Existing fork/rewind results persist without executor; live/unknown/restarting ownership cannot enter offline writes |
| Replay semantics | Snapshot retains references, State, views, model/input semantics, and allocator continuity; source remains intact and new work has fresh ownership |
| Replay accounting | Imported facts count in no destination live/completion/usage/archive totals, including repeated replay; imported live-looking rows accept no controls |
| Failure/continuation | Partial seed cannot execute; failed roots persist; Chat failure cannot undo a committed fork/rewind |
| Compatibility | Schema 62 read-only/migration to 63 preserves data; unsupported guests/stores fail clearly; HTTP contracts/cursors/errors/gating remain and tags use `activity` |
| Shared inputs | Concurrent project edits retain one source-owned guard while each instance keeps its own runtime state |

Extend existing layout, acquisition, routing/thread, store, Stats, observation,
Top API, and sandbox tests. Real Docker mount/status validation uses existing
opt-in integration checks; default tests must remain offline.

Before implementation commits, run `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.
This definition requires source/link/example verification and `git diff --check`.

## Upgrade, risks, and approval

Stop old workloads with the old version; never adopt an unverifiable live launch.
Preserve stopped resident settings and execution data through supported migration.
Visiting uses the new directory spelling and may choose a new first port.
Leave old visiting roots, shared roaming roots, and nested execution histories
untouched for explicit migration; ordinary execution does not import them.
Rollback requires stopping new workloads; old writers cannot open schema 63.

Strict startup matching can reject previously accepted defaults. Fresh roots
increase preparation cost; replay snapshots and permanent root retention increase
disk use. No root cleanup is authorized. OS temporary-directory cleanup can
still remove visiting data; cooperative guest-status trust remains unchanged.

No design questions remain pending in this draft. Human confirmation of this
complete proposal is required before implementation; earlier approval of #882
does not approve this replacement.
