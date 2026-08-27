# Define Root-Run Agent State Application

## Status

Proposed.

## Goal

Give execution records and `RunExecutor` one explicit, durable model for the
Agent State bound to every run and effective at every step. A newly published
State is available to the next root run through normal acceptance, or may be
explicitly applied to an active root tree at its next step boundary.

This is an execution foundation. It is independent of flow authoring, model
tools, dynamic runnable calls, and watcher orchestration.

## Success Criteria

- Every accepted run and step has one durable State binding reference.
- A root run owns its initial State binding and every later State transition.
- Run and step records reference root-owned bindings instead of copying State
  revisions.
- A newly accepted root can use the latest caller-supplied durable State while
  existing root trees remain unchanged.
- Applying State records one exact pending revision for an active root tree and
  installs it before the next step begins.
- The step that requests application uses the previous binding; the next
  `StepBegin` and all later checkpoints use the new binding.
- Concurrent applications use an expected binding and cannot silently reorder.
- Accepted runs, their modules, and their resources never change after an
  application.
- Applying the current revision is an idempotent no-op.
- A pending application becomes `wontapply` if the root ends without another
  step.
- Retry does not attempt to reconstruct a root tree containing State
  transitions.
- Existing execution behavior is unchanged when State is never applied.
- The default verification suite passes.

## Vocabulary And Ownership

| Term | Meaning |
| --- | --- |
| run State | Immutable `AgentState` used to accept and prepare one run |
| step State | Root state head effective when one step begins |
| State binding | Durable root-owned control reference naming one revision |
| State head | Binding selected at later step checkpoints in one root tree |
| State transition | Durable compare-and-set change from one binding to another |

The root entry `run` or `rerun` control is the initial State binding. Later
bindings are root-owned `state` controls. A run's State binding is immutable.
Each step captures the state head at `StepBegin`, after applying any transition
scheduled for that boundary.

The two availability paths are separate:

- **Next run:** a top-level caller refreshes State and puts the concrete result
  in `RunSpec`. That new root records its own entry binding. Other active roots
  retain their own heads.
- **Next step:** an explicit executor operation queues a transition on one
  active root. The first step checkpoint after the request installs it. Steps
  already begun retain their recorded binding.

`AgentState` preparation and publication remain owned by `StateWatcher`.
`RunExecutor` never asks for the "latest" State, reads authored source, or
loads a revision implicitly. Its application API receives one concrete
`AgentState` from the call site, following the existing rule that environment
and freshness decisions are resolved before entering execution core.

## Durable Records

`RunRecord` and `StepRecord` gain:

```text
RunRecord.state: ControlRef
StepRecord.state: ControlRef
```

`RunRecord.state` identifies the root-owned State binding used to accept and
prepare that run. For a root it initially points to its own entry control. A
static child inherits its parent's run binding. A future state-aware or dynamic
child may instead receive the step's current binding explicitly.

`StepRecord.state` identifies the state head captured for that physical step.
It is required on every new step, including nested and structural steps. A step
that does not consult Agent State still records the head available at its
boundary, so execution order and later state-aware behavior remain
deterministic. Module source, static calls, prepared frames, and resources come
from `RunRecord.state`; a step State does not reinterpret its containing run.

`RunStore.accept_run()` must receive the binding choice, and every `StepBegin`
must carry the checkpoint binding. Neither store path infers whether a call is
static or state-aware.

One transition therefore produces this record graph without repeating a
revision on each consumer:

```text
root control 0  run(state = revision A)
  <- root RunRecord.state
  <- requesting StepRecord.state

root control 1  state(previous = root:0, state = revision B)
  <- next and later StepRecord.state
  <- future runs intentionally accepted from the State head
```

An existing static child still points its `RunRecord.state` to `root:0`, even
when its calling step points to `root:1`. The two references deterministically
express immutable module preparation versus the State available at that step.

`ControlKind` gains `state`, with this payload:

```text
StateControlPayload:
  previous: ControlRef
  state: lowercase SHA-256 Agent State revision
```

A `state` control:

- targets only a root run;
- is inserted with `timing = next_step` and `status = pending`;
- points to the exact previous root binding;
- becomes `applied` and the new root state head at the next step checkpoint;
- becomes `wontapply` if the root becomes terminal first;
- is not revocable or submitted through the current public control API; and
- stores no prepared State blob.

Only one State application may be pending for a root. A later request must wait
until the pending transition applies or becomes terminal. This avoids silently
collapsing multiple revisions onto one step boundary.

