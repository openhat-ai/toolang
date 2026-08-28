# Define Agent State Reload Controls

## Status

Approved. Amended on 2026-08-28 so every future Run and Step boundary reads the
root tree's latest State independently of its parent.

## Goal

Let an active root execution adopt a newer durable `AgentState` through a
`reload` control. Persist one State control reference on every run and physical
step so inspection can determine the exact State used without repeating its
revision.

This plan builds on existing Agent State publication and Flow module support.
It does not change State preparation or authored-language behavior.

## Success Criteria

- Every run and physical step records one State `ControlRef`.
- Only preparation and reload controls store State revisions.
- Reload controls always use `immediate` timing.
- The executor starts from the root run State and adopts a reload at the next
  serialized execution boundary after observing it.
- A started step keeps its captured State; later steps use the reloaded State.
- Every later child `RunBegin` uses the root tree's current State even when its
  calling step began under an older State.
- Parallel Flow steps remain concurrent and record their State deterministically.
- Behavior is unchanged when no reload is submitted.

## Record Model

`RunRecord` and `StepRecord` gain:

```text
RunRecord.state: ControlRef
StepRecord.state: ControlRef
```

The reference identifies the durable control that introduced the State. The
referenced control must be:

- the root `run` or `rerun` control for the initial State;
- a `reload` control for a reloaded State; or
- a historical preparation control retained by migration.

Its payload contains the State revision. No `StateRef`, `ErrorRef`, or new
`Pointer` behavior is introduced.

`RunRecord.state` is immutable after `RunBegin`. It identifies the State used
to resolve and prepare that run. A top-level run points to its entry control. A
child run independently captures the root tree's current State when it begins;
it does not copy the calling `StepRecord.state` reference.

`StepRecord.state` is captured when `StepBegin` commits. The step uses the
resolved immutable `AgentState` object for its full lifetime and never rereads
the executor's mutable current State.

Example:

```text
root control 0: run(state = revision A)
  root run.state  = ControlRef(root, 0)
  step 0.state    = ControlRef(root, 0)

root control 1: reload(state = revision B)
  step 1.state    = ControlRef(root, 1)
  child run.state = ControlRef(root, 1)

root control 2: reload(state = revision C)
  step 2.state    = ControlRef(root, 1)
  child run.state = ControlRef(root, 2)
```

New child `run` and `retry` controls omit their duplicated `state` revision.
Their run record is authoritative. Readers continue accepting State revisions
on historical child and retry controls.

SQLite adds required `state_target` and `state_index` columns to `runs` and
`steps`, matching existing `ControlRef` storage. Migration points each
historical run and its steps to that run's index-zero preparation control. No
revision is rewritten.

Run and step inspection expose the run-control reference. Resolving the State
revision loads that control and reads its payload.

## Reload Control

`ControlKind` gains `reload` with this payload:

```text
ReloadControlPayload:
  state: lowercase SHA-256 Agent State revision
```

Reload controls always have `timing="immediate"`; accepting another timing is
an error. `ControlTiming` remains unchanged for steer and cancel controls.

`RunExecutor.reload()` accepts an active run ID, a concrete durable
`AgentState`, and an optional request ID. It normalizes a child ID to the active
root, retains the immutable object, and persists a root-targeted reload control
containing its revision.

The existing run-control feed delivers reloads to the owning executor in
control-index order. A pending reload remains revocable until claimed. Claim
and revocation use the existing SQLite race, so exactly one succeeds. A root
that ends first marks any pending reload `wontapply`.

This phase exposes reload only through the process-local executor. It adds no
HTTP, CLI, Chat, scheduler, or model-facing endpoint.

## Executor Boundary

The root `_Execution` maintains:

```text
current State: AgentState object + ControlRef
```

The pair starts with the accepted root State and its entry-control reference.
When the executor observes a reload, it schedules one short application under
the root `_ActiveRun.event_lock`:

1. claim the reload control;
2. mark it `applied` in the store;
3. replace the current State object and reference before releasing the lock.

There is no await between the durable application and the in-memory swap. A
failed claim leaves the current pair unchanged.

Every physical step and child run begins through the same lock. Their boundaries
persist `StepBegin(state=current_ref)` or `RunBegin` plus the matching run State
reference and return the matching immutable `AgentState` object. Model, tool,
human, agent, and child-run work starts only after its own boundary commits and
the lock is released.

