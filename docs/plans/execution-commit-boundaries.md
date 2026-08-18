# Define Execution Commit Boundaries

## Goal

Make deterministic Flow values and agic steer inputs align with their durable
Step boundaries. Local value production commits at StepEnd; steer input commits
at the consuming model StepBegin.

## Success Criteria

- `value` replaces `system` in the durable Step vocabulary.
- Let and positional keep/drop emit value Steps; predicate keep/drop remains
  parallel work.
- Retry selects real failed value Steps without replaying an earlier call.
- A consuming model Step records steer input before executor messages change or
  a provider is invoked.
- Flow runs retain no unreachable steer-to-local path.
- Existing execution stores are rejected through a schema version break.

## Scope

This change covers Step vocabulary, deterministic Flow statement routing,
default retry anchors, staged agic steer messages, dead system-error
presentation, schema validation, documentation, and tests.

It does not type `given` or `noted`, add an update Step, change control payloads
or timing modes, support Flow steer, migrate old stores, or implement the
execution progress state machine.

## Design

### Value Steps

`StepKind` contains `value`, not `system`. Let and positional keep/drop use a
value Step whose successful output Local carries the binding. The executor
updates its runtime local table only after StepEnd persists. Predicate
keep/drop continues to use `par`.

There is no separate update Step. Durable dependencies remain in
`StepBegin.input`; the operation and binding remain in `given`; the produced
Local remains in `StepEnd.output`.

### Retry Anchors

An explicit anchor always wins, including a value Step. Without an anchor:

- use the latest visible failed, canceled, or running Step regardless of kind;
- for a failed or canceled Run with no incomplete Step, use the latest Step;
- for a succeeded Run, prefer the latest non-value Step and fall back to the
  latest value Step.

The anchor is the first invalidated Step, not the point after which execution
continues.

### Steer Application

Control acceptance only persists steer locals. At a model boundary, the
executor claims pending controls and computes staged messages without mutating
`_AgicState.messages`. It builds the exact ModelCall and emits StepBegin with
control pointers. After the event transaction persists the Step and marks the
controls applied, the executor installs the staged messages and invokes the
provider.

A failed StepBegin leaves messages unchanged and never invokes the provider.
StepEnd is too late because a failed model call has still consumed the steer.
The unreachable Flow `apply_steer` path is removed.

### Persistence

The execution schema version advances from 25 to 26. Version 25 stores are not
migrated. Step row decoding rejects `system` and every unknown Step kind.

## Implementation Touchpoints

- `src/toolang/execution/types.py`, `store.py`, and `executor/steps/`
- `src/toolang/execution/executor/stmts/`, `runs/flow.py`, and `steps/model.py`
- CLI progress and Chat presentation fallbacks
- execution, API, CLI, and model tests
- execution vocabulary and executor documentation

## Acceptance Tests

- Let and all positional keep/drop forms persist value Steps; predicate forms
  persist par Steps.
- A failed value Step is the default retry anchor.
- A failed Run after a succeeded value retries from that value.
- A succeeded Run ending in value prefers the preceding non-value Step.
- An explicit value retry anchor remains valid.
- A StepBegin persistence failure leaves agic messages unchanged and invokes no
  provider; successful steer input appears once in the model request.
- Version 25 stores and stored `system` Step rows are rejected.
- Default formatting, lint, type checking, and tests pass.

## Risks

- The vocabulary and schema version are intentional compatibility breaks.
- Mutable message aliasing could rewrite a captured ModelCall; the durable call
  and runtime message list must use separate list snapshots.
- Default retry selection must not leave an incomplete Step in the committed
  prefix.

## Open Questions

None.
