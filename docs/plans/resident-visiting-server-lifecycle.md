# Resident and visiting Chat server lifecycle

Status: Approved in [#814](https://github.com/openhat-ai/toolang/pull/814).

## Goal, scope, and baseline

Compatible Chat clients share one server for the same agent and Toolang root.
Resident servers remain running; visiting servers started on demand exit after
both clients and owned work become idle. Explicit start keeps either placement
available until stopped. Include host and Docker, driver defaults, and companion
thread/control commands needed to make these lifetimes compose.

Roaming and script execution are deferred and retain their current behavior.
Do not change workspace grants, thread/history selection, source identities,
authored run/spawn semantics, or historical stores. Add no supervisor process,
server pool, driver hot reload, or detached-job abstraction.

At approval, Chat shared a persistent server and closed only its client, without
retention leases or idle shutdown. Start/stop accepted resident agents only;
serve accepted visiting targets. Fresh hosting defaults were resident
chat,talk,chore and visiting chat,talk, including implicit Chat.

Verified sources: [acquisition](../../src/toolang/cli/common/agent_server.py),
[Chat](../../src/toolang/cli/toolang/commands/chat/main.py),
[routing](../../src/toolang/cli/toolang/routing.py),
[runtime commands](../../src/toolang/cli/toolang/commands/runtime.py),
[defaults](../../src/toolang/up/config.py),
[source identity](../../src/toolang/up/source.py),
[launch records](../../src/toolang/up/records.py), and
[spawn ownership](../../src/toolang/execution/executor/awaitables.py).
This definition supersedes only the in-scope startup/lifetime rules in
[execution drivers](serve-execution-drivers.md) and
[canonical subscriptions](local-subscriptions.md).

## Command defaults and ownership

Defaults apply only to fresh servers. Resolve them at CLI call sites and pass
concrete values into hosting; keep roaming and script call sites unchanged.
Driver selection and server lifetime are independent.

| Entry | Fresh drivers | Resident lifetime | Visiting lifetime |
| --- | --- | --- | --- |
| start | chat,talk,chore resident; chat,talk visiting | Persistent | Persistent |
| serve | chat,talk,chore resident; chat,talk visiting | Foreground-owned | Foreground-owned |
| chat | chat | Persistent | Automatic |
| retry/rerun, fork/rewind | chat | Persistent | Automatic |
| steer/cancel | No launch | Attach only | Attach only |
| Read-only commands, talk, chore commands | No launch | Existing prerequisites | Existing prerequisites and placement restrictions |

The implicit chat baseline lets retry/fork-first and Chat-first compose without
enabling background talk/chore. Generic management APIs still require no chat
driver. Preserve the existing flags: only start, serve, and chat expose --enable.
Chat requires chat even when an explicit selection is supplied.

Attachment preserves the server's drivers and sandbox when options are omitted.
Explicit driver/sandbox selections must match. Preserve existing workspace and
startup-option checks: --dev, --catalog, and --compact-model remain startup-only;
do not add digest matching or relax grants in this scope. A startup-only option
on attachment fails before promotion or mutation. Conflicts give stop/restart
guidance. Per-call runnable/model/limit settings and workdir retain their existing
semantics.

Extend visiting start/stop to the same before/after target positions as resident.
Reuse AgentLayout/source identity: distinct visiting sources or Toolang roots
remain separate. Start derives the layout and checks the live server before
fetching/preparing source; fetch only for a fresh launch. Stop needs no source
fetch. Compatible calls share; incompatible calls fail without takeover, restart,
or an implicit private instance.

Start creates a persistent server if absent, promotes an automatic server in
place, or succeeds idempotently for a persistent server. Report its actual
drivers and lifetime; omission of --enable does not add drivers during promotion.
Validate explicit options first, distinguishing CLI defaults from user assertions.
Start cannot detach an existing foreground serve; serve rejects an active server.
Explicit stop may terminate any lifetime, including active work. Disconnected
clients never relaunch automatically; a later user command may start again.

## Server-owned automatic shutdown

Reuse the existing launch_id as generation and client_id as lease identity.
One server lifespan task handles idle exit; clients renew in their own processes
independently of terminal input and SSE. Only automatic servers need leases.

1. Under the existing host management lock, elect/reuse a launch, check identity
   and configuration, and register the caller before returning. Release the lock
   before caller work. Add generation-checked lease upsert/release and promotion
   endpoints; profile/status exposes generation and lifetime. Renew every five
   seconds; expire after fifteen seconds of server monotonic time. A thirty-second
   hold from readiness covers startup; consume it on the first registration.
   Release is immediate, idempotent, and best effort after command completion.
   Failure to release cannot turn a successful operation into failure or affect
   another generation.
2. Exit only when lifetime is automatic, the startup hold has ended, no leases
   remain, and no owned pending/running/finishing execution or mutation remains.
   Retain work from admission before acknowledgement through descendants,
   terminal persistence, and cleanup, for every executor ingress. Use live
   ownership, never persisted running rows or all background asyncio tasks.
   Spawn creates independent roots, which retain the server after their source
   finishes. Parent completion drains/cancels owned async run children. Preserve
   both existing lifetimes.
3. Lease changes, admissions, promotion, and exit election share a short
   server-local gate, held across neither execution nor network I/O. Close
   admissions before graceful shutdown. Racing acquisition joins first or gets
   a closing response and waits for cleanup before acquiring another generation.
   Fork/rewind --chat keeps one acquisition through both phases. Promotion
   disables idle exit before host metadata is mirrored; recover from the live
   server's authority. CLI mutations send the acquired launch_id as a protocol
   precondition; reject stale generations before mutation if an endpoint is reused.
   Protocol additions are optional for other existing callers; only the in-scope
   CLI paths opt in, preserving roaming/script behavior.
4. Closing Chat or losing its process/transport releases or expires its lease;
   it does not cancel accepted work. Preserve explicit user interruption/cancel
   semantics. Runs obey existing limits and remain controllable by another
   caller. Report accepted IDs or uncertain acceptance; never resubmit silently.
   Renewal failure beyond the lease window ends the client session clearly.
   No new orphan timeout kills work merely because all clients disappeared.
5. Host exits normally. Automatic Docker launches use native exit removal, with
   history, runtime logs, and startup diagnostics preserved on host mounts before
   removal. Promotion disables idle exit while retaining native exit removal.
   Status/release tolerate an already removed container. Available clients or
   later host management clean control/stage files idempotently by launch_id;
   guest code neither controls host Docker nor writes host-owned control fields.

Legacy servers remain retained/unmanaged and explicitly stoppable. Compatible
existing execution APIs remain usable; automatic lifetime, promotion, and guarded
controls require advertised support or explicit stop/restart. Never silently
downgrade lifecycle safety. An idle browser and future scheduled work do not
retain automatic services; use start/serve for unattended availability, including
when explicitly enabling talk/chore.

## Active controls

Resident/visiting steer/cancel attach only to a healthy live execution owner;
they never launch a replacement for a stale database row. Pin launch_id and
resolve a thread target once. For guarded controls, the hosted API checks live
executor ownership and the expected invocation ControlRef atomically before
writing; these CLI paths always supply that optional protocol reference. A
delayed control must not reach a retry reusing the same run ID. Keep local
executor semantics unchanged elsewhere and reuse existing control references.

## Command combinations and acceptance

Examples use too, the alias for toolang. The table defines required behavior.

| Combination | Required outcome |
| --- | --- |
| Two too alice chat calls | One persistent server; independent clients and default threads; explicit thread sharing remains valid |
| Two too https://toolang.ai/dev.too chat calls | One automatic server; either client may exit first |
| Final visiting Chat closes with no active work | Immediate idle shutdown after release; history survives |
| Final idle visiting client is killed | Exit after its lease expires; no helper process is needed |
| Final client is killed with root/spawn work active | Lease expires; work/cleanup retains server until complete |
| One Chat disconnects while another remains | Other client and accepted work continue |
| Visiting retry/fork before Chat, or fork/rewind --chat | Same chat-capable server; no shutdown gap between phases |
| too https://toolang.ai/dev.too start during Chat, then Chat exits | Same generation persists with its actual drivers |
| too alice chat, then too start alice | Existing chat-only server persists; report chat without adding drivers |
| Explicit --enable none service, then generic retry, then Chat | Generic execution works; Chat fails clearly without changing service |
| too stop https://toolang.ai/dev.too with active clients/work | Stop that generation; clients disconnect without relaunch |
| start/serve against a foreground server | Start cannot detach it; serve rejects duplicate ownership |
| New acquire/promotion races final release or delayed old request | One accepting generation; no lost accepted work or mutation of its replacement |
| steer/cancel against stale history or a retried invocation | No launch or misdirected control; clear conflict |
| Different root/source, incompatible settings, legacy server | Separate identity or explicit compatibility guidance; no takeover |

Acceptance tests cover these scenarios offline, using deterministic execution,
concurrent CLI processes, and host/Docker lifecycle contracts. Include idle input,
SSE loss, registration failure, startup-hold consumption/expiry, finalization,
promotion/stop races, stale generation/control references, diagnostic preservation,
and deferred host-file cleanup. Verify roaming/script behavior is unchanged.

## Implementation touchpoints and risks

Likely files: cli/common/{agent_server,client,run_client}.py;
cli/toolang/{routing,commands/runtime,commands/thread}.py and
commands/chat/{main,remote}.py; up/{types,core,records,process,server,sandbox}.py;
api/{app,schemas,routers/agent,routers/runs,routers/threads}.py;
execution/{remote,executor/executor}.py; base/types/sandbox.py and Docker
sandbox launch/release.
Use existing factories, source identity, launch records, and executor ownership;
keep protocol types in schemas and lifecycle with hosting.

Implementation must pass default repository verification, update focused CLI/docs,
and generate its changelog through too aid.too update_changelog for maintainer
review.

Risks: repeated visiting calls may incur cold starts; use start for warm reuse.
Implicit resident Chat no longer starts talk/chore. For background drivers,
start first with the desired selection; an already running chat-only service
needs stop/restart to change drivers.
Unlimited active work can retain a server without clients; existing limits,
cancel, and stop remain the controls. Lease expiry can end a suspended idle
client. Host staging files may remain until later management. Server crashes keep
existing recovery semantics; automatic run resumption is not promised.

Open questions: none in scope.
