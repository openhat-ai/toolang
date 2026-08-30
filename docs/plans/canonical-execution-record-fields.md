# Canonical Execution Record Fields

## Goal

Make durable execution records use concise, uniform identity and occurrence
field names while preserving the vocabulary that describes shared execution
errors.

## Success Criteria

- `ThreadRecord` stores its identity as `id: str` and canonical inspection shows
  `/id`, not `/thread_id`.
- `RunRecord` stores its identity as `id: str`; `RunId` is no longer a distinct
  source-level alias and canonical inspection shows `str`.
- `RunRecord` and `StepRecord` store structural occurrence facts as
  `occur: Occurrence | None`; SQLite and canonical inspection use `occur`.
- Run and Step errors continue to use the shared `ExecutionError` vocabulary.
- Caller-facing method parameters and HTTP schemas keep their existing names
  unless they directly expose one of the changed durable records.
- New stores use one exact schema version and incompatible stores remain
  unchanged.

## Canonical Decisions

The durable record and SQLite mappings are:

| Record | Current field | New field | Declared type |
| --- | --- | --- | --- |
| `ThreadRecord` | `thread_id` | `id` | `str` |
| `RunRecord` | `id` | `id` | `str` |
| `RunRecord` | `occurrence` | `occur` | `Occurrence | None` |
| `StepRecord` | `occurrence` | `occur` | `Occurrence | None` |

The corresponding SQLite columns are `threads.id`, `runs.occur`, and
`steps.occur`. Canonical record JSON uses the dataclass field names, so Pointer
paths become:

```text
THREAD/id
RUN/occur
STEP/occur
```

`THREAD/thread_id`, `RUN/occurrence`, and `STEP/occurrence` no longer resolve.
No compatibility aliases are added to record JSON or Pointer traversal.

`Occurrence`, `OccurrencePosition`, and `IterationOccurrence` keep their class
names. `occur` names the compact durable field; prose continues to call the
concept an occurrence.

## Identifier Types

Remove `RunId = str` from `execution/types.py` and annotate all current uses
with `str`, including `RunRecord.id`, `StepPath.run`, Pointer constructors, and
control payload boundaries. Validation remains owned by `valid_run_id()` and
`validate_execution_id()`; replacing the alias does not weaken runtime checks.

Thread IDs already use `str`. Rename only the durable `ThreadRecord` field to
`id`; store APIs may continue accepting parameters named `thread_id` because
those names describe lookup arguments, not canonical record members.

## Error Vocabulary

Keep:

```python
ExecutionError = Annotated[str | Pointer, ...]
```

The same type represents a direct message or a Pointer to the Step that owns
the detail. It is used by Runs, Steps, execution events, history resolution,
work inspection, and CLI presentation. `RunError` would incorrectly imply that
the value belongs only to Runs. Do not add record-specific aliases until Run
and Step errors have different invariants or serialization.

Canonical inspection therefore continues to show:

```text
/error  ExecutionError?  null
```

## Boundary Mapping

Only durable record vocabulary is shortened. These caller/runtime names remain
unchanged:

- store and manager arguments such as `thread_id`;
- transient event and executor arguments named `occurrence`;
- HTTP summary fields such as `RunInfo.thread_id`, `RunInfo.occurrence`, and
  `StepData.occurrence`;
- descriptive human headings such as `OCCURRENCE`.

The store projection maps event `occurrence` to record `occur`. Public schema
builders map record `occur` back to their established caller-facing
`occurrence` fields. `ThreadInfo` already exposes `id` and maps directly from
`ThreadRecord.id`.

## Store Compatibility

Bump the RunStore schema from version 32 to version 33. Version 33 creates only
the new column names. Preserve the current exact-version policy:

- do not migrate or rewrite version 32 databases;
- do not read version 32 through column-name fallbacks;
- reject incompatible stores before reading or writing them;
- leave rejected files byte-for-byte unchanged.

Users must recreate execution history after the schema change. Migration or
multi-version reading requires a separate approved definition.

## Scope

Included:

- durable dataclass fields and canonical record JSON;
- SQLite schema, queries, row decoding, and schema-version validation;
- removal of the `RunId` alias and replacement of its annotations with `str`;
- event-to-record and record-to-public-schema boundary mappings;
- Pointer inspection, documentation, and deterministic offline tests.

Excluded:

- changes to ID syntax, prefixes, allocation, or validation;
- changes to occurrence structure or semantics;
- renaming HTTP request, response, route, or store-method parameters merely for
  consistency;
- renaming `ExecutionError` or changing its serialization;
- migration, export/import, or compatibility aliases for schema version 32;
- historical execution trees or new inspect projectors.

## Design Touchpoints

- `src/toolang/execution/types.py`: remove `RunId`; retain occurrence and error
  vocabulary plus validation.
- `src/toolang/execution/records.py`: rename durable fields and update explicit
  `str` annotations.
- `src/toolang/execution/store.py`: schema version, DDL, SQL, row decoding, and
  event-to-record mapping.
- `src/toolang/execution/schemas.py`: canonical record serialization/type
  display and mappings to existing caller-facing schemas.
- `src/toolang/execution/history.py`, executor, work, API, and CLI consumers:
  use the renamed durable attributes without changing their public contracts.
- `docs/run-step-records.md`: canonical fields, Pointers, and schema version.
- store, execution, API, CLI, and schema unit/integration tests.

## Acceptance Tests

1. A new database has `threads.id`, `runs.occur`, and `steps.occur`, and lacks
   the replaced columns.
2. Thread, Run, and Step records round-trip with the renamed dataclass fields
   and unchanged ID/occurrence validation.
3. Canonical JSON and `inspect` expose `/id` and `/occur`; the replaced Pointer
   paths report a missing field.
4. Run inspection renders `/id` as `str`, while `/occur` renders
   `Occurrence?` for both Runs and Steps.
5. Run/Step error strings and Pointers round-trip and still render as
   `ExecutionError?`.
6. Runtime events using `occurrence` project into durable `occur`, and existing
   HTTP summary fields still emit `occurrence` and `thread_id` where specified.
7. Store APIs named with `thread_id` retain their current behavior while
   `ThreadRecord.id` is used internally.
8. Opening a version 32 or unknown-version store fails before mutation; version
   33 reopens normally in read-only and read/write modes.
9. Ruff, formatting, ty, and the default offline pytest suite pass.

## Risks

- Direct SQLite, canonical JSON, and Pointer consumers break intentionally;
  documentation must list every changed path.
- Existing execution history becomes unreadable by the new binary because the
  store has no migration policy.
- Mixed durable `occur` and caller-facing `occurrence` names can leak across a
  boundary; tests must assert both sides explicitly.
- Removing `RunId` reduces a documentation hint but not runtime safety; all ID
  validation paths must remain covered.

## Open Questions

None.
