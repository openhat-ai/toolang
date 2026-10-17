# Separate client events from executor persistence

Status: proposed for review. Work type: feature definition; no implementation.

## Goal and success criteria

Make `RunEvent` exclusively an external subscription contract. Keep `_PersistSink`
as the executor's private persistence adapter, using internal facts instead of
Event objects. Preserve compact records, exact model-call recording, transactions
and the canonical memory stream.

Success means persistence and execution state changes do not consume client
Event types; subscribers receive only necessary direct data after commit.

## Scope

In: Event independence, `_PersistSink` implementation boundaries and the executor
call sites required to separate recording from notification. Adapt existing
consumers only where the changed event contract requires it.

Out: RunClient return types, HTTP query/control projections, Chat recovery policy,
input-resolution restructuring, `MessageRecall`/`StateSync` ownership, Top/Hub,
Activity/Stats and retry. No database schema, new indexes, event journal, generic
projection framework or second hierarchy of internal events.

## Verified current coupling

| Location | Current coupling |
| --- | --- |
| [`_PersistSink.on_event()`](../../src/toolang/execution/executor/_persist.py) | Consumes `RunEvent`, writes records and resolves some refs for observers. |
| [`RunExecutor._project_event()`](../../src/toolang/execution/executor/executor.py) | Combines persistence, control application and notification. |
| `_Execution.begin_step()` and relation/state helpers | Prepare and manage execution through `StepBegin`, `StepEnd` and `RunEnd`. |
| [`Publication.append()`](../../src/toolang/execution/stream.py) | Converts complete model Given data to `ModelStepInfo` before retention. |
| [`events.py`](../../src/toolang/execution/events.py) | Reuses internal Given/Noted/output schemas and reference-capable fields. |

## Persistence contract

Replace `_PersistSink.on_event()` with explicit methods over existing internal
facts. Keep cursor recording and authoritative write results with the operation.

| Method | Inputs and responsibility |
| --- | --- |
| `begin_run(...)` | Run identity, control, occurrence and time; persist the start. |
| `begin_step(...)` | Step identity, complete `StepGiven`, input/state/control refs, occurrence and time; persist compact Step-begin facts and model content dependencies. |
| `finish_step(...)` | Status, internal `Output`, `StepNoted`, error, control provenance and time; return the actual stored Step result where needed. |
| `finish_run(...)` | Run identity, terminal status, output, error and time; persist terminal facts. |

The sink does not import `events.py`, construct subscriber payloads or resolve
refs for notification. Reuse existing Store methods and durable codecs; do not
add recording structures or change existing Record formats.

Keep State capture and Step preparation inside the existing execution lock.
Preparation callbacks produce internal facts rather than external `StepBegin`.
Move control application, relation adoption and active-run tracking to those facts
or write results; they must not depend on client Event fields. Use explicit
private method parameters, not replacement `InternalRunEvent` classes.

Atomic spawn, exec, await and runtime-control admission already finish some
source Steps. Forward their completion facts to notification construction without
re-entering persistence through a manufactured `StepEnd`. Preserve the existing
cursor check that prevents duplicate terminal publication.

## External Event contract

Keep the Run/Step/Part lifecycle vocabulary. Events contain directly usable
client data, with the following boundaries:

| Internal data | External representation |
| --- | --- |
| Run/Step/control identity | Scalar IDs/indexes needed for correlation, including owning Run and parent identities. These never require a Store read. |
| `StepGiven`, Flow AST, complete model/tool preparation | Necessary operation/target/model/tool identity and factual summary; no complete call, AST or preparation snapshot. |
| `FieldRef`, `TypedRef`, `ContentRef`, `ErrorRef` | Necessary resolved data or error text; no data pointers. |
| Internal `Output` | Concrete type and actual value/Parts; no reference alternatives or local-table binding. |
| `StepNoted`, model accounting | Only completion facts and usage/cost consumed by existing clients; no internal pricing/accounting structures. |
| Internal control provenance | Actual application information correlated by scalar Run ID/control index; not a persistence provenance chain. |

