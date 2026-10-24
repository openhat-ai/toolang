# Roaming script HOMEs and foreground Docker shutdown

Status: proposed for review; definition only, no implementation.

## Goal and scope

Separate durable roaming HOMEs from script invocations: select or create a HOME,
allow one script owner per HOME, and let different HOMEs run on host and Docker.
Success requires ROOT isolation, preserved source evolution, and equivalent
cancellation/record finalization for host Ctrl-C, Docker Ctrl-C and owner loss.
Container exit alone does not prove successful finalization.

Share owner detection and shutdown with resident/visiting foreground
`serve --sandbox docker`. Preserve their HOME selection, background start and
automatic client/work retention. Roaming has no Chat, start, stop or serve;
Chat attachment, owner transfer and background roaming execution are out of scope.

## HOME selection

Use `too` (the `toolang` alias). Script syntax is
`too [run] OPERAND [RUNNABLE] ...`; preserve optional-run routing, default entries
and both existing `info` command orders. This table includes proposed syntax:

| Local source operand | Script execution | `info` |
| --- | --- | --- |
| `aid.too@P19` | Run in that existing HOME; missing fails. | Inspect that existing HOME; missing fails. |
| `aid.too@` | Run in the latest existing HOME; none fails. | Inspect the latest existing HOME; none fails. |
| `aid.too` | Create a fresh HOME and run. | Inspect the latest HOME; if none exists, prepare one. |

