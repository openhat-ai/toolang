# Dot-Separated Step Paths

## Goal

Use one canonical dot-separated syntax for durable step paths everywhere that
Toolang stores, transmits, accepts, or displays them.

```text
run_root.2.3
```

This makes a `StepPath` identical to the step anchor already used by value
pointers. A pointer may still append an RFC 6901 value path:

```text
run_root.2.3/field/0
```

## Success Criteria

- Full step paths use `RUN_ID.INDEX[.INDEX...]`.
- Run-local step paths use `INDEX[.INDEX...]`.
- Slash-separated step paths are rejected rather than treated as aliases.
- Events, records, API payloads, controls, SQLite rows, CLI input, and
  presentation use the same syntax.
- Pointer value selection retains its slash-separated RFC 6901 suffix.

## Scope

Change `StepPath` parsing and serialization, pointer step-anchor construction,
retry anchors, persisted run parents and local step paths, documentation, and
the tests that define these contracts.

Do not change run IDs, zero-based step indexing, parent-child identity,
`ControlRef` syntax, executor scheduling, or JSON Pointer syntax.

## Design

`StepPath` keeps its structured representation:

```text
run: RunId
indices: tuple[int, ...]
```

Only its canonical text forms change:

```text
global: run_root.2.3
local:  2.3
```

Run IDs cannot contain dots, so the grammar is unambiguous. Every index is a
canonical non-negative decimal integer with no leading zeroes. `str(path)` is
the single source for both serialized `StepPath` values and Pointer step
anchors.

The run-store schema advances from 27 to 28. Toolang keeps the existing
exact-version policy: version 27 stores are rejected without mutation rather
than migrated. New rows store local paths such as `2.3`; full paths embedded in
run parents and retry payloads use `run_root.2.3`.

## Touchpoints

- `src/toolang/execution/types.py`
- `src/toolang/execution/records.py`
- `src/toolang/execution/store.py`
- `src/toolang/execution/executor/`
- `src/toolang/api/`
- `src/toolang/cli/`
- execution documentation and protocol tests

## Acceptance Tests

- Parse and serialize global and local dot-separated paths.
- Reject missing indices, negative indices, leading zeroes, and slash forms.
- Serialize event, API, record, and retry fields with dots.
- Persist local step paths, run parents, and retry anchors with dots.
- Accept full and run-local dot-separated CLI retry anchors.
- Preserve pointers such as `run_root.2.3/field/0`.
- Reject old and future run-store schemas without modifying them.
- Pass the default repository verification.

## Risks

This is an intentional breaking protocol and storage-schema change. External
consumers must switch from slash-separated StepPaths to dots, and existing
version 27 run stores cannot be opened by version 28.

## Open Questions

None.
