# Inspect Controls Collection

## Status

Approved by the human on 2026-08-30. Implementation may proceed in a separate
change.

## Goal

Let an Agent-level `inspect controls` query enumerate durable Thread and Run
Control records through the same collection projector used by `threads`, `runs`,
and scoped `steps`.

## Success Criteria

- `toolang AGENT inspect controls` is a valid root collection query.
- The collection contains every Thread Control and every visible Run Control in
  the selected Agent's execution store.
- Human output identifies each Control by its copyable Pointer and summarizes
  its kind, status, and creation time.
- JSON output is a bare array of canonical `ControlRecord` objects, with each
  object identical to inspecting the printed Control Pointer individually.
- Existing Pointer inspection, subject relations, projector behavior, and
  execution semantics remain unchanged.

## Current Behavior

The root subject registry accepts `threads` and `runs`, but not `controls`.
Consequently, `inspect controls` parses `controls` as a Thread Pointer and
fails with `record not found: controls`.

Thread and Run Controls already share the durable `controls` table. Individual
records are addressable as `THREAD@INDEX` or `RUN@INDEX`, and whole Control
records already use the `CONTROL FIELD` heading. The store exposes per-target
Control listing methods but has no method that returns the Agent-level visible
Control collection.

## Subject And Collection Semantics

Add `controls` as an Agent-to-Controls subject transition:

```text
controls                every visible Control record
```

The exact `controls` token becomes a reserved root subject, consistent with
`threads` and `runs`, and takes precedence over Thread Pointer parsing. The
result is unbounded and contains both durable scopes:

- every Thread Control in the selected Agent store, including `create`, `fork`,
  and `rewind` controls;
- every Run Control whose owning Run is visible under the existing Run
  visibility rules.

Run Controls are excluded when their Run is ejected or when the Run's parent
Step is ejected. This matches individual Control Pointer lookup and guarantees
that every JSON item can be inspected individually. Thread Control visibility
continues to match individual Thread Control Pointer lookup.

Records are ordered newest first by `created_at`. Equal timestamps are ordered
by `target` ascending and then `index` descending, giving a deterministic total
order without exposing internal persistence columns. Later status changes do
not move a Control because ordering depends only on immutable record identity
and creation data.

This change adds only the Agent-level collection. It does not add
`THREAD controls`, `RUN controls`, filters, limits, pagination, or additional
projectors. A Controls collection does not accept child subjects.

## Projection And Presentation

The subject selects the existing implicit `records` projector. Agent remains an
implicit root source, so the human table omits `AGENT` and uses:

```text
CONTROL             KIND       STATUS       CREATED
run_ab12@1          steer      pending      2026-08-30T01:01:00Z
term_ab12@0         create     applied      2026-08-30T01:00:00Z
```

The columns are:

- `CONTROL`: the full canonical `target@index` Pointer;
- `KIND`: the Control kind;
- `STATUS`: the existing human status display;
- `CREATED`: the canonical creation timestamp.

The table retains the existing collection style and empty-table behavior. It
does not add a separate `SCOPE` or `TARGET` column because the complete Control
Pointer already distinguishes Thread and Run targets.

`--json` emits a bare array in the same order. Each entry is produced by the
existing canonical record serializer and has exactly the same shape as
`inspect TARGET@INDEX --json`. In particular, no synthetic `scope` or Pointer
field is added, and JSON Pointer resolution behavior does not change.

## Scope And Compatibility

Included:

- an Agent-level `controls` subject and records projection;
- a store query for visible Thread and Run Controls with deterministic order;
- human and JSON collection rendering;
- tests and directly affected CLI documentation.

Excluded:

- scoped Control relations from Threads, Runs, Steps, or collections;
- changes to Control Pointer syntax, record schemas, or persistence;
- changes to control creation, application, cancellation, or visibility rules;
- new filtering, limiting, pagination, sorting options, or table columns;
- changes to the legacy history commands, HTTP APIs, or execution runtime.

Compatibility impact is limited to reserving the exact root token `controls`.
A Thread record whose id is exactly `controls` can no longer be selected by that
bare token, matching the existing reservation behavior for `threads` and
`runs`. Other Thread ids and all explicit Control Pointers are unaffected.

## Design Touchpoints

- `src/toolang/execution/store.py`: add an unbounded Agent-level Control query
  that applies existing Run visibility conditions and returns both scopes in
  the defined order.
- `src/toolang/cli/toolang/commands/inspect.py`: register the root subject,
  resolve it to the `records` projector, and render the `CONTROL` collection
  table.
- `tests/integration/cli/test_local_core_commands.py`: cover mixed Thread and
  Run Controls, ordering, exact human columns, canonical JSON equivalence,
  empty stores, reserved-token parsing, and child rejection.
- Store-focused tests: cover visible and hidden Run Control selection plus
  deterministic ordering at the new query boundary.
- `docs/api.md`: document `controls` alongside the existing root collections,
  its mixed scope, visibility, ordering, table summary, and reserved token.

No record, schema, database migration, HTTP, or runtime execution change is
required.

## Acceptance Tests

1. `inspect controls` succeeds and includes mixed Thread and visible Run
   Controls from the selected Agent store.
2. Human output uses `CONTROL`, `KIND`, `STATUS`, and `CREATED`; every first
   column value is a valid full Control Pointer.
3. `inspect controls --json` returns a bare canonical array in newest-first,
   deterministic order, and each entry equals `inspect POINTER --json`.
4. Controls belonging to an ejected Run or a Run below an ejected parent Step
   are absent, while visible Run Controls and Thread Controls remain present.
5. Controls with equal creation timestamps are ordered by target ascending and
   index descending; a status update does not reorder them.
6. An existing empty store returns an empty table or `[]`, while a missing
   execution store retains the current error.
7. Exact `controls` selects the collection even if a same-named Thread exists;
   Controls collections reject child subjects through the standard grammar
   error.
8. Existing `threads`, `runs`, scoped `runs`, scoped `steps`, Control Pointer,
   projector, human, and JSON behavior remains unchanged.
9. The complete default verification passes.

## Risks And Open Questions

The mixed collection may become large because it is intentionally unbounded,
consistent with existing inspect collections. The full Pointer and stable
ordering keep the result scriptable until a separately defined filtering or
pagination feature is needed. There are no open product questions in this
definition.
