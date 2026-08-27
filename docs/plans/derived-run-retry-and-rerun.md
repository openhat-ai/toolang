# Define Derived Run Retry And Rerun

## Work Type

Feature definition. This plan supersedes the retry and rerun semantics in
earlier execution plans. It does not implement the feature.

## Verified Current Behavior

- `retry` appends a control to an existing root run, ejects a step suffix,
  clears the run result, and changes the terminal run back to pending.
- `rerun` creates a new root run and ejects the complete source run tree.
- A retried Flow resumes from the remaining visible steps. An agic retry ejects
  its complete step history and starts the agic again.
- Limit restoration scans previously executed model steps.
- `RunClient` exposes `run`, `cancel`, and `steer`; CLI and HTTP retry/rerun
  paths call `RunExecutor` directly.

## Goal

Make retry and rerun immutable run derivations. Each operation starts a new
root run, leaves the source records unchanged and visible, and uses explicit
lineage to reuse retry history without copying Step records.

## Success Criteria

- `run`, `retry`, and `rerun` each accept one new root run with a new run ID and
  an index-zero preparation control of the matching kind.
- Retry and rerun never mutate or eject their source run, steps, or controls.
- Retry reconstructs a valid source prefix and executes the selected boundary
  and suffix under the new run ID without copying prefix Step records.
- Durable Step records always represent work actually executed by their owning
  run, so usage and cost need no clone exclusion rule.
- Repeated retries follow explicit lineage without rewriting existing Step
  paths or creating clone provenance.
- `RunClient` and `RunExecutor` expose the same run-control vocabulary without
  a retry/rerun-specific client.
- Existing `eject` and `ejected_by` vocabulary remains unchanged for thread
  operations and future cleanup.
- Default verification and the acceptance tests in this plan pass.

## Scope

In scope:

- new run identity for retry and rerun;
- source and retry-anchor lineage in preparation controls;
- source-prefix reconstruction for Flow and agic retries;
- attempt, retry-lineage, and thread usage/cost semantics;
- local executor, transport-neutral client, HTTP, CLI, projections, and tests;
- one intentional execution-store schema break without migration.

Out of scope:

- changing thread `create`, `fork`, or `rewind` behavior;
- renaming or removing `eject`, `ejected_by`, or their schema columns;
- copying run or Step records, adding a clone field, or rewriting source paths;
- exposing public `create`, `clone`, or `trim` storage commands;
- changing run, cancel, steer, client connection, or executor lifecycle
  semantics established by the run-control vocabulary change;
- custom model-catalog configuration or sandbox-selection policy.

## Durable Model

### New Run Identity

`RunExecutor.run`, `retry`, and `rerun` all return a handle for a newly accepted
root run. Unless a caller supplies an ID for deterministic testing, the
executor allocates one for every operation.

Every root run permanently points to `ControlRef(run.id, 0)`. Its preparation
control kind is `run`, `retry`, or `rerun`; later steer and cancel controls do
not replace that reference.

The derivation payloads use the same lineage names:

```text
RerunControlPayload(..., source: RunId)
RetryControlPayload(..., source: RunId, anchor: StepPath | None)
```

`source` identifies the terminal root run from which the invocation is
derived. A retry `anchor` identifies the first source-lineage Step that will be
executed again after boundary normalization. It remains a source Step path and
is `None` when there is no reusable Step boundary.

The source may be any visible terminal root run physically owned by the target
thread. Multiple retry or rerun targets may derive from the same source; their
controls preserve the relationship without mutating the source. Deriving from
a fork-inherited projected run is deferred to the thread design. State,
sandbox, runnable, and request identity validation complete before acceptance.

### Source Records Remain Unchanged

Retry and rerun do not write `ejected_by` on the source root or any source child
run or Step. They do not change source control, status, output, error, or
timestamps. Both source and derived roots remain visible in chronological
thread history and directly inspectable by ID.

