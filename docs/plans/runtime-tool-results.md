# Persist runtime tool results

## Goal and scope

Persist every runtime-generated ToolResult as a Tool Step output, independently
of a subsequent Model Call. Reuse existing records, parts, and control relations.
This is PR5 after bounded history reads.

## Decisions

- A model `_too/run` call is one Tool Step owning one child Run. Its `given`
  contains the ToolCall, `input` references that Model output part, and `output`
  contains the actual ToolResultPart. Authored Flow run statements remain Run
  Steps. Child-internal messages do not enter the parent conversation.
- Unknown reserved runtime calls produce failed Tool Steps. Steer-skipped calls
  with generated cancellation results produce canceled Tool Steps without
  invoking handlers or consuming additional tool-call budget.
- Started calls terminate their existing Step. Preserve a completed result if
  delivery is interrupted; do not replace it or append a duplicate cancellation
  result. Do not manufacture results for other unexecuted calls.
- Reuse reload/execute Tool Steps and preserve execute control transfer, routing,
  authorization, result formats, State snapshots, limits, and child cancellation.

No schema change, new runtime tool, message-template storage, recall trigger,
Model Call assembly redesign, compaction, or runspace work is included.

## Implementation and verification

Touchpoints: `executor/runs/agic.py`, Tool Step lifecycle in `executor/steps/tool.py`,
child output tracking, execution-tree/CLI readers, and focused integration tests.
Reuse child-run execution directly and remove the obsolete runtime-call options
from the Flow Run Step wrapper. Preserve existing runtime-run progress headers.

Acceptance checks:

- Success, rejection, and skipped-call results survive restart without a later
  Model Call; call IDs and exact output are preserved.
- Child Runs point to the owning Tool Step; parent history includes the result
  once and excludes child-internal messages. Flow Run Steps remain unchanged.
- Cancellation during execution or result delivery preserves the appropriate
  output and control relations. Interrupted begin delivery closes the existing
  Step. Immediate steer resumes with one result per call; a committed execute
  still transfers to its target.
- Retry removes and rebuilds the owning Step and child without stale results.
- Inspection/progress and the default offline verification suite pass.

Main risks are duplicate results during interrupted delivery and changing child
ownership assumptions. Exercise these through runtime integration tests. No open
scope decisions remain.
