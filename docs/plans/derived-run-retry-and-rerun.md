# Define Derived Run Retry And Rerun

## Work Type

Feature definition. This plan replaces the current retry and rerun semantics.

## Goal

Make retry and rerun start new root runs. Retry destructively trims its source
to reuse a committed prefix; rerun starts from the source invocation without
changing it.

## Success Criteria

- `run`, `retry`, and `rerun` each create a new root run with a new ID and an
  index-zero control of the matching kind.
- Retry deletes the invalid source Step suffix without backup or clone records.
- Rerun leaves its source unchanged.
- Existing `eject` and `ejected_by` vocabulary remains unchanged.
- `RunExecutor` and the single `RunClient` expose `run`, `retry`, `rerun`,
  `cancel`, and `steer`.
- Default verification passes.

## Scope

In scope:

- destructive retry trimming and prefix reconstruction;
- rerun lineage;
- local executor, client, HTTP, CLI, history, and tests;
- an execution-store schema break without migration.

Out of scope:

- thread `create`, `fork`, and `rewind` changes;
- ejection renaming or cleanup;
- Step backups, clones, tombstones, or accounting snapshots;
- public storage primitives;
- sandbox-selection policy changes.

## Run And Control Identity

Every operation accepts a new root run. Its preparation control is permanently
`ControlRef(run.id, 0)` with kind `run`, `retry`, or `rerun`.

Derivation payloads use:

```text
RerunControlPayload(..., source: RunId)
RetryControlPayload(..., source: RunId, anchor: StepPath | None)
```

The source must be a visible terminal root run physically owned by the target
thread. Fork-inherited projected runs are deferred to the thread design.

## Retry

Retry performs one atomic store operation:

1. Validate the source, recorded state, runnable, sandbox, request ID, and
   optional anchor.
2. Normalize the anchor to a safe root-run boundary.
3. Delete the normalized anchor and following Steps physically owned by the
   source root run. No backup, clone, or tombstone is written.
4. Recompute the source output from its last retained primary Step, or clear it
   when no retained output exists. Other source Run fields and controls remain.
5. Insert the target root and its retry control.

The transaction rolls back completely on failure. The source remains visible
and is not ejected, but its Step history is permanently shortened.

The target links to the retained source prefix instead of copying it. Retained
Steps keep source paths; newly executed Steps use the target run ID and start at
the normalized logical index. Inputs may point directly to retained source
values. The target event stream contains only target execution events.

Child runs whose owning source Step was deleted remain directly inspectable,
but are excluded from the retained prefix. Retry deletes only Steps physically
owned by the source root; it does not delete Run or control records.

A source may produce only one retry target. This prevents a later trim from
deleting values already referenced by an accepted target. Repeated retry follows
the chain by retrying the newest target.

Boundary normalization is:

- Flow: the owning top-level statement;
- agic: the beginning of the selected model/tool turn.

Only successful work before the boundary is reusable. The executor reconstructs
Flow locals or provider-valid agic messages from the retained prefix.

## Rerun

Rerun creates a new root from the source invocation and requested fresh state,
model, limits, resources, and sandbox. It records the source relationship,
executes from index zero, and does not modify or reuse source Steps. A source
may produce multiple reruns.

## Accounting

Accounting sums the Step records that still exist. Deleted retry suffixes no
longer contribute usage or cost to run, lineage, or thread totals. Retry limits
are restored from the retained prefix only.

This inaccuracy is intentional and accepted. The implementation must not add
accounting snapshots, tombstones, or compensating records for deleted Steps.

## Executor And Client

`RunExecutor` keeps lifecycle `start`/`stop` and operations `run`, `retry`,
`rerun`, `cancel`, and `steer`. Retry and rerun return a `LocalRunHandle` for the
new target ID.

Add async `retry` and `rerun` to the single `RunClient`. Local and HTTP clients
return the existing `RunHandle`; no operation-specific client is added.
`disconnect()` does not cancel a run, and executor `stop()` remains separate.

CLI and API responses show source and target IDs. Progress binds to the target.
AgentServer-backed commands use the remote client; embedded execution uses the
local client.

## Schema

Advance the execution schema from version 30 to 31 and reject older stores
without mutation or migration. Change retry/rerun payload lineage only; retain
existing run and Step ejection fields.

Validation rejects missing sources, invalid anchors, retrying a source that
already has a retry target, incompatible retry snapshots, and sources outside
the target thread's physical history.

## Implementation Phases

1. Implement schema, atomic retry trimming, prefix reconstruction, rerun, local
   executor behavior, local CLI/API behavior, and tests.
2. Add retry/rerun to local and remote `RunClient` implementations and route
   AgentServer-backed commands through them.
3. Define thread fork/rewind separately, including any later ejection cleanup.

## Implementation Touchpoints

- `src/toolang/execution/records.py`, `schemas.py`, `store.py`, and `history.py`;
- `src/toolang/execution/executor/`, `client.py`, and `remote.py`;
- `src/toolang/api/routers/runs.py` and API schemas;
- retry/rerun CLI orchestration, progress, and execution tests.

## Acceptance Tests

1. Retry creates a new target, deletes the source suffix atomically, recomputes
   source output, and does not write ejection or clone records.
2. The target reconstructs Flow and agic prefixes and executes from the
   normalized target path.
3. A second retry from the same source is rejected; retrying the first target
   succeeds.
4. Deleted Steps disappear from accounting and limit restoration without a
   replacement record.
5. Rerun creates an independent target and leaves the source unchanged.
6. Existing thread ejection behavior remains unchanged, and schema 30 is
   rejected without mutation.
7. Local and remote clients return target handles, stream target events, and
   preserve cancel/disconnect behavior.
8. CLI and HTTP output distinguish source and target IDs.
9. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
   and `uv run pytest` pass.

## Risks

- Retry permanently loses deleted Step details and accounting.
- Source Run metadata may describe an earlier terminal attempt whose detailed
  suffix no longer exists.
- Sparse target paths and detached child runs must not enter the retained
  prefix accidentally.

## Open Questions

None.
