# Human-triggered compaction

Status: approved on 2026-09-08; PR1 implemented. PR2 remains pending.

## Goal and scope

Add a human-only CLI entry point, fix the failures found in live compaction,
then verify manual execution before automatic model-call preflight. Keep
`_toolang/compact` unavailable to models. No chat slash command, record schema
change, Step-level boundary, arbitrary interval summary, or memory plugin work.

Baseline: `9e58043c`. The live experiment on `f4c87621` produced `begin: ""`
from a null input and lost earlier facts during rolling summarization. Its next
Run retained all original history because the compact output was rejected.
The subsequent input-record refactor changes test harness APIs, not these
compact prompts; use fresh test data rather than migrate the old experiment.

## CLI

```sh
too <agent> compact THREAD [--end RUN] [--model MODEL_SPEC] [--limit LIMIT=VALUE]...

# Ten historical Runs: summarize 01–08 and retain 09–10.
too a compact term_qfejsswt --end run_zw54zcf7 \
  --model 'deepseek/deepseek-v4-flash effort=low' --limit time=600
```

- Register beside fork/rewind/retry in Control commands, not a new group.
- `THREAD` is required. `--end` is an exclusive root Run reference in its
  logical history. Omission selects the latest terminal root, retaining it.
- Always summarize a full prefix: `begin=null`. Do not expose redundant
  `--begin` or allow a partial summary to masquerade as far.
- Retain at least one terminal historical root; never summarize an active
  root. Reject an empty prefix, unknown/nonmember/child boundary, or compact
  Thread before starting provider work.
- A CLI invocation explicitly requests fresh work, even below the input budget
  or when an older result exists. No `--force` flag is needed. Automatic
  preflight retains its budget check and applicable-result reuse.
- `--model` applies only to this compact request, using existing model-body
  parsing, including effort. Otherwise use configured compact-model selection.
  Apply effective model authorization and require tool calls plus structured
  output; never fall back to the normal Run's model or change agent config.
- Reuse existing run-limit parsing. Show normal progress on stderr and emit
  JSON `{run, horizon, output: {thread, begin, end, summary}}` on successful
  stdout. Failure/cancellation returns nonzero, with the Run ID when available.

## Execution and adoption

Factor independent compact execution out of the budget-dependent preflight
entry point. CLI and preflight share model selection, the existing per-Thread
permit, isolated `compact_<thread>` Run, read-only history tools, output
validation, and cancellation handling. Keep CLI parsing outside execution.
Use the existing agent-server/client acquisition path; do not run a separate
host executor against a server's store as a shortcut.

Resolve and freeze the requested range, revalidate after admission and before
accepting the output, and fail if rewind invalidated it. Ordinary appends do
not change the frozen end. Wait without holding a Store transaction. Explicit
CLI requests serialize; automatic waiters may reuse the newly available result.

- **CLI:** persist the independent compact Run. Future root Runs capture the
  applicable output reference through normal horizon initialization. Do not
  inject a message or silently change an already-running Run's binding.
- **Preflight:** additionally create the calling Run's existing compact control,
  then reprepare the ModelCall. Keep its runtime Tool Step and progress behavior.
- Preserve earlier calls and records. Rejected/canceled work must not be
  adopted or make an earlier applicable summary unavailable.

## Fixes and acceptance, in order

1. **CLI entry point.** Add offline CLI/client/executor tests for explicit and
   default boundaries, model parameters, limits, isolation, mutual exclusion,
   cancellation, and invalid output. Demonstrate the current live failure
   through the command, without treating a successful flow status as success.
2. **Faithful compact output.** Preserve null when rendering the range and
   validate the exact supplied range. Make each rolling summary self-contained:
   retain prior facts and later corrections, rather than substituting traversal
   notes such as “already covered.” Keep cursor/progress bookkeeping separate
   from remembered facts. Cover multiple pages, previous-summary input, latest
   corrections, incomplete traversal, and invalid bounds with regressions.
3. **Live CLI acceptance.** Recreate the ten input groups in a fresh isolated
   agent/thread. Compact with `--end` at Run 09, then issue a new factual probe.
   Require an adopted horizon, far present, Runs 01–08 raw inputs absent, Runs
   09–10 exact inputs present, and correct old facts, overrides, and prohibitions.
   Reconstruct the persisted ModelCall and compare it with the dispatched call;
   inspect it again after restart. Retain inputs, terminal output, and records.
4. **Live preflight acceptance.** Only after CLI acceptance, use another fresh
   fixture and a controlled input budget to trigger the unmodified preflight
   path. Verify compact Tool Step → applied compact control → Model Step;
   no discarded candidate Model Step, no recursive compact, no compact tool
   result in model conversation, and the same far/near/fact checks. Include
   compact failure/cancel and irreducible-input offline cases.

## Delivery and touchpoints

- **PR1:** human CLI and shared compact execution. Likely files: CLI
  `main.py`/`commands/thread.py`, execution client/request transport and executor
  entry points, `executor/compact.py`, focused CLI/integration tests.
- **PR2:** range rendering and cumulative-summary fixes, their regressions,
  then CLI and preflight live verification. Main touchpoints:
  `executor/prompts/compact.too`, compact output validation, and compact tests.

Default tests remain offline and deterministic; live-provider checks are
explicitly opt-in. Run Ruff check/format, ty, and the full default pytest suite
before commits. Model summarization is probabilistic: a green execution status
is insufficient evidence, and live factual checks complement mocked tests.

## PR1 verification

The CLI was exercised with ten fresh DeepSeek Runs, selecting Run 09 as the
exclusive boundary. Compact Run `run_tc2fgb31` returned `begin: ""` instead of
null after reading the first eight Runs. The executor recorded failure and the
CLI exited with status 1 rather than publishing an invalid horizon. This
reproduces the range-rendering issue for PR2; it is not successful live
compaction acceptance. Offline checks cover valid output and next-Run adoption,
active-Run isolation, frozen boundaries, cancellation, and rejected output.

## Open questions

None. The CLI signature/defaults and future-root-only effect were approved.