Preparation controls retain a State revision for root `run` and `rerun`
acceptance. New child and `retry` controls do not duplicate it; their run's
`state` reference is authoritative. Record readers continue accepting a State
revision on historical child and retry payloads during the schema migration,
but new writes omit it.

The typed preparation payload makes `state` optional for storage compatibility.
Store acceptance enforces it as present for a new root `run` or `rerun` and
absent for a child `run` or `retry`; caller-facing encoding omits an absent
value. State revisions otherwise appear only on root entry and `state` control
payloads.

The runs database adds `state_target` and `state_index` to both `runs` and
`steps`. Migration maps every historical run and step to its root's index-zero
entry control. Existing behavior guarantees one revision per historical tree;
migration verifies that all preparation revisions agree before committing. A
disagreement fails the migration without partially updating the database.

Caller-facing run and step detail expose their State binding references. The
revision is resolved for inspection through the referenced control payload
rather than duplicated into either projection.

## Store Operations

The store provides explicit operations to:

1. resolve any run or step binding to a canonical State revision;
2. read the current root state head; and
3. append one pending root State transition; and
4. apply that transition at a step checkpoint.

Accepting a transition runs under `BEGIN IMMEDIATE` and requires:

- an existing active root run;
- an expected `previous` reference equal to the durable current head;
- no other pending `state` control for that root;
- a canonical target revision; and
- a target revision different from the current revision.

The store allocates the next root control index and inserts the `state` control
as pending in that transaction. A stale expected binding raises a conflict.
Requesting the current revision returns the existing binding without allocating
a control. The store accepts no arbitrary target run, alternate timing, or
caller-supplied control index.

At the next executor step checkpoint, the store atomically claims and marks the
pending control `applied` after verifying that its `previous` reference is still
the head. The returned control is the new binding written into `StepBegin`. If
there is no pending transition, the checkpoint returns the existing head. The
first checkpoint to acquire the root application lock wins; steps already
recorded as begun keep their prior binding. Existing terminal projection marks
an unconsumed pending transition `wontapply`.

State reference resolution rejects missing controls, non-State-bearing control
kinds, cross-root references, malformed revisions, and cycles. These checks
also protect migrated and manually modified databases.

## Executor Semantics

Each private `_Execution` owns an immutable pair for its current State head and
an optional pending candidate:

```text
current: AgentState object + root-owned ControlRef
pending: AgentState object + pending RunControlRecord, or none
```

It starts from the accepted root's bound State and entry reference. The
executor exposes an asynchronous process-local operation equivalent to:

```python
await executor.apply_state(run_id, state)
```

`run_id` may identify any active run in the locally owned tree; the executor
normalizes it to the root. `state` is the exact candidate to schedule. The
operation rejects an inactive, terminal, remotely owned, or unknown run and an
`AgentState` whose durable revision directory does not belong to the root
run's `AgentLayout`.

Applications are serialized by one lock per `_Execution`. Inside that lock the
executor:

1. reads the current in-memory binding;
2. returns without writing when the candidate revision is unchanged;
3. rejects a second pending candidate;
4. asks the store to compare and append the pending transition; and
5. retains the exact `AgentState` object beside that control until a checkpoint.

Every physical step obtains its binding through one executor checkpoint before
state-aware step preparation or `StepBegin`. Under the same root lock, the
checkpoint applies the pending control in the store, replaces the in-memory
head with its retained `AgentState`, and returns the new reference. There is no
await point between durable application and in-memory replacement. The step
then records that reference. The step that called `apply_state()` has already
begun and therefore retains the previous reference.

Parallel branches serialize only this short checkpoint. Durable `StepBegin`
order determines which physical step was first after the request; that step and
all later steps use the new binding. Steps whose `StepBegin` was already
recorded continue with their old binding.

If the store reports a stale binding, the executor reloads the durable head,
requires it to match its own State object, and otherwise fails the application
instead of guessing which snapshot to install.

`BoundRun` carries both its immutable `AgentState` and binding reference.
Existing static child acceptance copies the parent's run State, so application
does not change static module semantics. Its calling run step still records the
current step State. Executor code that intentionally wants the applied State
must use that checkpoint binding explicitly; this is the extension point for
later dynamic calls and state-aware model instructions.