The existing ejection fields and projections remain intact. Thread rewind and
other current owners continue to use them. Renaming ejection to archival or
removing Step ejection storage is a later cleanup, not part of this change.

Acceptance inserts only the new root and its index-zero control in one
`BEGIN IMMEDIATE` transaction. Concurrent derivations with distinct target and
request IDs may all succeed. Validation or uniqueness failure leaves the store
unchanged. A later execution failure leaves the source and failed derived run
visible.

### Linked Retry Prefix

Retry does not copy source Steps into the target run. The retry control links
the target to the source and normalized anchor. The executor builds a trimmed
source-prefix view in memory:

```text
source effective history before anchor + target Steps from anchor onward
```

Trimming is logical. No source row is changed or deleted. The target owns only
Steps that it actually executes. Source-prefix paths keep their source run IDs;
new suffix paths use the target run ID and the corresponding logical indices.
For example, a retry may read `old.0` and `old.1`, then execute `new.2`.

New Step inputs point directly to the source values they consume. Pointer
resolution already crosses run boundaries, so the store does not need a clone
field, copied output, or rewritten prefix path. Run inspection lists only the
Steps physically owned by that run; its preparation control provides the
lineage needed to inspect reused history.

Repeated retry follows retry controls recursively. If run `b` reused a prefix
from `a`, retrying `b` may reuse the effective prefix from `a` plus committed
Steps physically owned by `b`. Lineage traversal rejects cycles, missing
sources, invalid anchors, and state or runnable mismatches.

No Step event is emitted for a reused source Step. The target stream contains
only its own `RunBegin`, newly executed Step events, and `RunEnd`.

## Retry Boundaries

Retry inherits the source invocation and recorded state. It may apply the
existing supported model, resource-ceiling, and limit overrides, but must use
the source state revision, runnable identity, and canonical sandbox. A mismatch
is rejected before the target run is accepted.

The existing explicit/default anchor selection rules remain. The selected Step
is normalized to a safe executable boundary:

- For a Flow, normalize a nested or child-run Step to its owning top-level Flow
  statement. Reuse every complete preceding statement and its successful
  descendants through their original records. Execute the selected statement
  and suffix under the target run.
- For an agic, normalize to the beginning of the model/tool turn containing the
  selected Step. A reusable turn starts with a succeeded model Step and has a
  provider-valid, completely paired set of succeeded tool results before the
  next model Step. Reuse only complete preceding turns, then execute the
  selected turn and suffix under the target run.

The executor reconstructs Flow locals or agic messages from the linked prefix
and starts target path allocation at the normalized logical index. A source
with no reusable prefix starts the target from its original input. Running,
failed, and canceled source Steps are never part of the reused prefix.

## Rerun Semantics

Rerun inherits the source invocation but prepares it against the requested
fresh state, model, limits, resources, and sandbox. It records `source` for
lineage and presentation but reuses no Step history and executes from index
zero. The source remains visible and unchanged.

## Usage And Cost

Every stored Step was actually executed once. Accounting therefore has no
clone marker or clone-exclusion branch:

- **attempt usage** sums Steps physically owned by one root run tree;
- **retry-lineage usage** follows retry sources and sums each physical Step
  once, including work later trimmed from the effective prefix;
- **thread usage** sums physical Steps owned by the thread once, including all
  visible retry and rerun attempts.

Retry enforces token, model-call, tool-call, and cost limits against
retry-lineage consumption, so repeated retries cannot reset a consumptive
budget. Wall-time limits apply independently to each attempt. Rerun records a
source relationship but starts a fresh limit lineage.

Run summaries use attempt usage by default. Thread totals reflect actual spend
across all attempts. A lineage projection may expose cumulative retry usage,
but effective-prefix reconstruction must never be used as a cost aggregate.

## Executor And Client Boundary

`RunExecutor` retains lifecycle `start`/`stop` and run-control operations
`run`, `retry`, `rerun`, `cancel`, and `steer`. Retry and rerun return a
`LocalRunHandle` for the new run ID.

