# Execution Record Vocabulary

## Goal

Make durable execution records use one explicit vocabulary for successful
completion, control outcomes, request storage, and propagated errors.

## Success Criteria

- Run and step success is stored and exposed as `succeeded`.
- `StepStatus` includes the reserved, currently unused `pending` value.
- Controls use `pending | applied | wontapply | revoked` in that order.
- Durable control records and SQLite columns use `request`; caller-facing
  Python and HTTP structures continue to use `request_id`.
- Run and step errors use `ExecutionError = str | StepErrorRef`.
- Failures outside a step no longer create a synthetic system step.
- The default verification suite passes.

## Scope

### Statuses

Use these exact status types:

```text
RunStatus     pending | running | succeeded | failed | canceled
StepStatus    pending | running | succeeded | failed | canceled
ControlStatus pending | applied | wontapply | revoked
```

`StepStatus.pending` is vocabulary reserved for future use and is not emitted
by this change. Control meanings are:

- `pending`: accepted but not yet applied.
- `applied`: consumed by the runtime or synchronously completed.
- `wontapply`: terminal because the target checkpoint can no longer be reached.
- `revoked`: explicitly withdrawn before application.

All persistence, events, projections, CLI presentation, and public read schemas
use the new status values directly. There is no compatibility translation for
new records.

### Request Boundary

`RunControlRecord.request`, `ThreadControlRecord.request`, and their SQLite
columns use the shorter durable field name `request`. Store indexes and queries
use the same column name.

Caller-facing names remain `request_id`, including executor and thread-manager
parameters, HTTP request bodies, and `RunControlInfo.request_id`. Mapping to the
durable `request` field happens at the record/schema boundary.

### Execution Errors

Define the vocabulary in `execution/types.py`:

```python
@dataclass(frozen=True, slots=True)
class StepErrorRef:
    step: StepPath

ExecutionError = str | StepErrorRef
```

The step that directly catches an exception stores its message. An enclosing
step or run stores a `StepErrorRef` pointing to the child step that owns the
detail. A runtime failure outside any step stores its message directly on the
run. Persistence serializes these values as a JSON string or
`{"step": "run_id/path"}`.

`RunInfo.failure` and `FailureDetail` are removed because they duplicate the
recorded error chain. Display code resolves references only when it needs a
message.

### Explicitly Out of Scope

- Typing `RunRecord.context`, `StepRecord.given`, or `StepRecord.noted`; they
  remain open dictionaries.
- Changing caller-facing `request_id` names.
- Emitting `StepStatus.pending`.
- Migration design for existing stores.

## Implementation Touchpoints

- `src/toolang/execution/types.py`, `records.py`, `events.py`, and `schemas.py`
- `src/toolang/execution/store.py` and executor failure propagation
- CLI/work projections that consume statuses or display errors
- HTTP schemas and routers at the durable/public request boundary
- Execution, store, API, and presentation tests

## Acceptance Tests

- Successful runs and steps persist and present `succeeded`.
- Controls transition to `applied`, `wontapply`, and `revoked` as appropriate.
- New databases contain `request`, not `request_id`, in both control tables
  while public request bodies still accept `request_id`.
- Direct step, enclosing-step, and runtime failures round-trip through storage.
- Runtime failures do not add a synthetic system step and remain retryable.

## Risks

- Status values and durable column names are intentional compatibility breaks
  for direct record/SQLite consumers; repository consumers update atomically.
- Error references can point to an unavailable step in partial projections;
  display falls back to the referenced step path.

## Open Questions

None.
