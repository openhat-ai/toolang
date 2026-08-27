# Define Agent State Reload Controls

## Status

Proposed.

## Goal

Let an active root execution adopt a newer durable `AgentState` through a
`reload` control. Persist one State pointer on every run and physical step so
inspection can determine the exact State used without repeating its revision.

This plan builds on existing Agent State publication and Flow module support.
It does not change State preparation or authored-language behavior.

## Success Criteria

- Every run and physical step records one State `Pointer`.
- Only preparation and reload controls store State revisions.
- Reload controls always use `immediate` timing.
- The executor starts from the root run State and adopts a reload at the next
  serialized execution boundary after observing it.
- A started step keeps its captured State; later steps use the reloaded State.
- A child run uses the State captured by its calling step.
- Parallel Flow steps remain concurrent and record their State deterministically.
- Behavior is unchanged when no reload is submitted.

## Record Model

`RunRecord` and `StepRecord` gain:

```text
RunRecord.state: Pointer
StepRecord.state: Pointer
```

The pointer addresses the `state` field of a durable control:

```text
run_abc^0/state
run_abc^3/state
```

`Pointer.control(target, index, "state")` constructs this value. State-pointer
validation requires the target control to contain a valid Agent State revision.
No separate State-reference type is introduced.

The pointed-to controls are:

- the root `run` or `rerun` control for the initial State;
- a `reload` control for a reloaded State; or
- a historical preparation control retained by migration.

`RunRecord.state` is immutable. It identifies the State used to resolve and
prepare that run. A top-level run points to its entry control. A child run
copies the calling `StepRecord.state` pointer.

`StepRecord.state` is captured when `StepBegin` commits. The step uses the
resolved immutable `AgentState` object for its full lifetime and never rereads
the executor's mutable current State.

Example:

```text
root control 0: run(state = revision A)
  root run.state  -> root^0/state
  step 0.state    -> root^0/state

root control 1: reload(state = revision B)
  step 1.state    -> root^1/state
  child run.state -> root^1/state
```

New child `run` and `retry` controls omit their duplicated `state` revision.
Their run record is authoritative. Readers continue accepting State revisions
on historical child and retry controls.

SQLite stores the canonical pointer text in a new required `state` column on
`runs` and `steps`. Migration assigns each historical run the pointer to the
State field of its referenced preparation control and copies that pointer to
its historical steps. No revision is rewritten.

Run and step inspection expose the pointer. Resolving the State revision follows
the pointer to its control payload.

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
current State: AgentState object + Pointer
```

The pair starts with the accepted root State and its entry-control pointer.
When the executor observes a reload, it schedules one short application under
the root `_ActiveRun.event_lock`:

1. claim the reload control;
2. mark it `applied` in the store;
3. replace the current State object and pointer before releasing the lock.

There is no await between the durable application and the in-memory swap. A
failed claim leaves the current pair unchanged.

Every physical step begins through the same lock. Its boundary persists
`StepBegin(state=current_pointer)` and returns the matching immutable
`AgentState` object. Model, tool, human, agent, and child-run work starts only
after the boundary commits and the lock is released.

Reload application and `StepBegin` are therefore totally ordered within one
root tree:

- a step whose begin commits first keeps the old State;
- a reload that applies first is used by the next step begin; and
- an already-started step is never changed.

Agic steps normally reach the boundary sequentially. Parallel Flow branches
compete only for the short boundary lock; their work remains concurrent. The
first branch through the lock records whichever pointer is current at that
boundary. State is never inferred from `StepPath`, timestamps, completion
order, or the mutable executor pointer.

State-aware preparation for a step uses the returned snapshot. If that step
accepts a child run, the child uses the same object and pointer. Reload does not
reinterpret a run that was already accepted, but it is available to the next
step and to a child run accepted by that step.

A new top-level run remains independent: its caller supplies the current
`AgentState`, which is recorded on the new root entry control.

## Retry

Retry rejects a root with an applied reload because this phase does not replay
the executor State timeline. Revoked and `wontapply` reloads do not block retry.
Rerun is a new root and uses its caller-supplied State normally.

## Scope

Included:

- `reload` control records with immediate timing;
- State pointers on run and step records;
- revision de-duplication for new child and retry controls;
- pointer resolution and database migration;
- root executor State and serialized reload/step boundaries;
- child-run inheritance from the calling step State; and
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
  State pointer validation, and codecs;
- `src/toolang/execution/store.py`: schema migration, pointer resolution,
  reload lifecycle, child acceptance, and retry guard;
- `src/toolang/execution/events.py` and `schemas.py`: step State projection and
  run/step inspection;
- `src/toolang/execution/executor/common.py` and `executor.py`: current State,
  retained reload objects, shared boundary, and child inheritance;
- executor run/step modules: capture and pass the boundary State snapshot; and
- execution record, store, executor, and integration tests.

Implementation also updates `docs/agent-state.md`, `docs/execution.md`,
`docs/executor.md`, and `docs/run-step-records.md`.

## Acceptance Tests

1. Run, step, and reload codecs round-trip canonical State pointers and
   revisions and reject malformed values.
2. Reload accepts only `immediate` timing; existing steer and cancel timings
   remain unchanged.
3. Migration gives every historical run and step a resolvable State pointer
   without changing stored revisions.
4. New root and reload controls store revisions; new child and retry controls
   do not repeat them.
5. A root starts with its entry-control State pointer.
6. A started step keeps its State when reload applies, and the next step records
   the reload pointer and receives the matching `AgentState` object.
7. Parallel Flow steps beginning on opposite sides of reload application record
   the old and new pointers while their execution remains concurrent.
8. A child run accepted after reload records the calling step's State pointer
   and resolves its runnable against that State.
9. A second top-level root can use a newer State without changing another root.
10. Reload claim and revocation have exactly one winner; a root ending first
    marks the reload `wontapply`.
11. Missing, terminal, remotely owned, cross-layout, and non-durable reload
    candidates are rejected without changing current State.
12. Retry rejects applied reload history but permits revoked and `wontapply`
    reloads; rerun uses its caller-supplied State.
13. Runs without reload preserve existing behavior except for required State
    pointers.
14. The default Ruff, formatting, type, and pytest checks pass.

## Risks

- All step creation paths must use the shared boundary or their State record can
  diverge from the object used for preparation.
- Reload application and step begin must share one root lock so parallel Flow
  branches have an explicit order.
- Child resolution against a newer State can fail when the referenced runnable
  was removed or changed; the calling step reports that normal execution error.
- Required State columns need complete migration and fixture coverage.

## Open Questions

None.