Define these value-only payloads and their codecs with the Events. Scalar status,
kind and occurrence vocabulary, canonical Parts and Deltas may be shared.
Resolve nested runtime refs while preserving literal user data. Keep existing
content delivery and presentation behavior; independently deduplicating Part and
terminal output is outside this change. Do not filter or truncate actual operation
results; the event layer must stop adding internal history/preparation data.

Construct live notifications from execution memory and the current operation's
write result. `Local.value` versus `Local.stored/ref` already separates actual
values from stored representations. Await obtains its target value as part of
execution; notification must not reconstruct it from complete history. Remove
`observation_event()` and observation-only wrappers when direct construction
makes them unused.

## Commit and publication

1. Prepare facts under the existing execution lock.
2. In the existing transaction, persist records, content, control transitions and
   cursors, and stage their small notifications.
3. Adopt only committed execution state before subscriber delivery.
4. Publish through `Publication` after successful commit; rollback publishes none.

Event construction/validation may occur inside the transaction. Retain bounded
memory retention, cursor/checkpoint semantics, structural replacement and
subscriber-failure isolation. Live events come from memory. Existing explicit
record-based attachment/replacement snapshots remain separate state reads using
the same small client payloads and current budgets; they never reconstruct model
calls or become the normal event production path.

The external event schema changes together with the built-in consumers that use
it. Require external subscribers to upgrade and reconnect; do not retain a legacy
Event-as-persistence path. Stored history stays readable without rewriting it.
Record the event migration in the implementation changelog through the required
runnable.

## Action items and likely files

- [ ] Separate `_PersistSink` methods and lifecycle/control updates in
  `execution/executor/_persist.py`, `executor.py` and `common.py`.
- [ ] Adapt internal lifecycle producers in `execution/executor/steps/`, `runs/`,
  `tool_runtime.py`, `awaitables.py` and `spawn.py`, retaining atomic admission.
- [ ] Define value-only Events in `execution/events.py`; keep recording vocabulary
  in `types.py` and codecs in `records.py`.
- [ ] Adapt publication and explicit snapshots in `execution/stream.py`,
  `subscriptions.py` and `threads.py`; update `stream_client.py`, stream frame
  codecs, transport and CLI consumers only as required by the Event contract.
- [ ] Add focused acceptance coverage and update existing event/persistence docs
  in `docs/execution.md`, `docs/executor.md`, `docs/run-step-records.md` and
  `docs/api.md`; do not redesign query or recovery behavior.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`
  and `uv run pytest -n auto` before implementation commits. Record practical
  test results as a short summary.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| Persistence independence | Sink and internal lifecycle/control updates work with facts/records without client Event objects or subscribers. |
| Durable parity | Exact model calls, message deltas, typed references, control provenance, usage/limits and supported legacy history remain unchanged; no schema migration. |
| Small direct Events | Live and snapshot metadata contains no internal reference-capable schemas, assembled model histories or ASTs; model-begin/summary size does not grow with earlier context. Actual operation output remains intact. |
| Failed commit | Acceptance, begin/end and content/control rollback publish nothing and adopt no failed candidate. |
| Atomic admission | Native/runtime spawn, exec, await and control receipts remain atomic with source-Step completion; cancellation races do not duplicate terminal events. |
| Execution and consumers | Flow/Agic, children, parallel/loop work, state adoption, compaction, cancel/steer and local/remote presentation retain behavior without new history lookups for live notifications. |
| Stream lifecycle | Bounded retention, overflow, cursors, replacement, observer failures and detach-without-cancel preserve existing guarantees. |

Use deterministic offline coverage in `tests/unit/execution/`,
`tests/integration/execution/`, API streaming and CLI progress/Chat tests.

## Risks and open questions

The main risks are changing State/control ordering, duplicating already committed
terminal notifications or losing facts required by existing consumers. The tests
above must cover these boundaries. Event-schema changes require coordinated
subscriber upgrades; durable compatibility is unchanged.

No open scope choices remain. Human approval of this definition is required
before implementation; this PR contains the plan only.
