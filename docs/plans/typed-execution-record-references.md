# Typed Execution Record References

## Status

Proposed.

## Goal

Give durable execution records one typed, reversible reference vocabulary before
thread views, history reads, and compaction are implemented. Every record can be
looked up by its canonical ID, field references remain explicit, repeated content
can be addressed by hash, and stored errors have an unambiguous JSON shape.

## Success Criteria

- Thread, Run, Step, Control, and content records have canonical string primary
  keys and can be selected by their corresponding Ref types.
- One deterministic grammar round-trips record, field, and typed references.
- Durable fields use the narrowest Ref type and serialize it as canonical text.
- Content blobs round-trip through a content-addressed table whose ID verifies
  the stored bytes.
- Run and Step errors are `null`, an error message object, or an error reference
  object; legacy error encodings are rejected.
- The store keeps its exact-version policy and leaves incompatible databases
  unchanged.

## Scope

Included:

- Ref types, Pointer parsing, canonical serialization, and reference resolution;
- canonical IDs and SQLite primary keys for core execution records;
- the content-addressed `contents` table and its store operations;
- Run and Step error types, codecs, propagation, resolution, and presentation;
- updates required in existing record, executor, API, history, and inspection
  consumers;
- deterministic offline tests and reference documentation.

Excluded:

- thread logical views, fork/rewind redesign, retry/rerun changes, or MVCC;
- bounded history tools, recall, compaction, or ModelCall assembly;
- automatically moving existing ModelCall or file content into `contents`;
- database migration, compatibility aliases, or legacy reference parsing;
- wildcard, query, relative, or mutable references.

## Reference Vocabulary

```python
RecordRef = ThreadRef | RunRef | StepRef | ControlRef | ContentRef

@dataclass(frozen=True, slots=True)
class FieldRef:
    record: RecordRef
    field: JsonPointer

@dataclass(frozen=True, slots=True)
class TypedRef:
    ref: FieldRef
    type: RuntimeType
```

`StepPath` is a Run-relative tuple of non-negative indexes. `StepRef` combines a
`RunRef` and `StepPath`. `ControlRef` combines a `ThreadRef | RunRef` target and
a non-negative control index. `FieldRef.field` is a non-empty RFC 6901 JSON
Pointer; a whole record uses its `RecordRef` directly. `TypedRef` always refers
to a field because a runtime type describes a selected value, not a whole
record.

Canonical forms are:

```text
term_ab12                              ThreadRef
run_ab12                               RunRef
run_ab12.0.1                           StepRef
term_ab12@2                            ControlRef
run_ab12@2                             ControlRef
sha256_<64 lowercase hexadecimal>      ContentRef
run_ab12.0/output/value                FieldRef
run_ab12.0/output/value:Text           TypedRef
sha256_<digest>/value:Json             TypedRef
```

`sha256_` is reserved for Content IDs and can no longer begin a Thread ID.
Indexes are canonical decimal text: zero is `0`, and other indexes have no
leading zeroes.

## Pointer

`Pointer` is the generic tagged facade used only where the reference type is not
known in advance, such as CLI input, inspection, and the generic resolver:

```python
Pointer(ref)
Pointer.parse(text)
```

It wraps one `RecordRef | FieldRef | TypedRef`. Its `type` is derived from the
wrapped value, `ref()` returns that value, and type-specific accessors return the
matching Ref or `None`. Pointer never contains an inline value. Durable record
fields remain annotated with concrete Ref types rather than Pointer.

Parsing is deterministic: recognize a TypedRef suffix, split a non-empty JSON
field pointer, then parse the record section by its reserved syntax. A malformed
reserved form, including a malformed `sha256_` value, is invalid and never falls
back to another Ref type.

## Durable Records

Every canonical record stores its own identity as `id: str`; constructors verify
that the string parses as the record's Ref type. Ref classes are used only by
fields that refer to another record or field.

