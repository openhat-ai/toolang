# Define Derived Run Retry And Rerun

## Work Type

Feature definition. This plan supersedes the retry, rerun, and run-ejection
semantics in earlier execution plans. It does not implement the feature.

## Verified Current Behavior

- `retry` appends a control to an existing root run, marks a step suffix as
  ejected, clears the run result, and changes the terminal run back to pending.
- `rerun` creates a new root run, then marks the complete source run tree as
  ejected by the new control.
- A retried Flow resumes from the remaining visible steps. An agic retry ejects
  its complete step history and starts the agic again.
- Limit restoration scans previously executed model steps. Durable records do
  not distinguish reused work from work executed by the current attempt.
- `RunClient` exposes `run`, `cancel`, and `steer`; CLI and HTTP retry/rerun
  paths call `RunExecutor` directly.

## Goal

Make retry and rerun immutable run derivations. Every operation starts a new
root run, preserves the source records as historical truth, and records reused
steps without presenting them as newly executed work or charging their cost a
second time.

## Success Criteria

- `run`, `retry`, and `rerun` each accept exactly one new root run with a new
  run ID and an index-zero preparation control of the matching kind.
- Accepting a derivation never changes the source run's control, status,
  output, error, timestamps, steps, or controls.
- Retry reuses only a valid committed prefix and executes the selected boundary
  and suffix under the new run ID.
- Reused steps are ordinary, inspectable Step records with explicit clone
  provenance; they do not emit execution events or add usage or cost.
- The source run tree remains addressable by ID and is archived from effective
  thread history by the new derivation control.
- `RunClient` and `RunExecutor` expose the same run-control vocabulary without
  a retry/rerun-specific client.
- Original run, step, thread, and control records are never physically deleted.
  A clone may be removed only before execution can reference it.
- Default verification and the acceptance tests in this plan pass.

## Scope

In scope:

- retry/rerun identity, lineage, archival, prefix selection, and atomicity;
- clone provenance on reused Step records;
- Flow and agic retry reconstruction;
- attempt and lineage usage/cost semantics;
- local executor, transport-neutral client, HTTP, CLI, projections, and tests;
- one intentional execution-store schema break without data migration.

Out of scope:

- changing thread `create`, `fork`, or `rewind` behavior;
- exposing generic public `create`, `clone`, or `trim` commands;
- changing run, cancel, steer, client connection, or executor lifecycle
  semantics established by the run-control vocabulary change;
- migrating an older execution store;
- custom model-catalog configuration or sandbox selection policy.

## Durable Model

### Run Identity And Preparation

`RunExecutor.run`, `retry`, and `rerun` all return a handle for a newly accepted
root run. Retry no longer reopens its source. Unless a caller supplies a run ID
for deterministic testing, the executor allocates one for every operation.

Every root run permanently points to `ControlRef(run.id, 0)`. The preparation
control kind is `run`, `retry`, or `rerun`; steer and cancel controls may follow
at higher indexes but never replace the preparation reference.

The derivation payloads use the same lineage field names:

```text
RerunControlPayload(..., source: RunId)
RetryControlPayload(..., source: RunId, anchor: StepPath | None)
```

`anchor` is the first source step that will execute again after normalization;
it always uses the source run tree's path. It is `None` when the source has no
reusable step boundary. The source ID is stored separately so an empty retry
still has complete lineage.

The source must be the latest visible terminal root run in its physically
owning thread, which is also the target thread in this phase. Deriving from an
older or fork-inherited projected run requires an explicit thread operation in
a later feature. Validation of source visibility, state, sandbox, runnable, and
request identity completes before durable mutation.

### Archival Instead Of Ejection

Replace run `ejected_by` with `archived_by: ControlRef | None`. Accepting retry
or rerun marks every Run record in the source root tree with the new root's
index-zero control. It does not alter any other source field.

Archived records are excluded from effective thread history, latest-run
selection, and default listings. They remain available through direct run
inspection and explicit audit projections. Pointer resolution ignores archive
visibility and can continue to read archived source values.

Step ejection is removed. Retry no longer hides a suffix inside an existing
run, and thread operations work at run boundaries. The new schema therefore
has no Step ejection columns or `StepRecord.ejected_by` field.

