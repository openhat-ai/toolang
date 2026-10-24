# Roaming script HOMEs and foreground Docker shutdown

Status: proposed for review. This is a feature definition, not an implementation.

## Goal and scope

Separate a roaming agent's durable HOME from each script invocation. Users can
create a new HOME or explicitly reuse one, inspect the latest HOME, and run
different HOMEs concurrently on host and Docker. All HOMEs are isolated by the
selected canonical ROOT. Runtime resources follow their foreground owner.

Success requires consistent HOME selection, one script owner per HOME, and the
same execution cancellation/persistence contract for host Ctrl-C, Docker Ctrl-C
and Docker owner loss. Each script uses fixed startup program/configuration
snapshots while ordinary workspace I/O remains live. A container disappearing
alone is insufficient evidence of correct cleanup.

Include shared foreground Docker ownership for scripts and existing resident/
visiting `serve --sandbox docker`. Roaming exposes no Chat, start, stop or serve.
Preserve resident/visiting HOME selection, persistent background start, automatic
client/work retention, authored run/spawn semantics and existing history formats
unless owner provenance needed for safe recovery requires an additive field.
Do not implement Chat attachment, owner transfer or background roaming execution.

## HOME selection and preparation

Use `too`, the alias for `toolang`. Script syntax is
`too [run] OPERAND [RUNNABLE] ...`; optional `run` preserves existing command
precedence and default-entry behavior. `info` accepts both existing command orders.
The table defines proposed behavior, including currently unsupported syntax.

| Local source operand | Script execution | `info` |
| --- | --- | --- |
| `aid.too@P19` | Execute in that existing HOME; missing fails. | Inspect that existing HOME; missing fails. |
| `aid.too@` | Execute in the latest existing HOME; none fails. | Inspect the latest existing HOME; none fails. |
| `aid.too` | Allocate and prepare a fresh HOME, then execute. | Select the latest HOME; if none exists, allocate and prepare one, then inspect. |

Both `@` forms require an existing HOME and never fall back to creation. P19 here
stands for the complete roaming place code. Match canonical source and ROOT;
reject wrong-source/root codes, malformed identities and uncertain ownership.
Empty trailing `@` means latest, not an incomplete code. Preserve remote revisions,
filenames containing `@` before `.too`, and existing named instance addresses.
Parse selectors before resolving files or generating runnable parameters.

“Latest” means existing monotonic P19 creation order for this source and ROOT.
Runtime status, age, completion and reuse do not reorder HOMEs. Resolve once and
pin the target. Serialize latest-or-prepare info so concurrent first calls create
one HOME; standalone bare-source scripts always allocate independently.

All roaming HOMEs use the existing [identity encoding](../design/agent-addresses.md):

```text
SOURCE_DIR/.toolang/ROOT_CODE/P19/agents/NAME/
```

Remove unnumbered roaming HOME creation/selection at
`SOURCE_DIR/.toolang/agents/NAME`; preserve existing data without automatic adoption
or deletion. Update every call site that materializes such a HOME. Commands that
already prepare a HOME use ROOT-scoped latest-or-prepare resolution; historical
readers that require existing storage retain that prerequisite. Source-only
commands retain their operand restrictions. Shared source coordination locks are
not agent HOMEs and retain their existing role.

Preparation creates durable identity, source/configuration projection and State,
without an executor, Run or active runtime registration. Show prepared/not running.
A failed preparation remains diagnosable and retryable in that HOME. A later
explicit `@` script may use it; a later bare script still creates another HOME.

`info` may prepare resource metadata but never starts an executor, renews runtime
ownership or modifies the active generation's bindings. Distinguish configured
host resources from verified effective guest resources; report unavailable facts
and preparation errors without hiding readable identity/status or changing target.
Reject roaming Chat/hosting and `--chat` continuations before allocation or the
associated history mutation. Other historical inspection/offline edits retain
their existing contracts.

## Script ownership and invocation context

