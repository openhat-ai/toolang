# Define Agent State Reload Controls

## Status

Proposed.

## Goal

Give execution records and `RunExecutor` one explicit model for the Agent State
bound to every run and used by every step. A newly published State is available
to the next root run through normal acceptance. An active root tree may switch
its executor State through a durable `reload` control using the existing control
timings.

This is an execution foundation. It is independent of flow authoring, model
tools, dynamic runnable calls, and watcher refresh policy.

## Success Criteria

- Every accepted run and physical step has one durable State binding reference.
- A root entry control and each applied `reload` control own one State revision.
- Run and step records reference those controls instead of copying revisions.
- A new root run can use the latest caller-supplied State while existing roots
  retain their own current State.
- `reload` supports `immediate`, `next_step`, and `next_call` with the same
  checkpoint vocabulary as existing run controls.
- The executor starts from the root run State and changes its current State only
  when a `reload` control becomes effective.
- A step keeps the State captured at its `StepBegin`; a reload never changes an
  already-started step.
- Static run declarations, modules, and prepared resources remain bound to the
  accepting run State.
- Pending reloads are ordered, revocable before claim, and terminalized when no
  matching checkpoint remains.
- Existing execution behavior is unchanged when no reload is submitted.
- The default verification suite passes.

## Current Control Timing

`ControlTiming` already contains exactly:

```text
immediate | next_step | next_call
```

The timing names describe when a control becomes eligible; the effect remains
kind-specific:

- `immediate` is applied as soon as the owning executor observes and claims the
  control;
- `next_step` is applied at the next physical step checkpoint; and
- `next_call` waits until the next existing call checkpoint, skipping Flow
  statements that do not make a call.

Flows already checkpoint before statements and call-bearing statements. Agics
checkpoint before model and tool calls. Reload reuses those boundaries rather
than adding a fourth timing. `next-run` is not a control timing: it is normal
top-level acceptance against a new caller-supplied `RunSpec.state`.

## State Vocabulary And Ownership

| Term | Meaning |
| --- | --- |
| run State | Immutable `AgentState` used to accept and prepare one run |
| step State | Executor State captured when one physical step begins |
| current State | Mutable root-tree pointer maintained by `_Execution` |
| State binding | Root-owned control reference that resolves to one revision |

The root entry `run` or `rerun` control is the initial State binding. Each
applied `reload` control becomes another binding. The executor current State is
the `AgentState` object resolved by its current binding.

`AgentState` preparation and publication remain owned by `StateWatcher`.
Callers decide freshness before root acceptance or reload submission. The
executor never parses authored source or prepares State.

## Durable Records

`RunRecord` and `StepRecord` gain:

```text
RunRecord.state: ControlRef
StepRecord.state: ControlRef
```

`RunRecord.state` identifies the State used to resolve the runnable, owner
module, inputs, and resources for that run. A root initially points to its own
entry control. A static child inherits its parent's run binding. A future
state-aware child may instead be explicitly accepted from a step State.

`StepRecord.state` identifies the executor State captured for that physical
step. It is required on model, tool, run, value, structural, and nested steps.
A step that does not read Agent State still records the binding available at
its boundary. State-aware step preparation receives the same captured
`AgentState` object and must not reread the mutable executor pointer later.

One reload therefore produces this graph without repeating revisions on its
consumers:

```text
root control 0  run(state = revision A)
  <- root RunRecord.state
  <- steps begun before reload

root control 1  reload(state = revision B, timing = next_step)
  <- steps begun after reload applies
  <- future runs explicitly accepted from those step bindings
```

An existing static child may still point `RunRecord.state` to `root:0` while
its later steps point `StepRecord.state` to `root:1`. The two references express
immutable run preparation and the executor State available to the step.

`ControlKind` gains `reload`, with this payload:

```text
ReloadControlPayload:
  state: lowercase SHA-256 Agent State revision
```

The control reference itself is the new State binding, so the payload does not
repeat the previous revision or binding. Durable control index and application
status determine transition order.

Preparation controls keep `state` only for a new root `run` or `rerun`. New
child `run` and `retry` controls omit it because `RunRecord.state` is
authoritative. The typed preparation payload accepts an optional State field
for storage compatibility; store acceptance enforces presence for new root
entries and absence for new child/retry writes. Readers continue accepting
historical duplicated revisions.

The runs database adds `state_target` and `state_index` to both `runs` and
`steps`. Migration maps every historical run and step to its tree's root
index-zero entry control. Existing behavior guarantees one revision per
historical tree; migration verifies all historical preparation revisions agree
before committing. An inconsistency fails the migration atomically.

Caller-facing run and step detail expose the binding reference. Inspection
resolves its revision through the referenced root entry or reload control and
does not duplicate the revision into run or step projections.

## Reload Control Lifecycle

`RunExecutor.reload()` accepts an active run ID, one concrete durable
`AgentState`, a `ControlTiming`, and an optional request ID. It normalizes any
active child ID to the locally owned root and inserts a root-targeted pending
`reload` control containing the State revision.

