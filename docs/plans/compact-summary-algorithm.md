# Text-only compaction algorithms

Status: approved. Replaces separate result records; no compatibility required.

## Contract

```text
compact(thread: Text, summary: Text, start: Text, begin: Text, end: Text) -> Text
```

All inputs are required. `summary` is previous text or `""`; read `[begin, end)`
and combine it with that text. `start` records complete coverage and is unused
by the algorithm. Require `start <= begin < end`; initially `start == begin`.
Bundled and external algorithms share this signature and isolated history tools.
The bundled prompt uses implicit user prose.

Reconstruct `CompactionResult(thread, start, end, output)` from the successful
summary Run's entry input and Text output. Its four fields are concrete,
nonempty strings defined in base. No result wrapper, extra coverage metadata,
or recursive summary chain is needed. Explicit intervals are valid results;
published thread horizons must cover the complete prefix and retain a terminal root.

## Persistence and adoption

- `ThreadRecord.horizon` stores the latest published summary `RunRef` or null.
  All horizon references identify a Run, not an output field.
- CLI updates only thread.horizon. It rejects pending/running Runs in
  `compact_<target>`; active target Runs are allowed outside the covered range.
- Automatic `_toolang/compact` atomically updates thread.horizon and writes the
  calling Run's existing compact control: `{horizon: run_summary}`, with its
  tool Step as `triggered_by`.
- New Runs snapshot thread.horizon. Active Runs adopt only their own controls;
  adopting Steps record `preceded_by`. Replay uses these recorded facts.
- FORGET creates one model-free summary Run returning the forgetting marker,
  with the same coverage inputs and publication path.
- Discovery reads thread.horizon, never scans for the latest successful producer.
  Unpublished success does not change the horizon.

## Concurrency and ownership

Keep database-and-target-thread `flock` through admission, generation, and
publication. CLI rejects a busy lock without waiting, including the gap between
producer completion and publication. Auto may wait, recheck preflight, and reuse
an applicable published result. Both reject unfinished compact Runs left behind.

Use short SQL transactions. Recheck captured coverage and producer validity;
failed, empty, canceled, or stale results do not publish. Automatic publication
and control creation commit or roll back together.

Preflight owns admission and output/reasoning budgets; `execution/tools/compact.py`
owns the tool; CLI owns argument/file resolution; execution reconstructs results;
store/records own persistence. No leases or automatic recovery of stranded Runs.

## Acceptance

Touch base types, execution records/store/schemas, history/assembly, tool, CLI,
prompts, and related tests/docs. Verify DEFAULT/file/FORGET, incremental and
explicit ranges, active target Runs, busy compact rejection, cancellation,
atomic rollback, restart/replay, and unchanged issued model calls. Run default
checks and opt-in provider tests. Old persisted horizon formats are unsupported.
