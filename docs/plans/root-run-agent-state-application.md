# Define Root-Run Agent State Application

## Status

Proposed.

## Goal

Give execution records and `RunExecutor` one explicit, durable model for the
Agent State currently selected by an active root run tree. An executor caller
may apply one concrete, already-persisted `AgentState`; already accepted runs
remain bound to their original State, while later execution features can bind
new runs to the updated root state head.

This is an execution foundation. It is independent of flow authoring, model
tools, dynamic runnable calls, and watcher orchestration.

## Success Criteria

- Every accepted run has one durable State binding reference.
- A root run owns its initial State binding and every later State transition.
- Child runs reference a root-owned binding instead of copying its State
  revision.
- Applying State records and installs one exact revision atomically for one
  active root tree.
- Concurrent applications use an expected binding and cannot silently reorder.
- Accepted runs, their modules, and their resources never change after an
  application.
- Applying the current revision is an idempotent no-op.
- Retry does not attempt to reconstruct a root tree containing State
  transitions.
- Existing execution behavior is unchanged when State is never applied.
- The default verification suite passes.

## Vocabulary And Ownership

| Term | Meaning |
| --- | --- |
| bound State | Immutable `AgentState` used to accept and prepare one run |
| State binding | Durable root-owned control reference naming one revision |
| State head | Binding selected for future state-aware acceptance in one root tree |
| State transition | Durable compare-and-set change from one binding to another |

The root entry `run` or `rerun` control is the initial State binding. Later
bindings are root-owned `state` controls. A run's State binding is immutable;
the root state head is the only value that advances.

`AgentState` preparation and publication remain owned by `StateWatcher`.
`RunExecutor` never asks for the "latest" State, reads authored source, or
loads a revision implicitly. Its application API receives one concrete
`AgentState` from the call site, following the existing rule that environment
and freshness decisions are resolved before entering execution core.

## Durable Records

`RunRecord` gains:

```text
state: ControlRef
```

The reference identifies the root-owned State binding used when that run was
accepted. For a root it initially points to its own entry control. A static
child inherits its parent's binding. A future state-aware or dynamic child may
instead receive the root state head explicitly. `RunStore.accept_run()` must
receive the binding choice; it never infers whether a call is static or
state-aware.

`ControlKind` gains `state`, with this payload:

```text
StateControlPayload:
  previous: ControlRef
  state: lowercase SHA-256 Agent State revision
```

A `state` control:

- targets only a root run;
- is inserted with `timing = immediate`, `status = applied`, and identical
  creation/finish timestamps;
- points to the exact previous root binding;
- becomes the new root state head only after its transaction commits;
- is never pending, claimed by the polling loop, revoked, or submitted through
  the current public control API; and
- stores no prepared State blob.

`immediate` describes the already-completed executor mutation. It does not
rebind or interrupt an active step. A later action may choose its own safe call
boundary before invoking this operation.

Preparation controls retain a State revision for root `run` and `rerun`
acceptance. New child and `retry` controls do not duplicate it; their run's
`state` reference is authoritative. Record readers continue accepting a State
revision on historical child and retry payloads during the schema migration,
but new writes omit it.

The runs database adds `state_target` and `state_index` to `runs`. Migration
maps every historical tree to its root's index-zero entry control. Existing
behavior guarantees one revision per historical tree; migration verifies that
all preparation revisions agree before committing. A disagreement fails the
migration without partially updating the database.

Caller-facing run detail exposes the State binding reference. The referenced
revision is resolved for inspection through the control payload rather than
duplicated into the run projection.

## Store Operations

The store provides explicit operations to:

1. resolve a run's binding to a canonical State revision;
2. read the current root state head; and
3. append one applied root State transition.

Appending a transition runs under `BEGIN IMMEDIATE` and requires:

- an existing active root run;
- an expected `previous` reference equal to the durable current head;
- a canonical target revision; and
- a target revision different from the current revision.

The store allocates the next root control index and inserts the `state` control
in that transaction. A stale expected binding raises a conflict. Requesting
the current revision returns the existing binding without allocating a control.
The store accepts no arbitrary target run, pending timing, or caller-supplied
control index.

State reference resolution rejects missing controls, non-State-bearing control
kinds, cross-root references, malformed revisions, and cycles. These checks
also protect migrated and manually modified databases.