P19 denotes the complete [roaming place code](../design/agent-addresses.md#short-codes).
Resolve once by canonical source and ROOT, validating full ownership bindings;
wrong-source/root codes, malformed identities or uncertain ownership fail.
“Latest” means monotonic HOME creation order, unaffected by age, runtime status
or reuse. Never fall back to an older HOME or create one for an explicit `@`.
Parse selectors before file/runnable resolution; preserve remote revisions,
filenames containing `@` before `.too`, and existing named instance addresses.

Roaming HOMEs use `SOURCE_DIR/.toolang/ROOT_CODE/P19/agents/NAME/`. Remove creation and
selection of `SOURCE_DIR/.toolang/agents/NAME`, preserving old data without automatic
adoption/deletion. Other preparation call sites use ROOT-scoped resolution while
retaining source-only and history operand requirements. Shared source edit locks
remain shared across HOMEs and ROOTs.

Serialize first-info allocation/preparation so concurrent calls produce one HOME;
bare scripts allocate independently. Preparation records identity, projects inputs
and prepares State, with no executor, Run or active registration. Show prepared/not
running. Failed preparation remains diagnosable and retryable in the same HOME.
An explicit `@` can subsequently run there; a bare script still creates another.

While a HOME is owned, `info` reads identity/status and available runtime metadata;
it must not reproject source/config/catalog inputs, replace invocation workspace
bindings or renew ownership. Mark unavailable guest facts rather than presenting
host configuration as verified guest resources. Preparation errors remain visible
alongside readable identity/status; never silently select a different HOME.

## Invocation ownership and inputs

Claim a HOME atomically from preparation through final cleanup. A preparing,
running, closing or unverifiable owner means busy: no queue, attach, takeover or
fresh-HOME fallback. Each invocation has a new launch ID and Run identity; reuse
retains P19/history/lab. Apply this gate to CLI execution including `rerun`, while
preserving its history target/replay contract. Observation and live Run controls
remain available; authored child/spawn semantics do not acquire another CLI owner.
Reject roaming Chat/hosting and history `--chat` before allocation/history mutation.

Resolve workspace/workdir, environment grants, sandbox and development artifact
per invocation. Omitted options use current defaults, not previous temporary
bindings. After verified release, the same HOME may switch host/guest; different
HOMEs can run concurrently with different grants. Preserve grant precedence,
spaces, read-only inputs, writable lab/status and credential/development policies.

HOMEs share the canonical source and may share writable workspaces; they are not
source checkouts or filesystem isolation boundaries. Guest paths are translated
views of the same canonical host ROOT/source/HOME identity, never inputs for
recomputing a different identity. Under existing
[status ownership](../design/hosting.md#runtime-recovery-and-logs), the host owns
global active registration/staging, and the guest owns workload status/execution
writes. Do not add a writable global registry mount.

## Watching and evolution

Keep existing monitoring and explicit State sync for scripts, resident and visiting
agents on host/Docker, including current invalid-publication and visiting-source
acquisition rules. Host scripts monitor State; hosted servers monitor State and
Setup. `too grow.too` must be able to edit source and use a valid new publication
in subsequent calls during the same invocation. Edits require existing workspace
authority; watching grants no write access. Workspace I/O remains live.

Preserve [State capture](../design/execution.md#state-capture) and
[Setup publication](../design/setup.md#publication-and-lifetime): accepted Runs and
inline plans retain code; new named children/exec can adopt the latest valid State;
Model Calls use existing resource-capture boundaries. Keep validation, authority,
prepared frames and recorded revisions intact. Environment, installed resources
and startup mounts/drivers/ports retain existing restart/capture limits. Reuse
prepares current inputs while preserving history/lab. This plan changes no watch
policy and does not resolve existing host/guest configuration-projection differences.

## Foreground Docker owner channel

The script CLI owns its invocation; foreground serve owns its server. Work cannot
retain either runtime after owner loss. Host scripts may remain embedded; no
permanent supervisor or new host server is required.

Use attached Docker stdin (`-i`, no TTY) as a dedicated owner channel, with the host
frontend exclusively holding the pipe's write end. Guest bootstrap/server detects
EOF independently of model/tool execution, from bootstrap through shutdown.
Keep script/terminal input separate; helpers/children must not inherit a writer
that hides owner death. Isolate Docker helpers from terminal signals so Ctrl-C
can request cancellation before the owner channel drops. Keep diagnostics/output
and launch/resource verification. HTTP/SSE loss alone does not end ownership;
owner-channel loss does, without reconnecting or resubmitting uncertain work.

Replace the current detach-plus-logs/wait path. Native Docker probes support
[attached-stdin closure](https://github.com/docker/cli/blob/v29.4.0/cli/command/container/opts.go#L724),
not detached launch followed by attach; they do not prove Toolang finalization.
The initial guarantee covers local Docker Engine/Desktop. No periodic heartbeat
or bounded remote-daemon blackhole/half-open detection is introduced.

## Finalization and recovery

Use host script Ctrl-C as the cancellation/persistence contract, verifying rather
than assuming its completeness. Docker Ctrl-C and EOF use the same domain cleanup.
Record `script interrupted` for script Ctrl-C and `foreground owner channel closed`
for EOF; preserve serve's interruption semantics. EOF proves channel loss, not
host PID death. One idempotent closing transition, fenced by HOME/launch/resource:

1. Close admissions and persist shutdown intent/reason while storage is open.
2. Cancel unfinished owned roots, descendants, independent spawns and tool
   subprocesses. Await terminal Run/Step/control records; preserve completed
   results and committed output/usage/errors. Do not execute queued work to drain it.
3. Stop writers, watchers/providers/tools and streams, persist final workload
   status, then close storage. Never mark stopped or close the store while writers
   can still run. Records must remain readable after process/container exit.

Normally: persist script result → graceful guest finalization → container exit →
host registration/staging release → CLI return. Keep the owner channel until
closing is acknowledged. Ctrl-C returns 130 after cleanup; host SIGKILL has no
return path but still triggers guest cleanup. Never begin with force removal.

Use one ten-second finalization deadline from closing; signals/retries do not
reset it. The guest enforces its own deadline after EOF even when the host/Docker
client is gone; enforcement must not depend on cooperative cancellation of the
work being stopped. A live host bounds waiting/escalation too; align Docker stop
grace to the same deadline, not an additional ten seconds. After expiry, force
only the owned resources and report incomplete finalization honestly. Storage
failure/forced exit cannot guarantee terminal records.

Persist host PID/creation time and immutable container ID/labels. Before HOME
reuse or stale-record repair, verify old resource death and full generation
ownership; unknown resources block reuse. A later host observation may withdraw
stale active references/staging. Guest PID is not host liveness evidence; delayed
cleanup must not release a replacement launch. Preserve HOME/history/lab and
completed results; never invent lost output during recovery.

## Implementation touchpoints

- `up/{allocation,identity,source,records,registry,workload}.py`, `common/layout.py`:
  ROOT-scoped preparation, durable identity, exclusive claims and recovery.
- CLI routing/context, `commands/{script,agent,thread}.py`, `up/addresses.py`:
  selectors, observing info, rerun admission and unsupported-command rejection.
- `up/{lifecycle,server,sandbox,core}.py`, Docker `_cli`, adapter/bootstrap, execution
  cancellation/store/tool ownership: owner channel, deadlines, durable teardown
  and watcher shutdown; retain parsing/orchestration/execution ownership boundaries.
- Update CLI/help, addresses, scripts, Chat and hosting design docs when implemented;
  follow the prescribed changelog workflow. Add provenance fields only as needed,
  preserving existing history formats/contracts.

## Acceptance and verification

| Scenario | Pass condition |
| --- | --- |
| Three selectors, optional run, both info orders | Selection table holds; missing explicit targets/invalid arguments create nothing. |
| First/repeated/concurrent info; preparation failure | One ROOT-scoped HOME, retryable diagnostics, no Run/runtime/active registration or unnumbered HOME. |
| Latest ordering, same-name/symlink sources, multiple ROOTs | Canonical source/ROOT and creation order select one target without fallback/cross-identity access. |
| Bare run after info; reuse; concurrent script/rerun | Correct fresh/reused HOME; one owner wins, others report busy; history target remains fixed. |
| Host/guest parallel HOMEs and sequential reuse | Stable identity, current per-invocation grants/settings, preserved history/lab. |
| Info during owned execution; rejected Chat/hosting/continuations | No input projection, ownership renewal, binding change or rejected-command mutation. |
| Authorized self-edit via normal tools on host/Docker, using watch and explicit sync | Later named child/exec adopts new valid code; accepted code/inline plans stay bound; revisions recorded; missing grants/read-only paths still deny writes. |
| Model-frame changes, invalid edits, resident/visiting watch | Existing capture/validation/authority behavior retained; ordinary workspace reads see current files. |
| Real host/Docker Ctrl-C; owner/client SIGKILL before readiness and during work | Equivalent cleanup invariants across model/tool waits, child/spawn/queued work and terminal races; no leaked owner descriptor. |
| Reopen RunStore after graceful exit | Valid terminal Run/Step/control records/reasons, preserved output/usage/errors; inspect/stats usable. |
| Normal completion and interruption ordering | Container exits before CLI returns; expected result/130; no active/staging residue. |
| Stuck work, repeated signals, storage failure, owner already dead | Deadline works without a surviving host, incomplete finalization reported, no false success. |
| Lost HTTP stream, stale recovery and replacement race | HTTP loss preserves work; EOF ends it; verified recovery cannot release a replacement or delete history. |

Keep default tests offline/deterministic with synchronized execution points. Use
real CLI subprocess/PTY signals and bounded containers on Linux/Docker Desktop,
normal product tool execution and deterministic provider fixtures. Cover spaces,
read-only inputs, environment grants and custom toolchains. Run default checks
for implementation; native owner-channel fixtures alone are insufficient.

## Risks and exclusions

Roaming Chat removal/info-created HOMEs change public behavior. Descriptor/signal
handling, guest deadline enforcement and generation fencing require integration
coverage. Force deadlines limit persistence under faults. Legacy HOME migration,
roaming Chat, configuration-projection parity and remote-daemon leases are deferred.
