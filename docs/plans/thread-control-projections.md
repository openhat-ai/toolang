# Thread control projections

## Goal and scope

Make fork and rewind logical history operations without copying or rewriting
Runs. This approved scope changes Thread records, Thread controls, store queries,
and their CLI/API readers. RunRecord and the runs table remain unchanged;
Run.ejected_by cleanup, compaction, and ModelCall assembly are deferred.

## Decisions

- Thread stores only `id`, `origin`, `peer`, `created_at`, and `updated_at`.
  Control `0` records creation; the latest applied Thread control is its head.
- Fork stores `fork_from`, `fork_at`, and `fork_head`. Its immutable prefix is
  the source view at `fork_head`, through `fork_at` inclusive.
- Rewind stores `rewind_from`, `rewind_through`, and `rewind_if`. It removes
  that closed interval from the current view. Later appends survive.
- A shared projection resolves inherited prefixes, then applies the destination's
  own rewinds. Root Runs use durable SQLite insertion order; child Runs remain
  inside their root's execution tree. Physical ownership never changes.
- Thread-filtered history and inspection follow logical root membership, including
  the selected roots' child Runs under the existing reader contracts. Direct
  record reads and unfiltered record inspection retain physical records.
- Fork requires every Run in its inherited root trees to be terminal. Retry
  rejects any root tree ever captured by a durable fork, even if later rewound.
  Both checks and mutations run under SQLite write transactions.
- Bump the execution schema once. Reject incompatible databases without changes;
  do not migrate them or alter the runs table.

## Touchpoints and acceptance

`execution/records.py`, `store.py`, a shared Thread projection helper,
`threads.py`, `history.py`, and `schemas.py`; existing CLI/API commands keep their
interfaces and derive Thread summaries from controls and projected history.

Tests cover pre/post-fork source rewind, inherited rewind, repeated/nested forks,
later appends, restart reconstruction, stable ordering, child isolation,
fork/retry races, unchanged physical Runs/Steps, and schema rejection. Run the
default offline verification suite.

## Risks

Every history reader must distinguish logical membership from physical ownership.
Closed rewind bounds and captured source heads must survive later appends and
rewinds. SQLite transactions, not process-local locks, protect forked content.

No open design questions remain for this scope.
