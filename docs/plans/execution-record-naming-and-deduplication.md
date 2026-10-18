# Execution record naming and deduplication

Status: definition only; implementation requires approval of this plan.

Apply the confirmed vocabulary and remove two duplicated execution facts.
Success means one persisted owner per affected fact, consistent field/type names,
and unchanged execution, recovery and presentation apart from these contracts.

## Naming

| Owner | Current | New |
| --- | --- | --- |
| Control record / column | `request` | `request_id` |
| Run / Step internal columns and attributes | `begin_cursor`, `end_cursor` | `_begin_cursor`, `_end_cursor` |
| Control internal column and attribute | `event_cursor` | `_event_cursor` |
| Chore checkpoint record / columns | `chore_id`, `active_run_id` | `id`, `active_run` |
| `stats.db` tables | `stats_roots`, `stats_buckets`, `stats_state` | `roots`, `buckets`, `state` |
| Step | `given`, `noted` | `spec`, `report` |
| Run | `noted` | `report` |

Apply recording vocabulary to codecs, query/event fields, annotations, imports,
validation/accounting helpers and fixtures, including these type families:

| Current types | New types |
| --- | --- |
| `StepGiven`, `StoredStepGiven` | `StepSpec`, `StoredStepSpec` |
| `ModelStepGiven`, `StoredModelStepGiven` | `ModelStepSpec`, `StoredModelStepSpec` |
| `ToolStepGiven` | `ToolStepSpec` |
| `StepNoted`, `RunNoted` | `StepReport`, `RunReport` |
| `ExecStepNoted`, `ModelStepNoted`, `ToolStepNoted` | `ExecStepReport`, `ModelStepReport`, `ToolStepReport` |
| `CollectionStepNoted`, `LoopStepNoted` | `CollectionStepReport`, `LoopStepReport` |

Keep the [Pointer/Ref encoding](../run-step-records.md), relationship names,
`controls.index`, `controls.payload`, and `occur`. Run's definition stays in its
entry Control payload; introduce no `runs.spec`. Preserve compact model-call
storage and separate value-only event types. Input, output, status, error and
lifecycle times keep their existing roles outside `spec`/`report`.

Treat underscored cursors as internal metadata like `_claimed` and `_revision`:
exclude them from canonical record JSON and domain Pointer inspection. Public
stream cursor fields, encoding and replay semantics stay unchanged. Caller-facing
`request_id`, chore lookup parameters and HTTP `active_run_id` stay unchanged.

Talk/Hub storage relocation, further Stats optimization, removal of other
redundant columns/aggregates and additional renames are outside this scope.

## Deduplication

- Remove `steps.created_at` and its stored dataclass member; `started_at` owns
  the timestamp. Derive the existing HTTP `StepData.created_at` from it. Other
  entities retain their creation timestamps.
- Persist only `control` in an Exec Step's report. Read the target from the
  referenced `ExecControlPayload.runnable` for inspection and explicit record
  recovery. Live completion summaries use the runnable already available to the
  committed exec transition, without another Store read or persisted copy.
  Preserve atomic Control admission, source-Step completion and cursor stamping.

## Compatibility and risks

Canonical paths use `STEP/spec`, `RUN/report`, `STEP/report` and
`CONTROL/request_id`, including descendants. Remove the replaced paths and
canonical Step fields `created_at` and `report/runnable`; add no old-name or
`Given`/`Noted` type aliases. Consumers follow the report's Control reference to
`payload/runnable`. SQL, record, Pointer and event consumers must upgrade together.

Coordinate affected schema/API increments with
[Contract Version Boundaries](contract-version-boundaries.md), without choosing
counter values or implementing that consolidation here. Retain exact-version
rejection without rewriting incompatible execution stores; preserve old runtime
data separately and use fresh compatible execution storage. The old Stats cache
may be discarded and rebuilt from compatible records; retain cache lifecycle
policy and source history. Resolve exec targets from the referenced Control,
never from the Run's later state, and preserve stream recovery across the rename.

## Implementation touchpoints

- [ ] Update `src/toolang/execution/{types,records,store,schemas}.py`, Pointer
  inspection, SQL queries/indexes and record version manifests.
- [ ] Adapt executor producers, `events.py`, `event_projection.py`, `live.py`,
  `subscriptions.py` and consumers while preserving event/persistence separation.
- [ ] Update `src/toolang/driver/chore/{records,store,scheduler,inspection}.py`
  and projections; retain identity generation and scheduling semantics.
- [ ] Rename Stats tables/references under `src/toolang/stats/`; retain metrics
  and existing rebuild/query algorithms.
- [ ] Update API/CLI projections, templates and current documentation. Preserve
  historical plans; generate implementation changelog coverage with
  `too aid.too update_changelog`.
- [ ] Add the acceptance coverage below. Before implementation commits run
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pytest -n auto`, and `git diff --check`.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| Stores | Fresh schemas contain only the agreed names and round-trip on reopening; chore scheduling and Stats totals retain their behavior. |
| Records and Pointers | Renamed fields/types and nested paths work for every spec/report variant; retired names and private cursors are absent from domain inspection. |
| Timing | Successful, failed and canceled Steps preserve their start time; HTTP creation time is derived; snapshot/manifest queries do not use the removed column. |
| Exec | Committed reports contain only the Control reference; reopening resolves the exact target, failed admission leaves no accepted report, and live summaries require no new Store reads. |
| Recovery | Attach, reconnect, terminal-event deduplication and Hub publication preserve their behavior with the updated contracts. |
| Compatibility | Incompatible execution stores fail without mutation; a discarded Stats cache rebuilds to the same totals from compatible records. |

Extend existing record/store/inspection, flow-exec, subscription, chore and Stats
tests; keep the default suite offline. This documentation PR requires source/link
validation and `git diff --check`, without the code checks or full test suite.

## Open questions

None within the confirmed scope.