SQLite uses these primary keys:

```text
threads.id
runs.id
steps.id
controls.id
contents.id
```

`StepRecord.id` is the complete StepRef text. `ControlRecord.id` is the complete
ControlRef text. Private indexed columns may retain a Step's Run and relative
path, or a Control's scope, target, and index, but canonical records expose only
`id`. All record kinds can therefore be fetched by ID without reconstructing a
compound database key.

Reference-bearing fields use their exact types. In particular,
`RunRecord.thread` uses `ThreadRef`, `RunRecord.parent` uses `StepRef | None`,
and `StepRecord.input` uses `tuple[FieldRef, ...]`. A Step records its immediate
data source; it does not replace a FieldRef with a transitive ContentRef found in
that field.

## Content Storage

```sql
CREATE TABLE contents (
    id    TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
```

The ID is the complete `sha256_<digest>` ContentRef. Writes and reads verify the
SHA-256 digest against the raw blob. The table stores no kind, filename, or
algorithm column. The owning field and codec determine whether bytes represent
UTF-8 text, canonical JSON, or file content. Only hashes backed by this table are
ContentRefs; revisions and fingerprints remain their own domain values.

## Errors

No aggregate error alias is introduced:

```python
@dataclass(frozen=True, slots=True)
class ErrorMessage:
    message: str

@dataclass(frozen=True, slots=True)
class ErrorRef:
    ref: FieldRef

error: ErrorMessage | ErrorRef | None
```

The canonical JSON value is exactly one of:

```json
null
{"type": "message", "message": "model request timed out"}
{"type": "ref", "ref": "run_ab12.0/error"}
```

The parser rejects missing, extra, or unknown fields. An ErrorRef must reference
exactly `/error` on a RunRef or StepRef. The Step that catches an exception owns
the ErrorMessage; enclosing Steps and Runs store ErrorRefs. A failure outside a
Step is an ErrorMessage on its Run. Resolution follows ErrorRefs until a message
is reached and rejects cycles, missing targets, null targets, and non-error
targets. Human presentation may retain an unresolved Ref as diagnostic evidence,
but canonical JSON never follows it.

## Compatibility

The implementation increments the execution-store schema once for the complete
change. It creates only the new schema and accepts only that exact version. An
older or newer database is rejected before any read or write and remains
byte-for-byte unchanged. Deleted records, including Steps removed by a future
retry design, invalidate their Refs; this feature does not provide MVCC.

## Likely Files

- `src/toolang/execution/types.py`, `records.py`, `schemas.py`, and `store.py`
- `src/toolang/execution/executor/`, `history.py`, `trees.py`, and `events.py`
- execution inspection and progress projection code under `src/toolang/cli/`
- execution unit and integration tests, plus `docs/run-step-records.md`

## Acceptance Tests

1. Every canonical Ref form parses, serializes, and resolves to the intended
   record or field; malformed and ambiguous forms fail deterministically.
2. Every core record round-trips with its string ID, and Step and Control lookups
   use those IDs while private query columns remain consistent.
3. Concrete Ref fields reject the wrong record kind; TypedRef rejects whole
   records and invalid runtime types.
4. Content bytes deduplicate by ID, survive restart, and fail validation when the
   ID or stored bytes do not match.
5. `null`, ErrorMessage, and chained ErrorRefs round-trip and render correctly;
   malformed objects, invalid targets, missing targets, and cycles are covered.
6. Opening an incompatible store fails without modifying it.
7. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
   `uv run pytest` pass.

## Risks

- This intentionally breaks persisted IDs, Pointer text, error JSON, and direct
  SQLite consumers; the schema boundary prevents mixed interpretation.
- Ref changes touch many consumers even though runtime behavior is unchanged;
  implementation should remain mechanical and avoid unrelated record redesign.
- Reserving `sha256_` invalidates a previously legal Thread-ID prefix.

## Open Questions

None.
