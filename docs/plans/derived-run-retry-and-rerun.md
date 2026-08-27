# Define Retry Trimming And Rerun

## Work Type

Feature definition. This plan replaces the current retry and rerun semantics.

## Goal

Retry one run in place by physically trimming its invalid suffix, then
continuing with the same run ID. Rerun creates a separate run without changing
its source.

## Success Criteria

- Retry keeps the original run ID and appends a retry control.
- Retry deletes the invalid suffix without backup, clone, tombstone, or
  accounting snapshot.
- Rerun creates a new root run and leaves its source unchanged.
- Existing `eject` and `ejected_by` vocabulary remains only to minimize the
  diff; retry and rerun do not use it.
- `RunExecutor` and the single `RunClient` expose `run`, `retry`, `rerun`,
  `cancel`, and `steer`.
- Default verification passes.

## Scope

In scope:

- destructive retry trimming and same-run continuation;
- non-destructive rerun;
- local executor, client, HTTP, CLI, history, and tests.

Out of scope:

- thread `create`, `fork`, and `rewind` changes;
- ejection renaming, removal, or cleanup;
- deleted-record recovery or accurate accounting for trimmed work;
- public storage primitives;
- sandbox-selection policy changes.

## Identity And Controls

`run` and `rerun` create root runs with new IDs and index-zero preparation
controls. `retry` targets an existing visible terminal root run, appends its
next control, and updates `RunRecord.control` to that retry control.

Keep the existing payload vocabulary:

```text
RerunControlPayload(..., rerun_from: RunId)
RetryControlPayload(..., retry_from: StepPath | None)
```

Retry returns a handle for the existing run ID. Rerun returns a handle for its
new run ID.

## Retry

Retry performs one atomic store operation:

1. Validate that the run is a visible terminal root and that its recorded
   state, runnable, and sandbox remain compatible.
2. Resolve the existing explicit or default retry anchor.
3. Normalize a Flow anchor to its owning top-level statement. Agic retry trims
   the complete agic Step history and restarts from index zero.
4. Physically delete the anchor and following Step suffix.
5. Remove child run subtrees owned by deleted parent Steps so recreated paths
   cannot adopt stale children.
6. Mark earlier pending controls that can no longer apply as `wontapply`.
7. Append an applied retry control, update `RunRecord.control`, and reset the
   run to pending with no output, error, start, or finish timestamp.

The transaction rolls back completely on failure. The run is not ejected. Its
retained prefix keeps the same Step paths, and execution recreates the trimmed
paths under the same run ID.

Flow execution reconstructs locals from the retained committed prefix and
continues at the anchor statement. Agic execution starts again from its input.
Repeated retry performs the same operation after the run becomes terminal
again.

## Rerun

Rerun creates a new root from a visible terminal source invocation and the
requested fresh state, model, limits, resources, and sandbox. It records
`rerun_from`, executes from index zero, and does not eject or otherwise modify
the source. A source may produce multiple reruns.

## Accounting

Accounting and limit restoration use the Step records that still exist.
Deleted retry suffixes no longer contribute usage or cost.

This inaccuracy is intentional and accepted. Do not add accounting snapshots,
tombstones, or compensating records for deleted work.

## Executor And Client

`RunExecutor` keeps lifecycle `start`/`stop` and operations `run`, `retry`,
`rerun`, `cancel`, and `steer`.

Add async `retry` and `rerun` to the single `RunClient`. Local and HTTP clients
return the existing `RunHandle`; no operation-specific client is added. A retry
handle carries the original run ID, while a rerun handle carries the new ID.

`disconnect()` does not cancel a run, and executor `stop()` remains separate.
Progress binds to the handle's run ID. AgentServer-backed commands use the
remote client; embedded execution uses the local client.

## Persistence And Compatibility

Keep execution schema version 30 and the current retry/rerun payload shapes.
No migration or compatibility layer is required. Retry changes store behavior,
not durable vocabulary. Existing ejection fields and projections remain solely
to minimize the change; retry and rerun neither read nor write them.

Request-ID uniqueness, retry trimming, control insertion, and run reopening
must commit in one write transaction. Concurrent retries of the same terminal
run have at most one winner because the winner changes the run to pending.

## Implementation Phases

1. Implement physical trimming, same-run executor continuation, non-destructive
   rerun, local CLI/API behavior, and tests.
2. Add retry/rerun to local and remote `RunClient` implementations and route
   AgentServer-backed commands through them.
3. Define thread fork/rewind separately, including any later ejection cleanup.

## Implementation Touchpoints

- `src/toolang/execution/store.py`, `history.py`, and `executor/`;
- `src/toolang/execution/client.py` and `remote.py`;
- run API routers and schemas;
- retry/rerun CLI orchestration, progress, and execution tests.

## Acceptance Tests

1. Retry preserves the run ID, deletes the selected suffix atomically, appends
   a retry control, and recreates trimmed paths during continuation.
2. Flow retry restores retained locals; agic retry restarts from index zero.
3. Related child subtrees cannot reappear under recreated parent paths.
4. Concurrent retry accepts once and rejects after the run becomes pending.
5. Deleted Steps disappear from accounting and limit restoration without a
   replacement record.
6. Rerun creates a new ID and leaves the source unchanged and visible.
7. Retry and rerun do not read or write ejection fields; existing ejection
   storage and projections remain unchanged.
8. Local and remote clients return the correct same/new run IDs, stream events,
   and preserve cancel/disconnect behavior.
9. CLI and HTTP output report the retried run ID or rerun source and target IDs.
10. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Retry permanently loses trimmed execution details and accounting.
- Child-run cleanup must be complete before parent Step paths are reused.
- Reopening the same run makes its current status and result describe only the
  latest attempt.

## Open Questions

None.