Only the process owning the active root exposes reload in this phase. There is
no public HTTP, CLI, Chat, scheduler, or generic remote-control submission. The
store still uses the ordinary control revision feed and claim flag, so the
lifecycle remains compatible with cross-process observation.

At most one reload may be pending for a root. A second request is rejected
until the first becomes `applied`, `revoked`, or `wontapply`. Requesting the
already-current revision is an idempotent no-op and creates no control.

A pending reload may be revoked through the existing control-cancellation path
until the executor claims it. Claim and revocation retain the current
linearizable SQLite race: exactly one succeeds.

When the timing becomes eligible, the executor claims the reload and the store
marks it `applied` before the in-memory current State changes. There is no await
point between the durable application and pointer replacement. If the root
ends before a `next_step` or `next_call` checkpoint, terminal projection marks
the reload `wontapply` with the existing control error behavior.

### Immediate

An `immediate` reload applies when the owner observes it. It does not cancel,
restart, or rewrite an already-started step. That step continues with its
captured State; the next step captures the new current State. This preserves
one deterministic State per step while still advancing the executor pointer
without waiting for another planned checkpoint.

### Next Step

A `next_step` reload applies immediately before the next physical `StepBegin`
in the root tree. The step that requested the reload has already begun and
keeps its previous reference. The next step records the reload control as its
State binding.

Parallel branches serialize the short root checkpoint. The first physical step
to acquire it receives the new State, and durable `StepBegin` order makes the
result inspectable. Steps already begun keep their earlier binding.

### Next Call

A `next_call` reload applies at the next existing call checkpoint. Non-call
Flow steps may begin first and continue recording the previous binding. The
first subsequent call-bearing step captures the reload binding. The call
classification is the same one currently used by next-call cancel checks; no
reload-specific statement list is introduced.

## Deterministic Step Boundaries

Step State is recorded directly; it is never reconstructed later from the
mutable executor pointer, `StepPath` ordering, timestamps, or reload-control
timestamps. Those values cannot globally order parallel Flow branches.

Reload application and `StepBegin` persistence are the two durable
linearization operations. Control acceptance only makes a reload pending; it
does not retroactively determine a step State from wall-clock time. A step uses
the binding selected when its `StepBegin` boundary commits.

The existing `_ActiveRun.event_lock` already serializes event projection for
every run in one root tree. It becomes the single root event/boundary lock used
for reload application and step start. Agic model/tool steps normally enter it
sequentially. Parallel Flow branches may execute concurrently, but every
physical step must enter this lock before it begins.

For each step, one boundary operation performs:

1. refresh the root's observed control cache;
2. select a pending reload only when its timing matches this boundary;
3. choose the exact `AgentState` object and root-owned control reference;
4. perform any pure state-aware step preparation against that object;
5. in one SQLite transaction, mark the selected reload `applied` and project
   `StepBegin(state=<binding>)`; and
6. after commit and before releasing the lock, update the in-memory current
   State and control cache.

Without an eligible reload, the same operation projects `StepBegin` with the
existing current binding. It returns an immutable step State snapshot used for
the complete step. External model, tool, human, agent, and child-run work starts
only after the transaction commits and the boundary lock is released.

For parallel Flow execution, the coroutine that acquires this lock first owns
the next durable step boundary. If reload application is ordered between two
parallel `StepBegin` commits, the earlier step records the old binding and the
later step records the new one. If both begin transactions commit before reload
application, both keep the old State even when they finish afterward. The
explicit references make the result deterministic without imposing serial step
execution.

Immediate reload observation also enters the same boundary lock. Its control
application and current-State swap are ordered before or after any concurrent
`StepBegin` transaction. It does not alter the State snapshot of a step whose
begin transaction already committed.

## Executor State

Each private `_Execution` owns:

```text
current State: AgentState object + root-owned ControlRef
pending reload: AgentState object + RunControlRecord, or none
```

The current pair starts from the accepted root's `BoundRun.state` and entry
control. `RunExecutor.reload()` validates that the candidate is a durable State
for the same `AgentLayout`, persists its revision, and retains the exact object
beside the pending control.

Immediate observation and step/call checkpoints use the shared root
event/boundary lock described above rather than a separate reload lock. The
matching path claims the pending control, finishes it in the store, swaps the
current pair, clears the pending value, and returns an immutable snapshot.

A later reload cannot change that snapshot. Model instructions, tools, or
dynamic resolution that intentionally use executor State must receive the
captured step object, not access `_Execution.current_state` again during the
step.

`BoundRun` separately keeps its immutable run State and binding. Existing
static child acceptance copies those values from the parent. Reload therefore
does not reinterpret an accepted declaration, module-local structs, static
calls, prepared frames, caps, model selection, or resources.

Future runs intentionally accepted from a step State must resolve and validate
their own runnable, module, inputs, and resources against the captured State
and the root's original `AgentSetup`, ceilings, and limits.

## Next-Run Behavior

Top-level run callers continue refreshing State before constructing `RunSpec`.
`RunExecutor.run()` binds exactly that concrete State and records its revision
once on the new root entry control. The executor holds no process-global State,
so a new root using revision B can coexist with an older active root whose
current State remains revision A.

