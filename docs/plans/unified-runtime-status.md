# Unified runtime status

Approved in #804. Consolidate lifecycle files without adding universal
health/metrics endpoints. Host `serve` executes in one process.

## Decisions

- `CliContext.root` is user-selected `toolang_root`; `AgentLayout.root` is
  `agent_root`. Resident roots coincide; visiting roots derive from Toolang root
  and source; roaming roots derive from project directory. Remove observation
  root and its environment variable; retain `TOOLANG_ROOT` meaning Toolang root.
- Keep one `agent_root/agents/<name>/.runtime/status.json` and a transient `.lock`.
  Each execution-instance entry has a launch ID and two owned sections:
  `control` stores intent, complete recovery refs, startup bindings, and last
  successful settings; `workload` stores namespace-local PID/creation time,
  lifecycle state, timestamps, and errors. Remove independent sandbox state
  files/locks.
- Both host and guest workloads update their section through `up/records.py`.
  Every writer locks, rereads, validates launch ID, patches owned fields, and
  atomically replaces the file. Only successful control-side resource release
  clears refs; old workload writes cannot affect a new launch.
- Discovery remains `toolang_root/.runtime/instances/<name>-<placement>-<id>.json`, with
  version, absolute status path, and launch ID. Keep stable instance identities;
  database/logs stay in agent root. No `ids.json`, `channels/`, or new directories.
  References share one native `instances/registry.lock`; temporary executions
  never leave one lock per instance. Queries do not create this guard.
- Resolve placement into concrete `ServeSpec` inputs at the control boundary:
  paths, drivers, config/dotenv sources, catalogs, authorized source-write targets,
  and teaming membership. Preserve loading/watching. `_serve` receives runtime
  inputs, sandbox, location, and launch ID, without placement decisions.
- Defer universal health/metrics services and TCP/Unix-socket transport changes.
  Keep chat-specific API availability conditional on the chat driver and retain
  existing generic execution APIs. Preserve public command targeting/flags,
  independent script isolation, Hub activity, and offline history.

## Restart, retention, and mounts

- Persistent server port precedence: explicit inputs > last successful
  host-facing port > automatic allocation. Resident inputs retain CLI >
  `TOOLANG_AGENT_PORT` > current config; other placements retain CLI-only inputs.
  Remembered ports must be
  available and respect existing reservations and the Hub port exclusion; if
  unavailable, allocate another. Explicit/configured ports fail when binding
  fails. Isolated temporary script invocations still allocate temporary ports.
  Apply reuse only to existing API listeners; it stabilizes client URLs, not
  runtime identity, and does not require enabling another service.
- Sandbox precedence: CLI > current config binding > last successful selector >
  `host`. Reuse only the selector, with current plugin config and a fresh launch;
  never reuse a container ID, process, old credentials, or one-off workspace/dev
  options. An unavailable selected sandbox fails without falling back to host.
  This adds a historical fallback to today's config-or-host default.
- Store the successful port/endpoint and sandbox selector in `control` only after
  readiness. Failed attempts cannot overwrite them. Successful stop retains
  `status.json`, these settings, final lifecycle/timestamps/errors,
  launch ID, and database/log history. Confirmed exit clears the active PID;
  successful resource release clears operation ownership, recovery refs, and the
  matching discovery reference. Failed cleanup retains refs for recovery. Retained
  endpoints are not advertised as live; queries preserve settings and history.
- Host execution needs no mounts. Docker mounts the target
  `agent_root/agents/<name>/` read-write, including `.runtime/` for shared status,
  lock, database sidecars, and logs. Add required root inputs/catalogs and
  authorized workspaces only; retain existing source-write permissions. Mount
  bootstrap scripts/materials read-only. Do not mount entire roots or Toolang
  discovery references. Remembered settings need no separate mount. Directory
  mounts support atomic status replacement.

## Existing names and command boundaries

`AgentProcess` is the shared control/query entry, using existing `up/sandbox.py`
helpers and `Sandbox` plugins. `AgentCore` executes the workload; hosting lifecycle
hooks in `up/server.py` update its status. Launcher is a startup phase, not another
class/service. `RuntimeRegistration` publishes references; `RuntimeObservation`
and `ActivityReader` retain file/database-based observation.

Counts below cover Toolang processes, excluding Docker daemons/helpers, threads,
and tool subprocesses. `_serve` is already counted by its launching command.