One script owns a HOME at a time. Concurrent execution against a preparing,
running or closing owner fails as busy; no queuing, attachment, takeover or new
HOME fallback. Info remains available. Claim a released HOME atomically with a
fresh launch ID and Run identity; preserve its P19, history and lab. Uncertain
old resources prevent a new claim. Nested Runs do not allocate HOMEs.

Resolve workspace, workdir, environment grants, sandbox and development artifact
at each script command boundary, including explicit reuse. Omitted options use
current normal configuration/defaults, without restoring previous temporary
bindings. A stopped HOME may execute on host and later in Docker after verified
release. Different HOMEs may execute simultaneously with different grants.

Each HOME's `agent.too` still refers to the same canonical source; overlapping
workspace grants can expose shared writable code/data. HOMEs are not source
snapshots or independent workspace security boundaries. Preserve explicit-grant
precedence, paths containing spaces, read-only inputs, writable lab/status,
environment/credential policy and developer-wheel identity.

Keep canonical host ROOT/source identity across guest mount translation. Preserve
shared lock semantics and [host/guest status ownership](../design/hosting.md#runtime-recovery-and-logs):
host owns global active registration and staged inputs; guest owns its workload
status and execution writes. No writable global registry mount is added.

## Script startup snapshots

Use the existing `SetupWatcher.refresh()` and `StateWatcher.refresh()` during
startup preparation, after HOME and invocation options are resolved. Freeze the
resulting Setup/State before admitting the first Run. Do not start their `run()`
monitoring loops for host scripts or script-created Docker guest servers; no new
parallel loader abstraction is needed. Watcher objects may remain as one-shot
loaders, but execution getters must return the captured publications.

The snapshot covers parsed `.too` programs and imports, authored capability
sources, effective configuration, environment/grant bindings and Toolang-managed
plugin/tool configuration and loaded implementations. Materialize required
runtime resources from those captured inputs before execution. Root, child and
spawned Runs, runnable discovery and same-Run handoffs use that invocation's
publications. Disable live source-sync callbacks in script mode so an on-demand
refresh cannot bypass the snapshot boundary. An external `info` may update
prepared metadata without changing the active script's in-memory publications.

Reusing a HOME starts a new invocation and refreshes current authored inputs and
configuration again. Preserve old history/lab, not its previous runtime snapshot.
Invalid new inputs must fail preparation with diagnostics rather than silently
execute a last-valid cached revision. Source edits during a running script may
become effective only in the next invocation.

Ordinary workspace reads/writes remain live, including shell commands or Python
programs reading/executing files there. This does not copy or freeze the workspace,
container image filesystem or arbitrary files loaded by external programs. Those
live I/O results are expected. Owner EOF detection, cancellation controls,
execution event streams and persistence continue running; they are not source
watchers. Select the startup-snapshot policy explicitly at script call sites and
carry it through host/guest startup, independently of driver count or foreground
lifetime. This source-loading change applies only to script execution.

## Common foreground Docker ownership

A script CLI owns its invocation; foreground serve owns its server. Both use one
Docker foreground lifecycle mechanism, separate from background start and
visiting automatic idle retirement. Accepted work cannot retain a foreground
runtime after its owner is lost. Host scripts may keep the embedded executor;
no managed host server or permanent supervisor is introduced.

Prefer attached, interactive Docker stdin without a TTY as a dedicated owner
channel. The host frontend exclusively owns the pipe's write end; closing it or
killing that frontend produces guest EOF. Guest bootstrap/server watches EOF
independently of model/tool execution. Isolate the control stream from terminal
input, script inputs and tool descriptors. Keep Docker helpers outside the
terminal signal group so host Ctrl-C can request cancellation before dropping
the channel; helpers/children must not inherit a write end that masks owner death.

The channel must cover container creation, bootstrap, API readiness, execution
and shutdown, including death before readiness. Keep source/startup diagnostics,
stdout/stderr and launch/resource identity checks. A dropped HTTP/SSE result stream
alone does not end ownership. Definitive owner-channel loss does; do not reconnect
to a replacement or resubmit uncertain work.

Current Docker launches use detach plus logs/wait, which does not provide this
owner channel. Native Docker 29.4.0 probes on macOS verified EOF after frontend or
Docker-client SIGKILL with foreground `-i`; `-d -i` plus later attach did not have
the same closure behavior. Applications ignoring EOF continued running. Reuse
[Docker's attached-stdin semantics](https://github.com/docker/cli/blob/v29.4.0/cli/command/container/opts.go#L724),
then verify actual Toolang integration on Linux and Docker Desktop. These native
fixture probes do not establish RunStore or cancellation correctness.

No periodic heartbeat is included initially. Scope the owner-loss guarantee to
local Docker Engine/Desktop connections. Remote-daemon network blackholes and
half-open transports have no new bounded-detection guarantee; remote lease
fallback is a separate definition if required.

## Cancellation, records and shutdown ordering

Use host script Ctrl-C as the common semantic contract, verifying and correcting
its implementation rather than assuming it is already complete. Route Docker
Ctrl-C and guest owner EOF through the same domain cancellation/finalization
operations. Reuse execution controls, terminal projection and store transactions;
keep transport detection outside execution persistence. Preserve trigger-specific
reasons: `script interrupted` for Ctrl-C, `foreground owner channel closed` for
EOF. EOF establishes channel loss, not verified host PID death.

One idempotent, launch-fenced closing transition performs this order:

1. Close admissions and persist the shutdown reason and affected invocation/owned
   roots while storage is open. Racing signals, EOF and result completion must
   not duplicate cancellation or overwrite a terminal result.
2. Cancel unfinished owned roots, descendants, independent spawns and tool
   subprocesses using existing ownership semantics. Await their cancellation and
   terminal Run/Step/control records, preserving committed output, usage and
   errors. Already completed Runs/Steps keep their result; queued work is not
   executed merely to drain shutdown.
3. Finish execution resources and stream delivery, close providers/tools/watchers
   and durable storage, and persist final workload status. Records must remain
   readable after process/container exit. Never close the store while a task may
   still write it, or mark runtime stopped before finalization completes.

Normally, host waits for the persisted script result, requests graceful guest
shutdown, keeps the owner channel until closing is acknowledged, waits for
container exit, releases host registration/staging and returns the script result.
On Ctrl-C, return existing interruption exit code 130 after cleanup. A SIGKILLed
host cannot return a result; guest still performs the same execution finalization.
Do not force-remove the container as the first cleanup action.

Use a shared ten-second finalization budget, aligned with an explicit Docker stop
grace of ten seconds. Repeated interruption and stuck tasks/tools/storage must
not hang indefinitely. Force termination only after the grace; preserve committed
shutdown intent/diagnostics and report incomplete finalization honestly. Recovery
may repair records only after verified owner/resource death and proven generation
ownership; never rewrite completed results or invent missing output. A forced
exit cannot guarantee complete terminal records when storage is unavailable.

Persist host PID/creation time and exact immutable container ID/labels for
recovery; guest PID is not host liveness evidence. Fence cleanup by full HOME,
launch and resource identity. Docker exit removal preserves mounted HOME/history/
lab. If host died, a later observation may withdraw stale active references and
release staging after verifying death. Preserve unknown resources. Delayed old
cleanup cannot release a later invocation in the same HOME.

## Implementation touchpoints

- `up/{allocation,identity,source,records,registry,workload}.py`, `common/layout.py`:
  ROOT-scoped preparation separate from active execution and fenced HOME claims.
- `cli/toolang/{routing,commands/script,commands/agent}.py`, CLI context/argument
  resolution, `up/addresses.py`: three operands, optional-run routing and concrete
  HOME intent. Audit other preparation call sites; reject unsupported Chat/hosting
  and history `--chat` paths early.
- `up/{lifecycle,server,sandbox,core}.py`, Docker sandbox/CLI/bootstrap and script/
  foreground serve call sites: common owner channel, signal ordering and deadlines.
- Script/guest startup, `setup/watcher.py`, `state/watcher.py` and executor snapshot/
  sync bindings: reuse one-shot refresh, omit source-monitor tasks, freeze getters
  and preserve runtime control loops.
- Execution cancellation, tool runtime, owned-run bookkeeping and store/events:
  reasoned cancellation, durable ordering and generation-proven recovery. Keep
  parsing, orchestration, execution and persistence with their owning concepts.
- Update CLI/help and current [CLI](../design/cli.md), [scripts](../design/scripts.md),
  [Chat](../design/chat.md), addresses and hosting docs when implemented. Follow
  the prescribed changelog workflow for user-visible compatibility changes.

## Acceptance and verification

| Scenario | Pass condition |
| --- | --- |
| Three operands with optional run and both info orders | Match the selection table; missing explicit targets create nothing; invalid arguments fail before preparation. |
| First/repeated/concurrent info and preparation failure | One ROOT-scoped P19, retryable metadata, no Run/server/active reference or unnumbered HOME. |
| Bare script after info; explicit reuse and concurrent claim | Bare gets a new HOME; reuse keeps P19/history/lab with new launch/Run; one owner wins and the other reports busy. |
| Latest ordering, same-name sources, symlinks and multiple ROOTs | Select by canonical source/ROOT and creation order; never skip newer stopped/prepared targets or cross identities. |
| Host/guest simultaneous HOMEs and sequential reuse | Correct per-invocation workspace/workdir/env/sandbox/dev bindings; grants and source-sharing behavior remain explicit. |
| Edit source/config/imported code during a gated host or guest script | Existing and later child/spawn/exec operations use the original program/configuration; no source-watcher loop or post-admission source refresh starts. |
| Info/source-sync requests during script execution | Prepared cache changes cannot replace active snapshots; live source sync is unavailable; cancellation and result streams still work. |
| Reuse the same HOME after source/config edits | A new invocation adopts valid changes and preserves old history/lab; invalid changes fail without running a stale cached revision. |
| Modify an ordinary workspace file during execution | Later filesystem/tool reads observe current content; script source/configuration stays fixed. |
| Info during execution and unsupported commands | Observation cannot change/prolong ownership; Chat/hosting/continuations reject before writes; existing background/automatic policies remain valid. |
| Real CLI host Ctrl-C, Docker Ctrl-C and Docker owner EOF | Equivalent cancellation, persisted records and cleanup invariants across model wait, shell/file tools, nested/spawned work, pending work and terminal races. |
| RunStore reopened after process/container exit | Valid Run/Step/control terminals and reasons, committed output/usage/errors retained, no unfinished owned execution after graceful close, inspect/stats usable. |
| Docker script and foreground serve owner/client SIGKILL | Shared behavior before readiness and during work; no helper retains ownership; records commit before guest exit. |
| Normal completion and Ctrl-C ordering | Container/resources finish before the live host CLI returns; correct script result or exit 130; no stale active/staging data on normal close. |
| Slow cancellation, repeated signals, storage failure, ten-second grace | Bounded termination and honest incomplete-finalization diagnostics; no false success or cleanup of a replacement launch. |
| Lost HTTP stream, descriptor leakage and stale recovery | HTTP loss alone preserves work; owner EOF ends it; late recovery is generation-fenced and preserves history/lab. |

Default tests remain offline and deterministic, using synchronized cancellation
points and controlled deadlines. Add real CLI subprocess/PTY tests for actual
SIGINT delivery, not only coroutine cancellation or mocked remote clients. Run
bounded actual-container cases on Linux and Docker Desktop with deterministic
provider/tool fixtures and normal product execution paths. Include read-only
inputs, spaces, environment grants and custom toolchains; no live provider is
required. Run the default verification for implementation changes.

## Risks and deferred work

Roaming Chat removal and info-created HOMEs are user-visible compatibility
changes. Explicit reuse makes launch/resource fences essential. EOF ownership
requires changing the current detached foreground adapter and careful descriptor/
signal isolation. Disabling monitoring alone is insufficient if lazy source-sync
or getter paths still adopt newer publications. Force deadlines trade complete
records for bounded exit under faults; expose that limit instead of claiming
guaranteed persistence. Legacy
unnumbered HOME migration, roaming Chat and remote-daemon lease fallback are
explicitly deferred; there are no remaining decisions inside this scope.
