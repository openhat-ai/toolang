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
consumers only where the changed event fields require it; retain their algorithms
and behavior.

Out: RunClient return types, HTTP query/control projections, Chat recovery policy,
input-resolution restructuring, `MessageRecall`/`StateSync` ownership, Top/Hub,
Activity/Stats and retry. No database schema, new indexes, event journal, generic
projection framework or second hierarchy of internal events.

## Simplicity constraints

| Component | Required structural result |
| --- | --- |
| `RunStore` | Keep its public methods, SQL, transactions and Record formats unchanged; use its existing return values and cursor methods. |
| `_PersistSink` | Replace event dispatch with four thin typed operations. No duplicate validation, history reads, rendering, resolver or second state machine. |
| `RunExecutor` / `_Execution` | Change the existing call arguments and dependencies, not lifecycle policy. Add no state fields, caches, locks, tasks, retry branches or configurable hooks for Events. |
| Event construction | One small, pure field-selection path per event shape; no new dispatcher, adapter hierarchy, registry or shared projection framework. |
| Consumers | Adapt field access/decoding only; no new queries, protocol negotiation, recovery algorithms or UI redesign. |

Keep complete Given/Noted values and compact stored forms where they already
belong. Do not duplicate them into new internal fact/command classes or rename
the execution vocabulary as part of this work.

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

| Method | Inputs and responsibility | Existing Store result |
| --- | --- | --- |
| `begin_run(...)` | Run identity, control, occurrence and time; persist the start. | `RunRecord` |
| `begin_step(...)` | Step identity, complete `StepGiven`, input/state/control refs, occurrence and time; persist compact Step-begin facts and model content dependencies. | `StepRecord` |
| `finish_step(...)` | Status, internal `Output`, `StepNoted`, error, control provenance and time; persist the end. | `StepRecord` |
| `finish_run(...)` | Run identity, terminal status, output, error and time; persist terminal facts. | `RunRecord` |

The sink does not import `events.py`, construct subscriber payloads or resolve
refs for notification. Reuse existing Store methods and durable codecs; do not
add recording structures or perform another read merely to return a record the
Store already returned. Cursor writes remain in the owning transaction.

Keep State capture and Step preparation inside the existing execution lock.
Preparation callbacks produce internal facts rather than external `StepBegin`.
Move control application, relation adoption and active-run tracking to those facts
or write results; they must not depend on client Event fields. Use explicit
private method parameters and existing value types, not replacement
`InternalRunEvent` classes. Preserve the current single owner of each control
transition in the executor, within its current transaction; the sink writes
Run/Step facts and cursors. Do not copy control updates into both layers.

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
| `StepGiven`, Flow AST, complete model/tool preparation | Only operation/target/model/tool facts currently consumed by clients; no complete call, AST or preparation snapshot. Formatting remains with clients, not executor/store. |
| `FieldRef`, `TypedRef`, `ContentRef`, `ErrorRef` | Necessary resolved data or error text; no data pointers. |
| Internal `Output` | Concrete type and actual value/Parts; no reference alternatives or local-table binding. |
| `StepNoted`, model accounting | Only completion facts and usage/cost consumed by existing clients; no internal pricing/accounting structures. |
| Internal control provenance | Existing client-required application facts correlated by scalar Run ID/control index; not a persistence provenance chain or new control-event lifecycle. |

Define these value-only payloads and their codecs with the Events. Scalar status,
kind and occurrence vocabulary, canonical Parts and Deltas may be shared.
Choose an explicit field allowlist from existing consumers; do not replace
`StepGiven` with an opaque metadata bag or replicate CLI rendering in the core.
When output is required by the existing client contract, supply its actual value,
with nested runtime refs resolved and literal user data preserved. A stored
`Output` edge alone is not a reason to materialize it into an Event. Keep current
content delivery; independently removing repeated Part/terminal content or
filtering/truncating actual operation results is outside this change.

Construct live notifications from execution memory and the current operation's
write result. `Local.value` versus `Local.stored/ref` already separates actual
values from stored representations. Await obtains its target value as part of
execution; notification must not add Store/history reads, recalculate model
calls, rerun a tool, or retain another output cache to recover client data. Pass
the already available value to construction. Remove
`observation_event()` and observation-only wrappers when direct construction
makes them unused.

## Commit and publication

1. Prepare facts under the existing execution lock.
2. In the existing transaction, persist records, content, control transitions and
   cursors, and stage their small notifications.
3. Retain existing State/control adoption and cancellation ordering, using
   internal facts rather than external Events.
4. Publish through `Publication` after successful commit; rollback publishes none.

Event construction/validation may occur inside the transaction. Retain bounded
memory retention, cursor/checkpoint semantics, structural replacement and
subscriber-failure isolation. Live events come from memory. Existing explicit
record-based attachment/replacement snapshots remain separate state reads using
the same small client payloads and current budgets; they never reconstruct model
calls or become the normal event production path. Adapt their field projection
only; do not change snapshot algorithms, retention policy or critical sections.

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
- [ ] Adapt publication and explicit snapshot field construction in
  `execution/stream.py` and `subscriptions.py`; update shared Thread event field
  access only if required. Keep `ThreadManager` behavior unchanged. Update
  `stream_client.py`, stream frame codecs, transport and CLI field access only
  as required by the Event contract.
- [ ] Add focused acceptance coverage and update existing event/persistence docs
  in `docs/execution.md`, `docs/executor.md`, `docs/run-step-records.md` and
  `docs/api.md`; do not redesign query or recovery behavior.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`
  and `uv run pytest -n auto` before implementation commits. Record practical
  test results as a short summary.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| Structural simplicity | `RunStore` behavior/signatures/SQL and durable formats are unchanged; the sink is event-independent and thinner; executor gains no Event-specific state or strategy layer. No duplicate lifecycle schemas or transition logic are added. |
| Persistence independence | Sink and internal lifecycle/control updates work with facts/records without client Event objects or subscribers. |
| Durable parity | Exact model calls, message deltas, typed references, control provenance, usage/limits and supported legacy history remain unchanged; no schema migration. |
| Small direct Events | Live and snapshot metadata contains no internal reference-capable schemas, assembled model histories or ASTs; model-begin/summary size does not grow with earlier context. Actual operation output remains intact. |
| Failed commit | Acceptance, begin/end and content/control rollback publish nothing and adopt no failed candidate. |
| Atomic admission | Native/runtime spawn, exec, await and control receipts remain atomic with source-Step completion; cancellation races do not duplicate terminal events. |
| Execution and consumers | Flow/Agic, children, parallel/loop work, state adoption, compaction, cancel/steer and local/remote presentation retain behavior without new queries, output caches or history lookups for live notifications. |
| Stream lifecycle | Bounded retention, overflow, cursors, replacement, observer failures and detach-without-cancel preserve existing guarantees. |

Use deterministic offline coverage in `tests/unit/execution/`,
`tests/integration/execution/`, API streaming and CLI progress/Chat tests.

## Risks and open questions

The main risks are changing State/control ordering, duplicating already committed
terminal notifications or losing facts required by existing consumers. The tests
above must cover these boundaries. Review the implementation diff against the
simplicity constraints as well as running tests; moving complexity into new
helpers does not meet the goal. Event-schema changes require coordinated
subscriber upgrades; durable compatibility is unchanged.

No open scope choices remain. Human approval of this definition is required
before implementation; this PR contains the plan only.
