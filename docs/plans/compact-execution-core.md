# Restore a compact execution core

Status: approved for implementation on 2026-10-16.

## Goal and success criteria

Remove the current execution Activity/Stats and manual Run retry support. Restore
a normal execution core that does not know either feature. Preserve the existing
executor, transactional Store and canonical stream foundations.

- Normal acceptance, execution, terminal persistence and memory publication have
  no Activity/statistics dependency, session, checkpoint or manual-retry mode.
- No runtime API, client or CLI path can invoke the removed Run retry operation.
- Keep typed records/references, authority checks, atomic commits, cancellation,
  asynchronous execution, ordinary rerun and bounded subscriptions working.
- Add no replacement Activity, Top or retry implementation in this change.

## Scope and preserved foundations

Use the current implementation as the base; do not reset to an older Git revision
or remove unrelated execution features. Refer to the pre-statistics code only to
verify the original layering.

The normal path remains: prepare concrete inputs, atomically accept a Run, execute
with resident state, commit terminal facts, then publish typed memory events.
The Store owns durable mutation; the canonical stream owns publication ordering,
bounded retention and subscriptions. A failed core commit remains a failure.

Keep exact captured model requests, native continuation state and model usage/
accounting required for token/cost limits and ordinary inspection. Keep existing
commit/subscription cursors, uncertainty handling and bounded recovery. Remove
only retry-specific invalidation and observation-driven aggregation from these
mechanisms. Do not add new snapshot, attempt, ledger or checkpoint abstractions.

Hub will no longer collect execution Activity or statistics. Do not redesign Hub
commands or implement a new Top. Obsolete Top-only code/registration may be removed
as dependency cleanup; its replacement is separate work.

## Remove Activity and statistics

Delete the execution statistics indexes and Activity projection/read/publication
chain: agent/Hub Activity APIs, backend cache/scripts, runtime publishers, display
schemas and obsolete consumers. Remove profile-wide derived statistics that scan
execution history. Messaging counts unrelated to execution Activity are outside
this scope.

Remove statistics imports, initialization/backfill, pre-commit flush and derived
indexes from RunStore. Remove the statistics session, periodic checkpoints and
shutdown settlement from RunExecutor. Core control polling must not share an
observation failure domain.

Writable Store opening removes only the known old Activity triggers/tables in
its schema transaction. Retain execution records, content objects, IDs and cursors;
read-only opening performs no cleanup. Do not reset an execution database to
remove derived indexes. Derived historical Activity totals are discarded.

## Remove current manual Run retry

Delete the retry entry points, request schemas and command registration, retry
orchestration, Store acceptance/trimming helpers, Flow-prefix/local restoration
and _retry threading through launch and execution. Remove live RunRetried
production and retry-only reset/invalidation handling from local/Hub streams.
No new truncation or continuation operation is introduced now.

Remove additional durable invalidation lists and any new-record writing of retry
payloads. Do not remove general record/provenance fields or generic reconnect
replacement handling simply because retry also used them.

Retain narrowly isolated read compatibility for existing durable retry controls
so historical Runs and their effective settings remain inspectable and usable
as ordinary rerun sources. This is historical decoding, not an execution feature:
no acceptance, resume, runtime mode, new retry record or mutable retry state.
Do not rewrite surviving historical controls or discard their execution facts.

Provider-call recovery, network reconnect and uncertain commit handling remain
with their existing owners. _restore_model_limits has a compaction-continuation
caller independent of manual retry; retain only that necessary behavior. Do not
reintroduce global Activity attempt accounting for this purpose.

Future retry work starts from this retry-free core. A later definition may add
truncate plus continue with small boundary hooks; that design and implementation
are explicitly deferred.

## Implementation order and touchpoints

1. Remove Activity/Stats from execution/store.py, statistics.py, activity.py and
   schemas.py; executor lifecycle; API and up composition; teaming Activity,
   backend/runtime wiring; and obsolete consumers/tests. Keep usage and limits.
2. Remove Run retry from executor, Store, clients, APIs, CLI registration, record
   writers and local/Hub observation. Isolate the historical codec compatibility.
3. Update active documentation and CLI examples; record removed commands/APIs and
   discarded derived totals in CHANGELOG.md using the required runnable.

Keep these as separate implementation changes. Do not combine a model-content
storage migration, detailed API projection redesign or a new user interface.
Preserve historical release entries and approved core contracts.

## Acceptance checks

- New Stores create no Activity tables/triggers. Existing writable Stores lose
  only those old derived objects; execution facts remain identical.
- No executor/Store runtime path depends on statistics/Activity code. Admission,
  Step completion, control polling and shutdown need no observation state.
- Agent/Hub Activity routes, cache/publication integrations and obsolete consumers
  are absent. Remaining Hub and CLI loading works without deleted modules.
- Run retry methods, HTTP routes and CLI entry points are unavailable. Fresh
  execution has no _retry mode, prefix recovery or retry invalidation protocol.
- Existing durable retry history remains readable, without enabling retry, and
  ordinary rerun creates a new Run without modifying its source.
- Normal Flow/Agic execution, asynchronous children/spawn, cancel/steer/exec,
  usage limits, exact calls, typed pointers and canonical subscriptions pass.
- Generic cursor/reconnect, rollback and uncertain-commit tests remain green;
  no event is published for a failed commit. Removed-feature tests are replaced
  by removal/compatibility checks rather than replacement feature tests.
- Default Ruff, formatting, type checking and offline test suite pass.

## Risks and approval

Old Activity/Top and Run retry interfaces are removed. Use ordinary rerun for a
new execution where appropriate; this does not provide retained-prefix resume.
Stop old writers before writable cleanup because they still expect Activity
indexes. No live service is restarted or runtime data reset by this definition.

No open behavior choices remain. Implementation requires human approval under
the repository work-type policy. This supersedes the prior truncate/continue
proposal for the current work; future retry design is deferred.