The derived run and source-tree archival commit in one `BEGIN IMMEDIATE`
transaction. A validation, uniqueness, clone, or archival failure leaves both
the source and target unchanged. Once the new run is accepted, a later runtime
failure leaves the source archived and the new failed run visible; another
retry derives from that failed run.

### Reused Steps

Add `StepRecord.cloned_from: StepPath | None`. `None` means the step was
actually executed for its owning run. A clone copies the source step's durable
facts under the target run's corresponding local path and points
`cloned_from` at the original executed step.

Clone provenance is flattened. If a retry copies an existing clone, the new
record points to that clone's non-clone origin rather than forming a clone
chain. The origin must exist, must not itself be a clone, and is never deleted.

Copied facts retain their original `kind`, inputs, given, output, occurrence,
noted facts, status, error, and timestamps. Existing pointers are not rewritten;
they may resolve through archived source records. Only succeeded, committed
steps may be cloned. Clone creation emits no `StepBegin` or `StepEnd`, because
no work was performed.

Retry acceptance inserts only the normalized reusable prefix. It need not
insert and then delete a suffix. A store-internal trim may physically delete a
clone only in the same transaction, while the target run is still pending and
before any durable record can reference the clone. After execution starts,
clone records are immutable. Non-clone steps, runs, controls, and threads are
never physically deleted.

No public storage primitive is introduced in this phase. Atomic derived-run
acceptance owns creation, prefix cloning, and source archival. This keeps the
records reusable by a later thread design without prematurely coupling thread
operations to executor APIs.

## Retry Semantics

Retry inherits the source invocation and recorded state. It may apply the
existing supported model, resource-ceiling, and limit overrides, but it must
use the source state revision, runnable identity, and canonical sandbox.
A mismatch is rejected before the source is archived.

The existing explicit/default anchor selection rules remain. The selected
Step is then normalized to a safe runnable boundary:

- For a Flow, normalize any nested or child-run step to its owning top-level
  Flow statement. Clone every complete preceding top-level statement and its
  succeeded in-run descendants. Child Run records are not copied; cloned
  outputs may continue to point to their immutable source results.
- For an agic, normalize to the beginning of the model/tool turn containing the
  selected step. A reusable turn starts with a succeeded model step and contains
  a provider-valid, completely paired set of succeeded tool results before the
  next model step. Clone only complete preceding turns. The selected turn and
  every later step execute again.

The executor reconstructs Flow locals or agic messages from the cloned prefix,
then allocates all new paths within the target run. A clone at path `new.2`
may point to origin `old.2`, while the retried execution also creates `new.3`
and later paths without collision.

Retry of a source with no reusable prefix starts the new run from its original
input. Retry never copies a running, failed, or canceled Step into the target.

## Rerun Semantics

Rerun inherits the source invocation but prepares it against the requested
fresh state, model, limits, resources, and sandbox. It creates no cloned steps
and executes from the beginning. Its source is archived for the same linear
effective-history behavior as retry.

Rerun starts a new accounting lineage. Prior source usage is historical cost,
not consumption of the new run's limits.

## Usage And Cost

Durable accounting distinguishes:

- **attempt usage:** facts from non-clone steps actually executed in one root
  run tree;
- **retry-lineage usage:** distinct non-clone steps in the retry source chain
  plus the current attempt;
- **stored usage:** every non-clone step in the store, counted once regardless
  of archive visibility.

Clone `noted` facts remain inspectable but contribute zero usage and zero cost
to aggregations. Retry enforces token, model-call, tool-call, and cost limits
against retry-lineage consumption so repeated retries cannot reset a
consumptive budget. Wall-time limits apply independently to each attempt.
Lineage traversal follows retry controls, ignores rerun sources, and
deduplicates actual Step paths. Rerun limits start from zero.

Run and thread summaries must label or select their accounting scope instead of
silently summing visible cloned facts. A run's default summary uses attempt
usage. A thread total counts all non-clone attempts physically owned by that
thread, including archived attempts, so it reflects actual spend. An audit
projection may expose retry-lineage and store-wide usage separately.

## Executor And Client Boundary

`RunExecutor` retains lifecycle `start`/`stop` and run-control operations
`run`, `retry`, `rerun`, `cancel`, and `steer`. Both `retry` and `rerun` return a
`LocalRunHandle` whose ID is the new run ID.