Rerun is also a new root and uses the caller-supplied current State. It does not
inherit the source root's current binding or reload history.

## Retry

Retry rejects a root containing an applied `reload` control before trimming or
reopening records. This phase does not reconstruct a root executor State
timeline. Revoked and `wontapply` reloads did not change current State and do
not block retry; a terminal root has no valid pending reload. Retry otherwise
continues using the root's original run State.

## Scope

Included:

- `reload` control vocabulary and all existing timing values;
- root-owned State bindings for every run and physical step;
- revision de-duplication through control references;
- record codecs, database migration, binding resolution, claim/revocation, and
  terminal control behavior;
- root-tree current State ownership and timing-aware reload application;
- immutable static run binding and step State snapshots;
- next-run coexistence and retry rejection for applied reload histories; and
- focused unit, integration, migration, timing, and concurrency tests.

Excluded:

- changes to State preparation, publication, or watcher refresh policy;
- automatic reload after source changes;
- flow source CRUD or other authored-data tools;
- model-facing internal actions or tool naming;
- dynamic public runnable calls or runnable-catalog instructions;
- public API, CLI, Chat, task, or scheduler reload endpoints;
- applying setup, plugin, environment, or policy changes; and
- reload-aware retry reconstruction.

## Implementation Touchpoints

- `src/toolang/execution/types.py` and `records.py`: `reload` vocabulary,
  payload, binding references, validation, and codecs;
- `src/toolang/execution/store.py`: schema migration, root binding resolution,
  reload insertion, claim/revocation, application, terminalization, child
  acceptance, and retry guard;
- `src/toolang/execution/events.py` and `schemas.py`: `StepBegin` State binding
  and caller-facing run/step binding projection;
- `src/toolang/execution/executor/common.py` and `executor.py`: immutable run
  bindings, current State, retained reload candidate, the shared event/boundary
  lock, timing checkpoints, and static inheritance;
- `src/toolang/execution/executor/runs` and `steps`: capture the current State
  before every physical step and reuse existing step/call classification;
- execution record/store/executor unit tests and root-tree integration tests;
  and
- `docs/agent-state.md`, `docs/execution.md`, `docs/executor.md`, and
  `docs/run-step-records.md` when implementation lands.

## Acceptance Tests

1. `ControlTiming` remains exactly `immediate`, `next_step`, and `next_call`.
2. Record codecs round-trip `ReloadControlPayload` and reject malformed State
   revisions.
3. Database migration assigns every historical run and step its root entry
   binding, preserves all records, and atomically rejects inconsistent legacy
   revisions.
4. Root `run` and `rerun` controls store one revision; new child and retry
   controls omit it and use `RunRecord.state`.
5. Every physical step records a root-owned binding whose control resolves to
   the exact revision captured at `StepBegin`.
6. A second root accepted with a newer caller-supplied State uses it while an
   active older root retains its own current State.
7. An immediate reload becomes applied when observed, leaves an already-started
   step on its old binding, and changes the next step binding.
8. A next-step reload leaves the requesting step unchanged and applies before
   the next physical `StepBegin`, including across parallel branches.
9. A next-call reload allows intervening non-call Flow steps to retain the old
   binding and applies before the next call-bearing step.
10. State-aware step preparation receives the same `AgentState` object named by
    `StepRecord.state` and cannot observe a later pointer swap.
11. Reload application and the first `StepBegin` using it commit in one SQLite
    transaction; parallel Step paths and timestamps are never used to infer the
    boundary.
12. Two parallel steps whose begin commits are ordered on opposite sides of
    reload application record the old and new bindings respectively, while
    steps already begun retain their original State through completion.
13. Existing static children retain their parent's run State even when their
    calling and nested steps capture a newer executor State.
14. Reloading the current revision creates no control; a second pending reload
    is rejected.
15. Pending reload cancellation and executor claim have exactly one winner.
16. A root ending before the requested checkpoint marks reload `wontapply`.
17. Reload through an active child targets its root; another active root is
    unchanged.
18. Missing, terminal, remotely owned, cross-layout, and non-durable candidates
    are rejected without writing a reload.
19. Binding resolution rejects missing, cross-root, wrong-kind, and malformed
    references.
20. Retry rejects a root with an applied reload before modifying records, but
    permits revoked or `wontapply` reloads; rerun starts from the caller-supplied
    State normally.
21. Runs without reload retain existing records and behavior except for the new
    binding-reference fields.
22. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Required run and step binding references need an exact migration for durable
  history and all fixture builders.
- The pending control and retained `AgentState`, then the applied control and
  current pointer, must never diverge. Root serialization and store-first swaps
  make failures explicit.
- Immediate reload must not let a running step reread mutable executor State;
  step-local snapshots enforce the one-State-per-step invariant.
- Parallel branches make next-step order meaningful only at the shared root
  event/boundary lock; every `StepBegin` records the resulting binding.
- Accidentally using executor current State for static execution would mix
  module versions. Static child tests must lock inheritance to run State.
- This foundation has no user-facing reload trigger, so integration tests call
  the process-local executor operation directly.

## Open Questions

None.
