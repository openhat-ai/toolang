# Execution Model

This guide owns Run/Thread lifecycle, admission, controls, limits, and capture
boundaries. [Executor](executor.md) owns engine structure and task mechanics,
[Records](records.md) owns durable representation, and [Events](events.md) owns
post-commit notification and delivery contracts.


## Responsibilities

`toolang.execution` owns:

- accepting, controlling, and executing agic and flow runs;
- mandatory durable run and step history in `runs.db`;
- durable run controls that can be submitted by another process;
- thread creation, fork, and rewind semantics;
- caller-facing projections of durable execution truth.

It does not own API streaming protocols, CLI rendering, agent events, event
hubs, or exact historical event replay.

`RunHistory` is the read-only caller-facing entry point over durable execution
truth. In addition to thread and Run listing/detail, `get_output()` resolves
one output edge. The HTTP [latest-result endpoint](api.md#run-and-thread-endpoints)
selects the newest succeeded root with nonempty output through the history view;
it is not a RunHistory method. It remains independent from `RunExecutor` and
`ThreadManager`, so callers can inspect `runs.db` while no agent process is
running. `RunStore` performs raw and batched record reads; execution schemas
perform the final pure record-to-schema conversion.


## Durable Truth

`runs.db` contains:

- `ThreadRecord`, `RunRecord`, and `StepRecord`;
- one `ControlRecord` model for run- and thread-scoped controls;
- complete typed step outputs;
- content-addressed model instructions, messages, and toolsets;
- per-family ID high-water marks in `seqs`;
- conversation bindings and admitted batch receipts in `talk_bindings`/`talk_batches`.

Run events are transient facts emitted during execution. They are never stored
as `EventRecord` rows. A reconnecting caller reconstructs current state from
records, optionally replays retained events, and then observes the live suffix.
[Events](events.md) owns retention and structural replacement rules.

Persistence makes completed history available after process restart and for
later model calls. Toolang does not resume an unfinished run after its owner
process exits. The execution store reads and writes only schema 62. Older schemas
are rejected without modification, including read-only opens; writable opens also
reject adjacent legacy `ids.json` files. There is no migration or old-record
compatibility path. Stop old writers, preserve any required old runtime data
separately, and start with fresh storage. See
[terminal usage and cost records](#terminal-usage-and-cost-records) for the current
Report fields.


## IDs And Indexes

Run and interactive-thread IDs are issued through one process-owned `IdIssuer`
from `toolang.common.ids`. The process composition root gives the same issuer
and `RunStore` to `RunExecutor` and `ThreadManager`. Separate processes use
separate objects over the same database. Allocation uses a dedicated connection
and `BEGIN IMMEDIATE` to serialize updates to `seqs`. Each reservation commits
before its ID is returned and before a business write transaction begins.
Failed record creation leaves a gap rather than returning an ID to the pool.
Unused IDs are allowed; duplicates are not.

Control indexes are local to their Run or Thread target. They are allocated and
inserted under `BEGIN IMMEDIATE` in `runs.db`; index reservation is never a
separate operation. Non-null request ids are globally unique in the unified
Control table.

Runs within one thread use their durable SQLite acceptance order for history,
fork, and rewind boundaries. Wall-clock timestamps remain display metadata and
are not used to decide which runs follow an anchor.

Each `StepRef` combines an owning Run with a Run-relative `StepPath`. SQLite
stores the canonical StepRef as `steps.id`, retains the Run and relative path as
private indexed columns, and protects both identities. One process owns
execution of a run tree.


## Run Execution

`RunClient` is the transport-neutral caller boundary used by Terminal Chat. It
accepts self-contained `RunRequest` values, exposes asynchronous connect, run,
cancel, steer, and disconnect operations, and returns a transport-neutral
`RunHandle` plus
caller-facing `RunDetail` and `ControlInfo` values. The boundary deliberately
excludes stores, setup and state snapshots, local tasks, and durable records so
local and remote execution preserve the same interaction shape. Clients are
disconnected after construction and reject operations until `connect()`.

`LocalRunClient` implements that boundary over a `RunExecutor`. It reads the
current setup and state once for each run, validates the request's concrete
runnable, model parameters, materialized policy, and authored input, and
converts terminal and control records through the existing caller-facing
schemas. Local library consumers can use this client; execution owners such as
`AgentCore` continue to use `RunExecutor` directly.

`RemoteRunClient` implements the same boundary over an agent runtime's absolute
HTTP origin. Noninteractive submission uses `/api/v0/runs/authored/stream`;
interactive clients use `/api/v0/chat/runs/authored/stream` and the chat-gated
steer/rerun routes. Both consume canonical execution events and the same durable
Run detail. The server owns Setup/State capture, handler resolution, validation,
and Content evaluation. CLI clients capture file bytes before submission; the
HTTP path uses the supplied attachment map rather than opening client filenames
on the server. Direct local library calls can provide a filesystem include
resolver. Mutable session defaults remain client-owned. After an interrupted stream,
the client makes up to three GET reconnection attempts using its committed
cursor (100 ms, 500 ms, and 1 s delays); it never repeats a POST. Structural
replacement clears unfinished Parts before presentation. Disconnecting detaches readers and
owned HTTP resources without canceling server runs or managing the server
process.

[Hosting](hosting.md#server-lifetime-and-client-leases) owns shared-server
acquisition, placement-dependent lifetimes, leases, generation guards, promotion,
and cleanup. These control whether a server remains available; they do not
change the execution semantics of an accepted Run.

The process-local executor is the execution engine. [Executor](executor.md#public-boundary)
identifies its public operations and links the exact signatures. Transport
requests are resolved to RunSpec before acceptance.

The engine's process-local API and task lifecycle belong to
[Executor](executor.md#public-boundary). A caller waiting on a LocalRunHandle can
stop waiting without canceling accepted execution. Controls are durable requests;
[Run controls](#run-controls) owns their adoption and live-invocation guards.

Callers resolve the captured `AgentSetup` defaults and any session or run
policy into `RunSpec.limits` before `run()`. Per-agic model and tool call
limits reset on each agic invocation, while token, cost, and time limits are
shared by all recursive runs. Effective limits are stored on the root run
control.

A configured zero limit is active; `None` disables it. Call limits are checked
before invocation, while token and cost limits account for completed calls and
fail once totals exceed the ceiling. A token limit requires reported usage.
Unknown pricing does not prevent a call; missing/non-USD selected cost is omitted
from the USD budget. Partial estimates can therefore enforce only known charges.
A cost limit is not a guarantee that every provider charge is measured.

`run()` requires an existing thread. Thread creation belongs to
`ThreadManager` or to the package that owns a deterministic external thread id.
All independent root starts and root rerun share one transactional idle
check, regardless of prefix. A pending/running root makes that thread busy;
HTTP root admission returns 409. Processing children keep their existing lifecycle.

The run operation atomically inserts the pending run and its index-zero run
control. Run IDs are globally unique within `RunStore`; duplicates are
rejected. A non-null request ID is unique within its control table and is never
treated as a replay key. Clients either generate a globally unique request ID
across run and thread controls or pass `None`.
Talk batches and runtime dispatch use separate durable batch/Step receipts for
deduplication; callers do not turn arbitrary request IDs into replay commands.

If task startup fails after admission, `run()` uses the ordinary terminal path to
persist failure, close pending controls and publish one `RunEnd`, then re-raises
the startup error. Root and spawn admission share this path in `_launch()`;
observers drain after terminal publication, and failed task creation releases its
coroutines and subscriptions. Owned-task initialization uses the ordinary execution
failure path. No `RunBegin` is emitted before execution starts. Talk retains
the accepted batch receipt and reads its Run result without repairing execution
state or dispatching the batch again. A pending/running receipt keeps its cursor
without generating a failure notice.


## Mandatory Persistence And Tracing

Execution boundaries call `RunStore` directly with complete internal Spec/Report
values and compact output references. Persistence cannot be replaced or disabled.
`RunEvent` is exclusively a subscriber contract; no persistence adapter consumes it.

```text
prepare under the execution lock
  -> one transaction writes records, content, control changes and cursors
     and stages value-only notifications
  -> commit publishes notifications to the canonical memory stream
  -> independent subscribers deliver them to RunTracer or HTTP clients
```

Failed transactions publish nothing. Control adoption and active-run bookkeeping
use internal facts or Store write results, never client Event payloads. Atomic
admission can complete its source Step before ordinary cleanup; the existing end
cursor prevents duplicate notification. Observer failures are isolated from execution.
One tracer observes the complete run tree started by its `run()` call, including
child runs, Steps, Parts and terminal events. Tracer calls are serialized on the
owner event loop without making execution wait for subscribers.


Spawned roots have independent lifecycles and may reuse an idle local thread. The host may configure
`RunExecutor.root_tracer` and `thread_listener`; the API uses these to route the
existing thread/run events to the new IDs. Foreground tracers keep observing only
the source run tree. Dispatch failure can emit a root RunEnd before any RunBegin.
Script and Chat progress use the ordinary StepEnd output/summary to show the new
run/thread identity without adopting background progress.

## Run Events

[Events](events.md) owns value-only subscriber payloads, post-commit delivery,
stream cursors, structural replacement, and HTTP/Hub reconnection. Execution
never consumes subscriber payloads to decide persistence or control effects.
Full calls and durable provenance remain in [Records](records.md).

## Run Controls

The preparation control kind is `run`; runtime control kinds are
`exec`, `chdir`, `recall`, `compact`, `steer`, and `cancel`. Rerun creates a new
Run with a `run` control. Control timing is:

```text
immediate | next_step | next_call
```

Statuses are:

```text
pending   external steer/cancel accepted and not applied
applied   applied by the runtime
wontapply no longer applicable because the run ended or the checkpoint vanished
revoked   explicitly withdrawn before application
```

`applied` means the control was applied; it does not mean the run succeeded.
Runtime-created controls are applied when their effects commit. In particular,
an applied run control confirms admission even while the Run is still pending.
A cancel control is therefore `applied` when it cancels a run. An unapplied steer
left behind by a terminal run is `wontapply`.

Every Run entry control stores its concrete runnable and model bindings,
limits, resources, and a flat `CallInput[Value | TypedRef]`. Optional
`authored_input` records the corresponding `CallInput[str]` source snapshot.
Steer stores a primary `Part[]` value under `_`; cancel stores optional primary
Text under `_`. Exec stores input references keyed by parameter name.

Every Run entry stores its State revision. Root entries also store the accepted
sandbox; child entries omit the redundant sandbox. A durable run is a root exactly when
`parent is None`; callers derive its root by following parent-run ownership.

Every run-control insert or status change receives a monotonically increasing
SQLite revision. Each executor remembers the latest revision it observed and
loads only rows changed after that cursor. An unchanged table returns no rows
and causes no active-run processing. Changed pending controls are merged into
the matching locally owned run tree; changed terminal controls are removed.
Runtime checkpoints read this in-memory view instead of repeatedly querying
all pending controls for every active run.

Local submissions update the same cache immediately after their durable write.
Remote submissions and cancellations arrive through revision polling. An
immediate cancel cancels the owning task after the owner observes the durable
control. Before applying a steer or cancel, runtime atomically claims it in
SQLite. Cancellation is allowed only while a control remains unclaimed, so a
cross-process claim/cancel race has exactly one winner without adding another
public control status.


## Threads

`ThreadManager` synchronously performs `create()`, `fork()`, and `rewind()`.
Create and fork return the newly allocated thread id. Rewind changes the
specified thread in place and returns nothing. Fork and rewind accept an
optional run id; omission selects the last visible top-level run. Recursive
child runs are never thread anchors. An empty thread has no implicit anchor and
cannot be forked or rewound. Every selected anchor must be terminal.
All Thread kinds support these operations. Fork allocates a new ID with the
source kind but creates no Talk conversation binding or chore definition.
Rewind does not roll back message cursors or chore checkpoints.

Every successful mutation has a durable `ControlRecord` and produces one
success event:

```text
ThreadCreated
ThreadForked
ThreadRewound
```

Failures are returned or raised by the synchronous operation and do not
produce failure events.

A fork stores its source thread and anchor run but does not copy run, step, or
run-control rows. Its inherited history includes the anchor. It may select an
earlier terminal anchor even when the source thread has a later active run. A
rewind discards its anchor and the visible suffix after it, and is rejected
while any visible top-level run is pending or running. The caller must cancel
active runs before retrying; `ThreadManager` never writes run controls. Rewind
records a closed range in a Thread control without modifying Runs or Steps.
Logical history applies that Thread's controls; physical records remain readable.
Forks capture the source view at its control head, so later source rewinds do not
change the inherited prefix. Forks and rewinds are serialized across
processes by SQLite write transactions; they use no separate thread file lock. Anchor resolution, terminal checks,
the rewind idle check, and control insertion occur in one SQLite write
transaction. The store keeps expected-head comparison as an internal defensive
check; it is not part of the public manager API.


## History compaction

The thread's `horizon` optionally points to a valid compact-summary producer
Run or successful summary Model Step. A committed compact control publishes that
horizon for subsequent recall while retaining physical history. An unusable
horizon contributes no summary; history reads do not silently execute compaction.
[Models](models.md#automatic-compaction-configuration) owns budgets and
[runs/compact.py](../../src/toolang/execution/executor/runs/compact.py) owns the
recorded batched compaction loop.

Model preflight initiates compaction when required input exceeds the active
budget. It is an internal recorded Run, not an ordinary model tool. The compact
control adopts a successful summary for later recall; it preserves physical
history and does not resume old execution.

The retained near history keeps the latest complete Step and paired tool
exchanges, then adds older Steps while they fit. Fixed instructions and current
input take precedence. Compaction advances at least one history unit and may
stop inside a root.

Retained history and summary are soft targets, not a guarantee that the resulting
request fits. The complete request is checked again after compaction; required
content that cannot fit fails the Run. The 30%/80% defaults leave room for fixed
content, a summary, and subsequent conversation, but do not guarantee 50% free.

If the thread model has no declared context window, a percentage trigger leaves
admission to the known safety budget. No input limit is substituted as the
percentage base. When compaction is needed, percentage summary/recent settings
require context metadata or replacement with absolute token counts.

Compact batches use the compact model's own input capacity. Unless its model
specification explicitly sets `max_output`, the output allowance is
`max(2 * summary, summary + 1024)`, clamped to the model output limit. The default
allowance is 8192 tokens, including reasoning. A root can span multiple batches.
Oversized Step input/output is shortened with explicit omission markers, keeping
original stored records intact. The mandatory latest Step is bounded to half the
calling model's input budget so a single large historical reply does not block
progress. Fixed instructions and current execution input must still fit.

## State Capture

`RunSpec` carries one explicit immutable `AgentState`,
`toolang.setup.AgentSetup`, effective `RunBindings` and `RunLimits`, and zero
or more `AgentCeiling` restrictions. `AgentSetup` supplies the immutable
`AgentLayout`, installed runtime implementations, effective model/tool
collections, and captured policy defaults. `AgentState` includes the captured
workspace grants and effective per-module cap collections. Setup and State apply
their owned `[allow]` fields before publication; session and run policy only
narrow those bases.
Execution uses that layout directly for the agent identity, home, and runtime
rooms. `RunSpec.input` is a flat `CallInput[Value]`; `_`, when present, contains
`Part[]` or the explicitly declared input type, and other keys hold arguments.
Output coercion validates the final run value against the runnable's declared
output type. Setup and State remain
complete snapshots; the executor computes concrete `AgentResources` instead of
receiving filtered copies. A root starts from the State supplied in `RunSpec`.
Each physical Step retains its executing Run's State reference. A newly accepted
named child independently captures the latest published State and validates it
against the caller's contract. Inline bodies retain their containing plan.
Publication never rebinds accepted Runs. Invalid source units are excluded while
valid siblings can publish; fatal preparation errors preserve the last accepted
State. [State](state.md) owns those acceptance boundaries.

Model-call resource resolution is a separate capture boundary. When the executor
has a live State source, it applies accepted selectors to the latest published
State before each Model Call; otherwise it uses the bound State. Caps and
available runnable routes can therefore change between calls without replacing the
running declaration. Installed model/tool collections still come from the
accepted Setup, and workspace authority stays bound. See
[Agic frame construction](../../src/toolang/execution/executor/runs/agic.py) and
[resource resolution](../../src/toolang/execution/executor/resources.py).

## Terminal usage and cost records

For time windows, model breakdowns, and daily heatmaps, see
[execution statistics](execution-statistics.md).

`ModelStepReport` stores `usage: TokenUsage | None`, `cost: ModelCost | None`,
`invoked: bool`, and continuation encoded as `cont`. `ToolStepReport` stores
`summary` and `invoked`. `ModelCost` holds the selected `amount`, `currency`,
`source`, `complete`, `pricing`, `meters` and `lines`; there is no accounting
envelope or separate reported/estimated amount wrapper. Calculation lines explain
the catalog estimate, which can differ from a provider-reported final amount.

`RunRecord.report` is absent while pending/running. At termination it holds
`RunReport(calls, usage, cost, cost_approximate)` for that Run's resident subtree,
including already pruned branches. `calls` contains only `model_calls` and
`tool_calls`; entry counts include failed/canceled invocations and exclude skipped
or unprepared calls. Run cost is USD only; non-USD selected charges remain in
per-call records and make the aggregate USD amount unknown. Unknown optional
token/cost contributions remain unknown; known zero stays zero. These are captured
values, not a historical statistics index.

This format requires Store schema 62 and fresh storage. Older schemas are rejected
for both reads and writes; no migration or compatibility decoder is provided.
Retain any required old runtime data separately, then use a fresh runtime database.
Record/event consumers must adopt `spec`/`report` and matching `Spec`/`Report`
types, then reconnect subscriptions. See the [canonical record fields and Pointer
paths](records.md) for the complete contract.