| Command | Processes | Reads / writes |
| --- | --- | --- |
| Host `start` | 2 at startup; 1 after return | Config/status; resident-only control writes and child workload execution. |
| Docker `start` | 1 host + 1 guest; guest remains | Host owns control/reference; guest owns workload status and database/logs. |
| Host `serve` | 1 throughout | Control initialization then workload in-process; no agent child. |
| Docker `serve` | 1 host + 1 guest | Host controls/follows; guest executes; entry scripts use `exec`. |
| `_serve` | 1 workload | Explicit inputs/config/status/database; workload-section writes. Direct host invocation also performs control bootstrap. |
| `stop` | 1 command + target until exit | Resident-only; read saved refs, stop/release, then finalize control/reference. |
| `top` | 1 additional | Read references/status/database/Hub; correct confirmed-dead metadata only. |
| `list` | 1 additional | Read installed residents/config/status; include stopped agents; confirmed-dead correction only. |
| `info` | 1 additional | Read status/config and existing online APIs or offline resources; confirmed-dead correction and existing caches allowed. |

## Recovery and query cost

- Save intent and operation-owner identity before creation; publish its reference
  next. Workloads publish local PID/creation time before execution. Host control
  verifies host identity to bind/recover refs; Docker intent saves a recoverable
  container locator. Guest PIDs never authorize host signals.
- Use short locked updates and operation ownership to prevent competing starts;
  never hold the lock across creation, readiness, execution, or shutdown. Claim
  `status.json.lock` by atomically linking a complete writer identity; release
  only the owned inode. Host/guest kernel locks alone did not coordinate on the
  tested macOS Docker mount. The lock path stays fixed, but its inode is transient.
  Guest updates and claim checks read temporary hard-link snapshots to avoid
  stale file-name attributes after host replacement on shared mounts.
  Reap only a confirmed-dead native owner or guest launch; native locks serialize
  reapers in their own kernel. Guests cannot infer host PID liveness. Queries may
  remove a dead host writer's claim so a live guest can resume its own updates.
- Live workloads own normal status updates. Queries correct only confirmed exited,
  zombie, or identity-replaced workloads; unknown probes and unbound/active
  startup operations are not deaths. Probe outside the lock, then nonblockingly
  reread and compare launch/ref/state/operation before correction. Retain recovery
  refs and guard reference removal; no query resource release or history edits.
- Later control operations recover interrupted work idempotently. Neither launcher
  exit nor missing shutdown handlers establish workload death. Detection time
  cannot stand in for actual exit time/cause. Skip writes on contention/failure.
  Recover unbound host children by launch ID; a still-pending Docker creation
  helper prevents declaring its missing container dead or releasing the intent.
- `list`/`info` inspect relevant instances once. `top` separates repaint from
  liveness checks, caching at most one second per launch/ref with record-change
  invalidation. Bound backend timeouts/concurrency; batch Docker probes. Unchanged
  polls make no metadata writes or persistent probe-cache files.

## Implementation and acceptance

1. Update [layout](../../src/toolang/common/layout.py), `ServeSpec`, CLI inputs,
   and [server](../../src/toolang/up/server.py) for in-process host `serve`.
2. Consolidate [records](../../src/toolang/up/records.py) and
   [lifecycle helpers](../../src/toolang/up/sandbox.py); make host/guest section
   updates share lock/merge/replace logic and resolve remembered settings at the
   control boundary. Preserve host ownership/permissions on replacement.
3. Convert [registrations](../../src/toolang/up/observation.py) to references,
   update readers and nonblocking lock support, and add tests/changelog coverage.

Acceptance: verify process counts, section ownership, concurrent merges, and
stale-writer rejection. Kill controllers/workloads before binding, readiness,
execution, and release; subsequent operations must recover without duplicate
starts, false liveness, deadlocks, PID-reuse signals, or old cleanup affecting new
launches. Live/unknown workloads remain unchanged; dead correction happens once.
Preserve placement behavior, script isolation, and command scopes. Validate shared
locks, atomic replacement, permissions, and database sidecars with Docker. Check
restart precedence, remembered-port conflicts, unavailable sandbox failure,
failed-start history retention, and repeated stop/reconciliation. Verify guest
access to required inputs and runtime data without mounting discovery references.
Run Ruff checks/formatting, `ty check`, and the default offline pytest suite.

## Compatibility and trust

- **Shared-file trust:** use cooperative guest writes. Section
  ownership is an application contract, not filesystem isolation; arbitrary guest
  code can change the file. Strong isolation requires host-only refs or a broker.
- **Compatibility:** stop old workloads with the previous version before upgrading.
  Stopped legacy reports migrate; leftover sandbox refs require old-version
  cleanup. Keep databases/logs. Visiting roots now include the Toolang root in
  their source hash; preserve old roots for explicit history migration. For
  rollback, stop new workloads first and restore backed-up legacy status/roots.
  Replace observation-root settings with `TOOLANG_ROOT`. Unified health/metrics
  transport remains a future plan.
  After stopping old versions, orphan discovery `<id>.json.lock` files may be
  removed; new versions never create them or prune locks of a live old version.