Reload application, `RunBegin`, and `StepBegin` are therefore totally ordered
within one root tree:

- a step whose begin commits first keeps the old State;
- a reload that applies first is used by the next Run or Step begin;
- an already-started Step or Run is never changed; and
- a later child boundary does not inherit an older parent boundary.

Agic steps normally reach the boundary sequentially. Parallel Flow branches
compete only for the short boundary lock; their work remains concurrent. The
first branch through the lock records whichever reference is current at that
boundary. State is never inferred from `StepPath`, timestamps, completion
order, or the mutable executor State.

State-aware preparation for a Step or Run uses the snapshot returned by that
boundary. A Step may plan or request a child, but the child resolves its
runnable and resources from the current State when its own `RunBegin` commits.
The calling Step keeps its earlier snapshot. The same rule applies recursively
at every depth.

A new top-level run remains independent: its caller supplies the current
`AgentState`, which is recorded on the new root entry control.

## Retry

Retry rejects a root with an applied reload because this phase does not replay
the executor State timeline. Revoked and `wontapply` reloads do not block retry.
Rerun is a new root and uses its caller-supplied State normally.

## Scope

Included:

- `reload` control records with immediate timing;
- State control references on run and step records;
- revision de-duplication for new child and retry controls;
- State-reference resolution and database migration;
- root executor State and serialized reload, Run, and Step boundaries;
- independent latest-State capture for child Runs; and
- focused record, store, executor, migration, retry, and concurrency tests.

Excluded:

- State preparation, publication, and watcher changes;
- automatic reload after source changes;
- authored Flow or agent-language changes;
- public reload endpoints or model-facing tools;
- applying setup, plugin, environment, or policy changes; and
- replaying reload history during retry.

## Implementation Touchpoints

- `src/toolang/execution/types.py` and `records.py`: reload vocabulary, payload,
  State control-reference validation, and codecs;
- `src/toolang/execution/store.py`: schema migration, State resolution,
  reload lifecycle, child acceptance, and retry guard;
- `src/toolang/execution/events.py` and `schemas.py`: step State projection and
  run/step inspection;
- `src/toolang/execution/executor/common.py` and `executor.py`: current State,
  retained reload objects, and shared Run/Step boundaries;
- executor run/step modules: capture and pass the boundary State snapshot; and
- execution record, store, executor, and integration tests.

Implementation also updates `docs/agent-state.md`, `docs/execution.md`,
`docs/executor.md`, and `docs/run-step-records.md`.

## Acceptance Tests

1. Run, step, and reload codecs round-trip State control references and
   revisions and reject malformed values.
2. Reload accepts only `immediate` timing; existing steer and cancel timings
   remain unchanged.
3. Migration gives every historical run and step a resolvable State reference
   without changing stored revisions.
4. New root and reload controls store revisions; new child and retry controls
   do not repeat them.
5. A root starts with its entry-control State reference.
6. A started step keeps its State when reload applies, and the next step records
   the reload reference and receives the matching `AgentState` object.
7. Parallel Flow steps beginning on opposite sides of reload application record
   the old and new references while their execution remains concurrent.
8. A child Run beginning after reload records the reload reference and resolves
   its runnable against that State even when its calling Step began earlier.
9. A second top-level root can use a newer State without changing another root.
10. Reload claim and revocation have exactly one winner; a root ending first
    marks the reload `wontapply`.
11. Missing, terminal, remotely owned, cross-layout, and non-durable reload
    candidates are rejected without changing current State.
12. Retry rejects applied reload history but permits revoked and `wontapply`
    reloads; rerun uses its caller-supplied State.
13. Runs without reload preserve existing behavior except for required State
    control references.
14. The default Ruff, formatting, type, and pytest checks pass.

## Risks

- All Run and Step creation paths must use the shared boundary or their State
  record can diverge from the object used for preparation.
- Reload application, Run begin, and Step begin must share one root lock so
  parallel Flow branches have an explicit order.
- Child resolution against a newer State can fail when the referenced runnable
  was removed or changed; the calling step reports that normal execution error.
- Required State columns need complete migration and fixture coverage.

## Open Questions

None.