Extend the single `RunClient` contract with async `retry` and `rerun` methods
that accept transport-neutral request values and return the existing
`RunHandle`. Local and HTTP implementations use the same acceptance and
streaming protocol as `run`; no `RunRestartClient` or operation-specific client
is added.

Canceling an accepted run remains `client.cancel(handle.run_id)`.
`disconnect()` releases client resources without canceling runs. Executor
`stop()` closes executor-owned work. These rules do not depend on whether the
client currently has a run.

CLI and API responses identify both source and target. Progress tracers bind to
the target run ID. Retry/rerun commands use `RunClient` when an AgentServer owns
execution and the local adapter otherwise. Sandbox matching remains the
previously defined runtime-selection concern.

## Schema And Compatibility

Advance the execution schema from version 30 to 31 and reject every older
version without mutation. No compatibility aliases or migration are provided.

The schema break changes only retry/rerun preparation payload lineage and the
semantics that retry targets a new run. Existing run and Step ejection columns
remain unchanged. Store validation rejects missing or cyclic sources, anchors
outside the effective source lineage, incompatible retry snapshots, and a
source outside the target thread's physical history.

## Implementation Phases

1. Implement payloads, atomic derived-run acceptance, linked-prefix
   reconstruction, accounting, and local executor behavior. Update local CLI,
   existing API operations, projections, and tests atomically.
2. Add retry/rerun requests to `RunClient`, implement local and remote streaming
   operations, and route AgentServer-backed commands through the client.
3. Define thread fork/rewind composition separately. Revisit ejection naming
   and storage only after its thread semantics are approved.

Each implementation phase is an independently reviewable stacked pull request.

## Implementation Touchpoints

- `src/toolang/execution/records.py`, `schemas.py`, `store.py`, `history.py`,
  `client.py`, and `remote.py`;
- `src/toolang/execution/executor/`, especially Flow resume, agic preparation,
  logical path allocation, and limit restoration;
- `src/toolang/api/schemas.py` and `routers/runs.py`;
- retry/rerun CLI orchestration and progress presentation;
- execution store, executor, API, remote-client, CLI, history, and accounting
  tests;
- execution architecture, record, API, and CLI documentation.

## Acceptance Tests

1. Retry and rerun allocate a new target ID while source records remain visible
   and byte-for-byte unchanged.
2. Concurrent derivations with distinct identities can share one source;
   duplicate target or request identities reject without partial records.
3. Flow retry reconstructs the linked prefix and executes the anchor and suffix
   with target paths, without copying or ejecting source Steps.
4. Agic retry reconstructs provider-valid complete turns and re-executes the
   selected turn without duplicating prior calls.
5. Repeated retry follows mixed source/target paths without rewriting them and
   rejects missing, cyclic, or incompatible lineage.
6. Retry rejects changed state, runnable, or sandbox before acceptance; rerun
   can use fresh values and reuses no source Steps.
7. Direct run inspection reports only physically owned Steps, while lineage
   inspection and pointer resolution can follow reused source records.
8. Attempt, retry-lineage, and thread accounting count every physical Step once;
   retry cannot reset consumptive limits and rerun starts fresh limits.
9. Existing thread ejection behavior and `ejected_by` projections remain
   unchanged, and schema 30 is rejected without mutation.
10. Local and remote `RunClient.retry`/`rerun` return handles for the target,
    stream only target events, and preserve disconnect/cancel behavior.
11. CLI and HTTP output distinguish source and target IDs and attach progress
    to the target.
12. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Agic reconstruction must reject a partial tool turn before sending malformed
  provider history.
- Recursive retry lineage can cycle or reference a missing Step unless control
  decoding and prefix construction validate every hop.
- Target Step indices may begin above zero. All path allocation and presentation
  code must treat sparse, cross-run effective history as intentional.
- Concurrent derivations require atomic target/control insertion and global
  request-ID uniqueness.

## Open Questions

None.
