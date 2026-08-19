# Define Typed Step Facts

## Goal

Replace loose Step `given`, `noted`, and placement dictionaries with typed,
durable execution facts. Flow Steps expose their lowered semantic statement;
model and tool Steps expose the dynamic call that actually occurred. Flow
execution uses one `evaluate -> transform -> bind` commit path.

## Success Criteria

- Every Step kind has a frozen, slotted dataclass `given` contract known before
  `StepBegin` is emitted.
- Flow Step `given` is the corresponding `FlowStmt`; it does not duplicate the
  statement, runnable, transform, binding, documentation, or source heading.
- Model and tool Steps store their actual typed calls and safe target identity.
- `noted` contains only end-time model accounting and provider state; Flow and
  tool results remain in `output` and child/part events.
- Flow results are evaluated, transformed, durably ended, and only then bound.
- Typed occurrences describe item, lane, and repeat iteration positions.
- Retry validates the original state fingerprint and restores committed Flow
  outputs without loose source or noted fields.
- Event, record, storage, API, inspection, and TUI projections round-trip the
  same typed facts.

## Scope

This change covers the lowered Flow AST vocabulary, Step fact dataclasses,
events, records, codecs, SQLite persistence, executor Flow commit handling,
retry validation, occurrence vocabulary, CLI/API projections, and tests.

It does not add statement-specific operation classes, replay an individual
historical model or tool call, store a complete `AgentState`, change provider
or plugin behavior, add multi-human syntax, or migrate old execution stores.
Using a different state remains a rerun rather than a retry.

## Fact Model

`StepBegin.kind` selects the `given` type:

```text
value | run | agent | human | par | loop -> FlowStmt
model                                  -> ModelStepGiven
tool                                   -> ToolStepGiven
```

`ModelStepGiven` contains the selected model identity and `ModelCall`.
`ToolStepGiven` contains the resolved plugin identity and `ToolCall`. Both are
frozen, slotted dataclasses. Model persistence may privately compact a call,
but caller-facing records always expand the typed value.

`StepEnd.noted` is `ModelStepNoted | None`. `ModelStepNoted` contains token
counts, token prices, total cost, and opaque provider state. Reasoning content,
Flow reshape/item counts, tool results, duration, child counts, and terminal
status are already represented by parts, output, timestamps, descendants, or
the Step envelope and are not repeated.

An `Occurrence` has optional typed item, lane, and iteration positions. An
iteration also records `body | until`; sentinel iteration numbers are not used.

## Flow Execution

The internal Flow path has three stages:

1. evaluate according to `StepKind` and the statement;
2. transform with `item`, `list`, `filter`, or `sort` semantics;
3. emit and persist `StepEnd`, then bind the durable result.

The legal combinations are:

```text
value -> item | filter
run   -> item | list
agent -> item
human -> item
par   -> list | filter | sort
loop  -> item | no result
```

Filter evaluators normalize keep/drop behavior to a keep mask. Sort evaluators
normalize rank direction to sort keys; the transform applies the optional
limit. A missing binding still commits Step output with no local name, but the
between-Step bind phase does not mutate locals. Repeat has no wrapper result or
binding; its nested statements commit normally.

Run, parallel, and settle evaluation invokes child Runs. Repeat invokes the
canonical Flow block executor. Agic execution dynamically emits model and tool
Steps. No statement-specific `FlowOperation` hierarchy is introduced.

## Lowered AST Vocabulary

The lowered semantic AST uses execution-facing names: `lanes` for parallel
width, `name` and `runnable` for seek, `name` and `request` for ask, shared
`runnable` fields for predicate/scorer/until evaluators, and `selection` plus
numeric `limit` for rank. Named and inline runnables remain one string reference;
the immutable state program resolves both named values and generated line-based
Agic names. Repeat explicitly has no binding.

## Retry And Persistence

Preparation controls store the state fingerprint used to resolve their
runnable. Retry requires the supplied state to have the same fingerprint.
Committed Flow Steps are validated against their typed statements, restored
from successful outputs, and rebound using the frozen program semantics.
`noted` never participates in retry.

The execution schema version advances and rejects older stores. Model calls
retain content-addressed private storage, secret model target fields remain
excluded, and all public projections expose the typed expanded facts.

## Implementation Touchpoints

- `src/toolang/lang/ast.py`, lowering, formatting, and language tests
- `src/toolang/execution/types.py`, `events.py`, `records.py`, and `schemas.py`
- `src/toolang/execution/store.py`, `_persist.py`, and executor preparation
- Flow statement executors, Flow run handling, model/tool Step emitters, retry
- CLI progress, Chat TUI, history, API projections, and their tests

## Acceptance Tests

- Every Flow statement round-trips as typed `given` and is compatible only with
  its expected Step kind.
- Model and tool calls round-trip through events, compact storage, records, and
  public schemas without secrets or duplicated call identifiers.
- Invalid kind/given and kind/noted combinations are rejected.
- Item, list, filter, and sort transforms commit the expected output; ignored
  bindings retain unnamed Step output and leave locals unchanged.
- Parallel and repeat descendants expose typed occurrences and no sentinels.
- Retry succeeds with the original state and committed prefix, and rejects a
  different state before restoring outputs.
- Progress and inspection render entirely from typed events/records.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest` pass.

## Risks

- AST field renames touch parser, formatter, executor, and presentation code and
  must remain atomic.
- Event unions use the enclosing Step kind rather than duplicating a discriminator
  inside each payload; codecs must validate this boundary explicitly.
- Model calls can be large and sensitive; compact persistence and safe model
  identity projection must remain intact.
- Retry correctness depends on rejecting state mismatches before any durable
  suffix execution or local restoration.

## Open Questions

None.
