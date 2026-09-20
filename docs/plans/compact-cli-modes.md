# Compact CLI execution modes

Status: implemented. Algorithm execution and persistence follow
[Text-only algorithms](compact-summary-algorithm.md).

## Goal and interface

Support three producers behind the existing compaction result contract:

```sh
toolang AGENT compact --algorithm DEFAULT thread=THREAD before=RUN
toolang AGENT compact --algorithm ./compact.too thread=THREAD before=RUN
toolang AGENT compact --algorithm FORGET thread=THREAD before=RUN
```

- `--algorithm DEFAULT` (also the default when omitted): execute the bundled
  `agic compact` with existing model selection.
- `--algorithm FILE`: execute `agic compact` from a local UTF-8 `.too` file.
  Resolve and read the file once before admission. Require the existing compact
  text-only signature defined in [Text-only algorithms](compact-summary-algorithm.md).
  The framework assembles the complete result from frozen coverage and returned text.
  Use the target agent's store/model settings and isolated history tools, as
  the bundled producer does. No implicit entry selection, extra arguments,
  adjacent project configuration, or extra tool permissions.
- `--algorithm FORGET`: no model selection or provider call. Require explicit
  `before=RUN`; cover `[first_root, before)` and retain at least one terminal root. Reject
  explicit `--model`. The CLI accepts only `thread` and `before`, not `begin`,
  `end`, `bare`, or `previous`. Discard previous summaries for that prefix, even when repeating its existing boundary.
- A single `--algorithm` selects the producer; `DEFAULT` and `FORGET` are
  reserved case-sensitive values, and other values are file paths. Existing
  model/limit flags retain their meaning for both script modes. CLI help is
  a stable framework interface, independent of the selected script's declaration.

## Shared behavior

`before` is exclusive and maps to the result contract's `end`. Script modes
default to the latest terminal root; reuse a valid previous summary when it
ends at or before the selected boundary. Otherwise start from the first root.
No public interval or forced-fresh mode is included.

Resolve bounds and previous reuse before execution; freeze the request and
source, acquire the existing per-thread lock, and recheck coverage before and
at completion. Freeze the latest applicable summary reference in the same read
snapshot as the range, and reject requests if it changes while waiting for the
lock (including a first summary appearing). Preserve active-root, range, and
incremental checks.
Store each successful result as an immutable compaction record in
`compact_<thread>`. Return `{horizon, output}` and include the producer `run`
only for algorithm modes. All result fields remain concrete.

Forget uses the marker `Earlier history was intentionally forgotten.` and the
same validation/publication path, without running a model or a result flow.
Original records remain
inspectable. New model calls adopting this horizon see only the marker and
retained history; existing calls are not rewritten. Subsequent incremental
compaction may reuse the marker but must not reread the forgotten prefix.

## Scope and implementation touchpoints

CLI mode selection applies only to explicit requests; automatic compaction uses
the bundled producer. Both paths share text-only execution and publication.

- `cli/toolang/commands/compact.py`, `cli/toolang/main.py`: stable help,
  producer selection, input validation, and shared execution orchestration.
- `state/source.py` / `state/builtin.py`: reuse source parsing and isolated
  state preparation; keep file resolution in CLI orchestration.
- Shared framework execution/publication under `execution/compaction.py`.
- Existing compact CLI and model assembly integration tests; CLI documentation.

## Acceptance and risks

- Default producer behavior retains the same CLI usage; custom source is executed and
  malformed signatures/outputs fail without publishing an eligible horizon.
- Forget works without a configured model or credentials, makes zero provider
  calls, and persists a valid horizon across restart/replay.
- Forgotten content and previous summary text are absent from the next model
  request; retained tool pairs, reasoning, and budget admission remain valid.
- Cover flag conflicts, missing files/before, invalid/active ranges, append/rewind
  during execution, and incremental compaction after forgetting.
- Run Ruff checks/format, ty, and the complete default offline pytest suite.

Forgetting deliberately loses model-visible detail, not durable history.
External scripts may fail or produce poor summaries; validation establishes
coverage and structure, not summary quality. Open questions: none for this
approved scope.
