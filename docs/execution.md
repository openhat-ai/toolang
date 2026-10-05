# Execution

This document defines the ownership and lifecycle boundaries of
`toolang.execution`. Runnable evaluation belongs to [Agics](agic.md) and
[Flows](flow.md); detailed record shapes live in [records](records.md), and
observation contracts live in [events](events.md). Caller protocols belong to
[CLI](cli.md), [Chat](chat.md) and [HTTP](api.md).


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
truth. In addition to thread and run listing/detail, `get_run_result()` resolves
one output edge and `latest_thread_result()` selects the newest succeeded root
run with a nonempty output. It remains independent from `RunExecutor` and
`ThreadManager`, so callers can inspect `runs.db` while no agent process is
running. `RunStore` performs raw and batched record reads; execution schemas
perform the final pure record-to-schema conversion.


## Durable Truth

`runs.db` contains:

- `ThreadRecord`, `RunRecord`, and `StepRecord`;
- one `ControlRecord` model for run- and thread-scoped controls;
- complete typed step outputs;
- content-addressed model instructions, messages, and toolsets.

Run events are transient facts emitted during execution. They are never stored
as `EventRecord` rows. A reconnecting caller reconstructs current state from
records and observes only new live events.

Persistence makes completed history available after process restart and for
later model calls. Toolang does not resume an unfinished run after its owner
process exits. Both read-only and writable store opens reject incompatible schemas unchanged;
see the [current schema](records.md#persistence). This build
does not migrate older stores.


## IDs And Indexes

Run and interactive-thread IDs are issued through one process-owned `IdIssuer`
from `toolang.common.ids`. The process composition root gives the same issuer
and `RunStore` to `RunExecutor` and `ThreadManager`. Separate processes use
separate objects over the same files. Allocation is serialized with an
inter-process file lock. Unused IDs are allowed; duplicates are not.

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


## Policy resolution

Callers resolve configuration and authored overrides before acceptance. The
configuration precedence is built-in defaults, root config, agent-home config,
captured environment, process CLI overrides, then request/session fields.
Setup watchers reload configuration; environment and CLI overrides remain fixed
for the process lifetime. Setup publishes model/tool availability; State publishes
module cap visibility and workspace grants.

`SessionSetting` holds concrete defaults adopted by a caller. A sparse
`RunOverride` produces a self-contained `RunRequest`; [call input](call-input.md#shared-run-overrides)
owns its colon syntax and [Chat](chat.md) owns slash interactions. Execution
validates the concrete request and resolves authored values into `RunSpec`.
Transport requests carry neither a mutable session nor an instruction to choose
client fallbacks on the server.

- Session allow changes replace the specified session field. A run allow override
  adds another independently applied ceiling. Each ceiling intersects the
  published base; union queries in two ceilings do not merge into a broader OR.
- Runnable selectors apply within that authority. Their dynamic State visibility
  and inherited rules are described in [program semantics](program.md#directives)
  and [queries](queries.md); they cannot grant excluded resources.
- Limits overlay supplied fields; omission inherits, while `none` disables that
  limit. Limits are budget settings, distinct from allow ceilings.
- Model identity selection clears unmentioned explicit call parameters;
  parameter-only updates retain identity. `effort=auto` or `max_output=auto`
  removes an inherited explicit setting. One-run `default` restores the surface
  model; `unset` removes its binding. Validate the resulting request against the
  effective model before acceptance.
- Generic runnable `default` restores the surface selection. Explicit selections
  bypass default lookup but remain subject to existence, signature and authority.
- Workdir is resolved at the caller/server boundary against authorized workspace
  grants. Relative overrides use the supplied session/base context; a canonical
  workspace URI or absolute location starts its own resolution. See
  [scripts](scripts.md) for path and attachment boundaries.

## Run limits

| Field | Default | Accounting scope |
| --- | --- | --- |
| `agic_model_calls` | 200 | Each agic invocation |
| `agic_tool_calls` | Unlimited | Each agic invocation |
| `tokens` | Unlimited | Complete recursive root tree |
| `cost` | Unlimited | Complete recursive root tree, USD |
| `time` | Unlimited | Complete recursive root tree, wall-clock seconds |

`None` disables a limit. Model/tool call counts are checked before invocation;
zero blocks the corresponding call. Token/cost limits are checked after model
results, so zero rejects recorded positive usage rather than preventing dispatch.
A model Step can succeed before the enclosing Run fails on a total.

Token limits require provider usage. Cost limits accumulate the selected USD
amounts, including partial estimates. A cost limit alone does not require
usage or pricing: unknown amounts and selected non-USD amounts do not block a
call and contribute no known USD cost. Missing usage or rates can leave coverage
incomplete, so the limit bounds recorded costs, not necessarily actual spending.
See [accounting](models.md#runtime-calls-and-accounting) for cost selection and
coverage. The cost limit serializes as decimal text; recorded accounting amounts
use numeric fields.

Time expiry interrupts work and records failure rather than user cancellation.
Preparation controls retain effective limits. Retry restores token/cost totals
from retained succeeded Steps; deleted attempts are excluded, and reexecuted
agics restart local call counts.

## Run acceptance and ownership

`RunClient` is the asynchronous local/remote boundary over concrete requests,
handles and caller-facing projections. It starts disconnected and requires
`connect()`. `LocalRunClient` reads Setup/State once per request; `RemoteRunClient`
uses the [authored HTTP stream](api.md#run-requests). Neither owns Chat session
policy. Other local owners can call `RunExecutor` directly.

The process composition root shares one `RunStore` and `IdIssuer` between
`RunExecutor` and `ThreadManager`. The executor is usable after construction;
`start()` is idempotent and `stop()` is terminal, canceling its owned tasks before
the composition root closes the store.

`run()` requires an existing thread and a validated immutable `RunSpec`.
`RunStore.accept_run()` commits the pending Run and index-zero `run` Control in
one `BEGIN IMMEDIATE` transaction before launching the owner task. Duplicate Run
IDs or non-null request IDs fail; request IDs are globally unique across the
Control table and are not replay/idempotency keys. Scripts preallocate a Run ID for logging; the scheduler does so before its
durable dispatch claim. Other callers can let the executor issue it.

The returned `LocalRunHandle` awaits a terminal record. Its wait is shielded:
canceling an HTTP request or other waiter does not cancel durable execution.
The active registry tracks task ownership, not admission truth. Calling `run()`
in another process executes there; the store is not a cross-process work queue.

Each root has a private execution context and owner task on its event loop.
Recursive Runs share that owner, tracer and root accounting; child Runs have
independent durable IDs and entry controls. Agic/flow execution stays asynchronous
on that loop; explicitly synchronous tools may use worker threads.

## Retry and rerun

Both require a terminal root Run. Rerun prepares a new root against current
Setup/State and accepts it with a normal `run` Control; there is no `rerun`
Control kind. It does not rewrite the source Run or its history membership.

Retry retains the root identity, recorded State and model request. It appends
an applied `retry` Control, resolves its anchor, deletes the invalid structural
Step suffix and child Runs, fails stale pending controls, then reopens the root.
New Steps reuse trimmed indexes. The latest visible incomplete Step is the
usual implicit anchor; succeeded Runs prefer the latest non-value Step, falling
back to a value Step. See the executor for the complete anchor selection.

A flow restores typed locals from the succeeded top-level prefix. An unfinished
container is invalidated with its nested failure. An agic restarts its model/tool
cycle. Retry rejects trees captured by durable fork prefixes, prior applied
execute timelines, missing/mismatched sandbox provenance, and workspace grants
removed or remapped in current State. Validation precedes destructive trimming;
rerun is the route for accepting current code/grants.

Neither operation implies automatic resumption after owner-process loss.

## Execution and assembly

| Owner | Responsibility |
| --- | --- |
| [executor.py](../src/toolang/execution/executor/executor.py) | Acceptance, root ownership, controls and recursive execution |
| [resources.py](../src/toolang/execution/executor/resources.py) | Authority and runnable resource selection |
| [frame.py](../src/toolang/execution/executor/frame.py) | Bound agic frame and model-call inputs |
| [runs](../src/toolang/execution/executor/runs/) / [stmts](../src/toolang/execution/executor/stmts/) | Agic model/tool cycle and lowered flow statement behavior |
| [steps](../src/toolang/execution/executor/steps/) | Canonical Step boundaries and events |
| [assembly](../src/toolang/execution/assembly/) | Instructions, messages, tools, output schemas and replayable control text |

Assembly consumes prepared data and records without running tools or querying
the store. The model Step constructs a normalized `ModelCall`; adapters never
receive the private frame. [Agic instruction layers](agic.md#instruction-layers)
own model-facing composition and [records](records.md) own durable
call reconstruction. There is no loop plugin or public execution-frame protocol.

Top-level runs have no synthetic containing Step. A flow call emits a Run Step
around its child. Model `_toolang/run` instead commits the pending child and
receipt during the Tool Step; after that Step ends it runs the child before
continuing the remaining tool batch. The child retains the triggering Tool Step
as `parent`, even though their lifetimes do not overlap. Tool replies precede
run-result context; child internals are not flattened into the caller. Target
failure/cancellation leaves the receipt intact and supplies an outcome. Root
cancellation also cancels accepted children that have not started.

Model `_toolang/exec` validates its target, then records an applied execute
Control triggered by the Tool Step. That Step finishes before the target starts.
There is no child Run or second `RunBegin`: the Run retains root authority,
accounting and its original output contract while the target gets a fresh
continuation and agic call counters. Failed validation creates no execute
Control. Authored flow `exec` uses the same replacement semantics with an exec
Step; see [flow evaluation](flow.md#exec).

## Events and persistence

`RunExecutor` projects Run/Step changes into durable records before notifying
its optional tracer. Event projection cannot be disabled. Thread mutations
commit before their listener is notified. [Events](events.md) owns event kinds,
ordering, control relationships, serialization and observation contracts;
[records](records.md) owns stored facts and references.

## Run Controls

Preparation controls use `run` and `retry`; runtime controls include `execute`,
`steer`, `cancel`, `cwd`, `recall` and `compact`. Control timing is:

```text
immediate | next_step | next_call
```

Statuses are:

```text
pending   newly accepted and not applied
applied   applied by the runtime
wontapply no longer applicable because the run ended or the checkpoint vanished
revoked   explicitly withdrawn before application
```

`applied` means the control was applied; it does not mean the run succeeded.
A cancel control is therefore `applied` when it cancels a run. An unapplied steer
left behind by a terminal run is `wontapply`.

Every Run entry control stores its concrete runnable and model bindings,
limits, resources, and a flat `CallInput[Value | TypedRef]`. Optional
`authored_input` records the corresponding `CallInput[str]` source snapshot.
Steer stores a primary `Part[]` value under `_`; cancel stores optional primary
Text under `_`. Execute stores input references keyed by parameter name. Retry
inherits input from the entry control and records its effective settings.

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

Every successful mutation has a durable `ControlRecord` and produces one
[thread event](events.md#thread-events). Failures are returned or raised by the
synchronous operation and do not produce failure events.

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
processes with an agent-local file lock. Anchor resolution, terminal checks,
the rewind idle check, and control insertion occur in one SQLite write
transaction. The store keeps expected-head comparison as an internal defensive
check; it is not part of the public manager API.


## History recall and compaction

Each root recalls a fixed logical prefix of earlier root Runs in its thread.
`far` supplies a summary of covered history; `near` reconstructs retained recent
exchanges from durable records. Fork/rewind controls determine the logical view.
The [recall directive](program.md#directives) selects which sources are visible
and when a root agic prepends them to its messages. Child agics access history
only through explicit runtime-variable references.

A `horizon` is a Run or Step reference to a validated compaction result:
`{thread, begin, end, summary}`. Its half-open `[begin, end)` range identifies
covered history using Run or Step boundaries, so a summary may end inside a
root Run. Compaction changes the model's history view; it does not delete or
rewrite the underlying Runs and Steps. Provider continuation cursors are a
separate mechanism for continuing model calls.

When model preflight needs compaction, a runtime-only `_toolang/compact` Tool
Step starts an internal compact child Run. Neither is a public model-callable
tool or user-startable runnable. The child uses ordinary durable model/tool
Steps, and its usage counts against the enclosing root's token/cost limits.
Model choice, thresholds and retained-history budgets belong to
[compaction configuration](models.md#automatic-compaction-configuration).

A cancellable thread-scoped file lock serializes compaction across processes.
After acquiring it, execution rechecks the current history boundary and may
reuse a compatible saved result. Successful publication validates coverage and
the complete normal request's fit, then commits the thread horizon and applied
compact Control together. Failure or cancellation leaves the horizon unchanged
and prevents dispatch of the oversized normal model call.

Later Steps can adopt the published horizon throughout an active root tree.
An already prepared model call retains its captured history view. Durable
checkpoints support explicit retry/recovery; they do not provide automatic
resumption after owner-process loss.

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
Publication never rebinds accepted Runs. Invalid source is not published.

## Verification anchors

- [Control relations](../tests/integration/execution/test_control_relations.py)
  and [control scenarios](../tests/integration/execution/test_control_scenarios.py)
  cover durable acceptance, ordering and application.
- [Latest State binding](../tests/integration/execution/test_latest_state_binding.py)
  covers changed named callees versus pinned accepted code.
- [Thread controls](../tests/integration/execution/test_thread_control_scenarios.py)
  cover fork/rewind history boundaries.
- [Compaction lifecycle](../tests/integration/execution/test_batched_compact_run.py)
  and [compact controls](../tests/integration/execution/test_compact_controls.py)
  cover coverage, cancellation, durable publication and history adoption.
- [Policy tests](../tests/unit/execution/test_policy.py) cover caller layering;
  [remote runs](../tests/integration/api/test_remote_runs.py) exercise transport parity.