Model aliases, default models, runnable declarations, module-local structs,
caps, prepared frames, and effective resources already captured by accepted
runs remain unchanged. Future runs accepted from the state head must resolve
and validate their own module and resources against that head and the root's
captured `AgentSetup`, ceilings, and limits.

## Retry And Rerun

Retry rejects a root containing any `state` control, applied or pending, before
trimming or reopening records. This phase does not reconstruct multiple State
bindings. Retry behavior is otherwise unchanged and continues using the root's
original run State.

Rerun remains a new root run and uses the concrete current State supplied by
its caller. It does not inherit the source root's state head or transition
records.

## Scope

Included:

- execution State-binding and State-transition vocabulary;
- record codecs and caller-facing run/step binding projection;
- the runs database migration and transactional State operations;
- next-run acceptance and next-step root state-head application;
- step checkpoint binding and pending-control lifecycle;
- immutable binding propagation through existing static child runs;
- retry rejection for multi-State roots; and
- focused unit, integration, migration, and concurrency tests.

Excluded:

- changes to `StateWatcher`, State preparation, or publication;
- automatic refresh or application;
- flow source CRUD or other authored-data tools;
- model-facing internal actions or tool naming;
- dynamic public runnable calls or runnable-catalog instructions;
- API, CLI, Chat, task, or scheduler endpoints for State application;
- applying setup, plugin, environment, or policy changes; and
- multi-State retry reconstruction.

## Implementation Touchpoints

- `src/toolang/execution/types.py` and `records.py`: State control vocabulary,
  binding references, validation, and codecs;
- `src/toolang/execution/store.py`: schema migration, binding resolution,
  compare-and-set transition insertion, checkpoint application, child
  acceptance, and retry guard;
- `src/toolang/execution/events.py` and `schemas.py`: `StepBegin` State binding
  and caller-facing run/step binding projection;
- `src/toolang/execution/executor/common.py` and `executor.py`: immutable bound
  references, root state head, pending application, step checkpoints, and
  static inheritance;
- `src/toolang/execution/executor/steps`: acquire the binding checkpoint before
  each physical step begins;
- execution record/store/executor unit tests and root-tree integration tests;
  and
- `docs/agent-state.md`, `docs/executor.md`, and `docs/run-step-records.md` when
  the implementation lands.

## Acceptance Tests

1. Record codecs round-trip `state` controls and reject malformed revisions or
   references.
2. Database migration assigns every historical run and step its root entry
   binding and preserves all existing records; an inconsistent historical tree
   is rejected atomically.
3. Root `run` and `rerun` acceptance bind to their entry control; new child and
   retry controls do not copy the revision.
4. A second root accepted with a newer caller-supplied State uses it while an
   already-active first root retains its own State head.
5. Every new physical step records the root-owned binding returned by its
   checkpoint; resolving that reference yields the exact revision effective at
   `StepBegin` without a copied revision on the step.
6. Applying a different durable State appends one pending root `state` control
   linked to the previous binding; the requesting step keeps the previous
   binding.
7. The next physical step atomically applies the control before `StepBegin`,
   records the new binding, and advances only that root's in-memory head.
8. Existing static children accepted before or after application retain the
   parent's run State even when their steps record the newer head.
9. A terminal root with no later step marks its pending transition `wontapply`.
10. Applying the current revision creates no control and returns the existing
    binding; a second pending application is rejected.
11. Parallel checkpoints and applications serialize deterministically; a stale
    expected binding cannot overwrite or reorder a committed transition.
12. Applying through an active child targets its root; another active root is
    unchanged.
13. Missing, terminal, remotely owned, cross-layout, and non-durable candidates
    are rejected without writing a transition.
14. State reference resolution detects missing, cross-root, wrong-kind, and
    cyclic bindings.
15. Retry rejects a root with a pending or applied State transition before
    modifying records; rerun starts from the caller-supplied State normally.
16. Runs that never apply State retain existing execution records and behavior.
17. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Adding required run and step binding references needs an exact migration for
  durable history and all fixture builders.
- A pending control and its in-memory `AgentState`, then an applied control and
  current head, must never diverge. Root serialization and store-first
  application make failures explicit.
- Accidentally reading the root state head from static execution would mix
  module versions. Static child tests must lock inheritance to the parent
  binding.
- Parallel steps make "next" meaningful only through checkpoint order; every
  `StepBegin` records that order's binding so inspection remains deterministic.
- The foundation intentionally has no user-facing trigger, so integration tests
  must exercise the process-local executor operation directly.

## Open Questions

None.
