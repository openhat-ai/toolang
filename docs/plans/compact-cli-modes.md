# Compact CLI execution modes

Status: approved; persistence and algorithm contract are defined in
[Text-only algorithms](compact-summary-algorithm.md).

## Interface

```sh
toolang AGENT compact --algorithm DEFAULT thread=THREAD [before=RUN]
toolang AGENT compact --algorithm ./compact.too thread=THREAD [before=RUN]
toolang AGENT compact --algorithm FORGET thread=THREAD before=RUN
```

DEFAULT is implicit. FILE loads a UTF-8 `.too` source once, validates its compact
signature, and uses the target agent's model settings and isolated history tools.
No adjacent configuration or additional tool permissions are loaded.
FORGET requires `before`, rejects `--model`, and needs no model configuration.
CLI inputs are only `thread` and `before`; model/limit flags retain their meaning
for DEFAULT and FILE.

## Behavior

`before` is exclusive. DEFAULT/FILE retain the latest terminal root by default,
reuse a valid previous summary ending before the boundary, and otherwise start
from the first root. FORGET replaces the whole prefix with
`Earlier history was intentionally forgotten.`, discarding previous summary text.
All modes preserve original records and retain at least one terminal root.

Freeze coverage and the published summary reference together, take a nonblocking
per-target flock, reject active compact Runs, and recheck the snapshot. Active
target Runs outside coverage are allowed. Each mode creates one summary Run and
returns `{run, horizon, output}`. Success updates thread.horizon only; existing
Run controls and issued model calls remain unchanged. Subsequent compaction
starts at the previous retained boundary.

## Scope and acceptance

CLI owns selection, file/input resolution, and orchestration. Shared execution
validates Text results; store publishes Run references. Verify all three modes,
invalid flags/signatures/results, model-free forgetting, busy rejection,
append/rewind, incremental reuse, restart, and replay. Run default verification.
External algorithms may produce poor summaries; validation checks structure and
coverage, not factual quality. No open questions.