Extend the single `RunClient` contract with async `retry` and `rerun` methods
that accept transport-neutral request values and return the existing
`RunHandle`. Local and HTTP implementations perform the same acceptance and
streaming protocol as `run`; no `RunRestartClient` or operation-specific client
is added.

Canceling a run obtained through a client remains
`client.cancel(handle.run_id)`. `disconnect()` releases client resources and
does not cancel a run. Executor `stop()` closes executor-owned work. These
lifecycle rules do not depend on whether the client currently has a run.

CLI and API responses identify both source and newly accepted run. Progress
tracers bind to the new run ID. Retry/rerun commands use `RunClient` when an
AgentServer owns execution and the local client adapter otherwise; sandbox
matching remains the previously defined runtime-selection concern.

## Schema And Compatibility

Advance the execution schema from version 30 to 31 and reject every older
version without mutation. No aliases or migration are provided.

The schema change includes:

- `runs.archived_by_target` and `runs.archived_by_index` replacing ejection
  columns;
- removal of step ejection columns;
- nullable step clone-origin run/path columns with a foreign key to `steps`;
- retry/rerun payload source and anchor shapes defined above.

Store validation rejects missing clone origins, clone cycles, non-succeeded
clone origins, archive references other than retry, rerun, or thread rewind
controls, and a retry whose cloned prefix does not match its normalized anchor.

## Implementation Phases

1. Implement schema, records, atomic derived-run acceptance, prefix
   reconstruction, accounting, and local executor behavior. Update local CLI,
   existing API operations, projections, and tests atomically.
2. Add retry/rerun requests to `RunClient`, implement local and remote streaming
   operations, and route AgentServer-backed commands through the client.
3. Define thread fork/rewind composition separately, reusing archive and clone
   provenance only after its history and ID semantics are approved.

Each implementation phase is an independently reviewable stacked pull request.

## Implementation Touchpoints

- `src/toolang/execution/records.py`, `types.py`, `schemas.py`, `store.py`,
  `history.py`, `client.py`, and `remote.py`;
- `src/toolang/execution/executor/`, especially Flow resume, agic preparation,
  path allocation, and limit restoration;
- `src/toolang/api/schemas.py` and `routers/runs.py`;
- retry/rerun CLI orchestration and progress presentation;
- execution store, executor, API, remote-client, CLI, history, and accounting
  tests;
- execution architecture, record, API, and CLI documentation.

## Acceptance Tests

1. Retry and rerun allocate a new root ID, keep both source and target directly
   inspectable, and never modify source execution facts.
2. Derivation acceptance and source archival are atomic under request-ID,
   concurrent acceptance, clone-validation, and injected SQLite failures.
3. Flow retry clones only the valid prefix, flattens repeated-retry origins,
   reconstructs locals, and executes the anchor and suffix with target paths.
4. Agic retry clones only provider-valid complete turns, reconstructs messages,
   and re-executes the selected turn without duplicating prior calls.
5. Retry rejects changed state, runnable, or sandbox before mutation; rerun can
   use fresh values and clones no steps.
6. Default history hides archived source trees, audit/direct inspection retains
   them, and pointers through archived records continue to resolve.
7. Clone rows emit no execution events, add no attempt/stored cost, and cannot
   reset retry-lineage limits. Rerun starts a fresh limit lineage.
8. Store validation rejects invalid clone and archive references, and schema 30
   is rejected unchanged.
9. Local and remote `RunClient.retry`/`rerun` return handles for the new run,
   stream matching native events, and preserve disconnect/cancel behavior.
10. CLI and HTTP output distinguish source and target IDs and attach progress
    to the target run.
11. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Reconstructing an agic from an invalid partial tool turn can send malformed
  provider history; turn normalization must be validated before acceptance.
- Clone facts contain accounting data by design; every aggregate must exclude
  clones explicitly rather than relying on archive visibility.
- Archived values remain pointer targets. Any cleanup code must preserve
  original records and reject deletion when references exist.
- Concurrent retry/rerun requests can otherwise archive the same source twice;
  latest-visible validation and source archival must share one write
  transaction.

## Open Questions

None.