## Executor Semantics

Each private `_Execution` owns an immutable pair for its current State head:

```text
AgentState object + root-owned ControlRef
```

It starts from the accepted root's bound State and entry reference. The
executor exposes an asynchronous process-local operation equivalent to:

```python
await executor.apply_state(run_id, state)
```

`run_id` may identify any active run in the locally owned tree; the executor
normalizes it to the root. `state` is the exact candidate to install. The
operation rejects an inactive, terminal, remotely owned, or unknown run and an
`AgentState` whose durable revision directory does not belong to the root
run's `AgentLayout`.

Applications are serialized by one lock per `_Execution`. Inside that lock the
executor:

1. reads the current in-memory binding;
2. returns without writing when the candidate revision is unchanged;
3. asks the store to compare and append the durable transition;
4. replaces the in-memory State head only after the store commits; and
5. returns the resulting binding and optional transition record.

There is no await point between durable commit and the in-memory replacement.
If the store reports a stale binding, the executor reloads the durable head,
requires it to match its own State object, and otherwise fails the application
instead of guessing which snapshot to install.

`BoundRun` carries both its immutable `AgentState` and binding reference.
Existing static child acceptance copies both from its parent, so application
does not change any current behavior. Executor code that intentionally wants
the latest applied State must request the `_Execution` state head explicitly;
this is the only extension point intended for later dynamic calls.

Model aliases, default models, runnable declarations, module-local structs,
caps, prepared frames, and effective resources already captured by accepted
runs remain unchanged. Future runs accepted from the state head must resolve
and validate their own module and resources against that head and the root's
captured `AgentSetup`, ceilings, and limits.

## Retry And Rerun

Retry rejects a root containing any applied `state` control before trimming or
reopening records. This phase does not reconstruct multiple State bindings.
Retry behavior is otherwise unchanged and continues using the root's original
bound revision.

Rerun remains a new root run and uses the concrete current State supplied by
its caller. It does not inherit the source root's state head or transition
records.

## Scope

Included:

- execution State-binding and State-transition vocabulary;
- record codecs and caller-facing run binding projection;
- the runs database migration and transactional State operations;
- root-tree executor state-head ownership and application;
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
  compare-and-set transition insertion, child acceptance, and retry guard;
- `src/toolang/execution/schemas.py`: caller-facing run binding projection;
- `src/toolang/execution/executor/common.py` and `executor.py`: immutable bound
  references, root state head, serialized application, and static inheritance;
- execution record/store/executor unit tests and root-tree integration tests;
  and
- `docs/agent-state.md`, `docs/executor.md`, and `docs/run-step-records.md` when
  the implementation lands.

## Acceptance Tests

1. Record codecs round-trip `state` controls and reject malformed revisions or
   references.
2. Database migration assigns every historical run its root entry binding and
   preserves all existing records; an inconsistent historical tree is rejected
   atomically.
3. Root `run` and `rerun` acceptance bind to their entry control; new child and
   retry controls do not copy the revision.
4. Existing static children accepted before or after an application retain the
   parent's original State object and binding.
5. Applying a different durable State appends one applied root `state` control,
   links it to the previous binding, and advances only that root's in-memory
   head after commit.
6. Applying the current revision creates no control and returns the existing
   binding.
7. Parallel applications are serialized; a stale expected binding cannot
   overwrite or reorder a committed transition.
8. Applying through an active child targets its root; another active root is
   unchanged.
9. Missing, terminal, remotely owned, cross-layout, and non-durable candidates
   are rejected without writing a transition.
10. State reference resolution detects missing, cross-root, wrong-kind, and
    cyclic bindings.
11. Retry rejects a root with a State transition before modifying records;
    rerun starts from the caller-supplied State normally.
12. Runs that never apply State retain existing execution records and behavior.
13. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Adding a required run binding reference needs an exact migration for durable
  history and all fixture builders.
- A committed transition and its in-memory `AgentState` must never diverge;
  store-first replacement and stale-head checks make failures explicit.
- Accidentally reading the root state head from static execution would mix
  module versions. Static child tests must lock inheritance to the parent
  binding.
- The foundation intentionally has no user-facing trigger, so integration tests
  must exercise the process-local executor operation directly.

## Open Questions

None.
