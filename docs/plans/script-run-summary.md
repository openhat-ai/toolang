# Script Run Summary

## Goal

Give every terminal script execution one consistent, actionable root summary.
The summary must identify the run and lifecycle outcome, describe the canonical
result without duplicating it, expose durable inspect and save actions, and
report trustworthy aggregate work metrics.

Implementation starts only after this definition is approved.


## Success Criteria

- Successful, failed, and canceled executions use one ordered summary model and
  the same status and metric vocabulary.
- A successful result is described by its durable shape and can be reopened or
  exported without rerunning the script.
- Failed and canceled executions show one selected diagnostic, never present
  partial work as a result, and remain inspectable.
- Duration, descendant-run, tool-call, model-call, token, and cost fields have
  exact counting, ordering, formatting, and availability rules.
- Missing accounting is never converted to zero, and partial accounting is
  visibly distinguished from an exact total.
- Script stdout remains a clean final-value channel; summary content and action
  hints remain on stderr.
- Canonical result and run paths follow #258, while progress layout and Chat TUI
  differences remain owned by #260 and #261 respectively.


## Current Behavior

`src/toolang/cli/common/script_progress/blocks.py` currently renders a root
frame from the terminal `RunEnd` event:

```text
--- run_abc123 succeeded ---
1 item returned
8.2s · 4.6k/63 tokens · 1 model call
----------------------------
```

The same frame is used for failure and cancellation, but its contents are not
yet a complete shared summary contract:

- durable `finished` is already displayed as `succeeded`;
- a successful shape is shown only when `RunEnd.output` names a `StepEnd` still
  available to the live tracer;
- a pass-through run result that references a run input has no result
  description;
- failure displays a selected error, while a cancellation with no stored error
  can have no lifecycle detail;
- the frame exposes no reusable inspect path or save command;
- `_emit_result()` can append separate `Run:` and `Log:` lines after a failed or
  canceled frame, so the terminal outcome is split across two structures.

The tracer aggregates metrics in memory across the root run tree. The root is
not counted in the displayed run count; each descendant run is counted once.
Model and tool steps are counted when their terminal events arrive. Known input
and output token counts are summed, but a missing count is currently treated as
zero. Cost is durably recorded in `StepEnd.noted["cost"]` for model calls with
complete usage and pricing, but the script summary does not aggregate or show
it. The current metric order is duration, descendant runs, tokens, model calls,
and tool calls.

`src/toolang/cli/toolang/commands/script.py` writes the successful canonical
result to stdout after execution. Text-only output is emitted as text; other
message parts are emitted as compact JSON. Failed and canceled runs emit no
stdout value. The result is also retained in `runs.db`, but the summary does not
tell the user how to recover it.

The implementation, rather than the broader presentation draft, is the source
of truth for current quiet behavior: `-q` suppresses the live tracer but does
not suppress a successful stdout result. Default non-interactive execution also
keeps stderr progress silent unless `-v` is supplied. Failed execution returns
1. A cancellation caused by the local interrupt path returns 130; a canceled
record received without that local interrupt currently returns 1.


## Scope

This feature defines and later implements:

- the terminal root-summary fields, wording, order, and lifecycle variants for
  script execution;
- result-state and result-description rules;
- canonical inspect and save actions for a durable result;
- aggregate duration, run, call, token, and cost semantics;
- exact, partial, unavailable, zero, and malformed-metric behavior;
- stderr visibility, stdout separation, and compatibility behavior;
- focused unit, integration, and presentation tests plus affected developer
  documentation.

This feature does not define or change:

- live progress, `let` statement presentation, error cause expansion, terminal
  width measurement, resizing, or wrapping; those belong to #260;
- Chat TUI bars, scrollback, or summary layout; #261 reuses the semantic fields
  defined here where the concepts are equivalent;
- the inspect grammar or inspect result envelopes; those belong to #258;
- execution events, durable step accounting fields, database schemas, pricing,
  provider usage collection, or runtime limit enforcement;
- the canonical stdout encoding of text and non-text script results;
- retry, rerun, steering, cancellation, or control semantics;
- a new script-output file flag, an automatic file write, or a new save command.


## Terminal Summary Model

One terminal summary describes one accepted root run after its terminal
`RunEnd`. It has these semantic fields in this order:

1. root run path and display status;
2. one lifecycle diagnostic when the outcome is failed or canceled;
3. one result-state description;
4. inspect, save, and log actions that are valid for that state;
5. one aggregate metrics line when at least one metric is available.

The existing unmarked root frame remains the script representation:

```text
--- RUN_PATH DISPLAY_STATUS ---
LIFECYCLE_DETAIL
RESULT_DESCRIPTION
INSPECT_ACTION
SAVE_ACTION
LOG_ACTION
METRICS
--------------------------------
```

Optional fields are omitted without changing the relative order of the fields
that remain. Blank placeholder rows are never printed. The opening frame keeps
the root run path before its status. The exact width, wrapping, truncation, and
narrow-terminal treatment of the frame and action commands are coordinated
with #260; that work must not reorder or rename the semantic fields defined
here.

The summary is emitted once. When it has been emitted, post-execution result
handling must not append a duplicate error, `Run:`, or `Log:` block. If no
terminal summary can be emitted, the existing command-level fallback remains
responsible for the diagnostic, run ID, and log path.


## Lifecycle Vocabulary And Fields

Durable and display statuses use the existing shared mapping:

| Durable status | Display status |
| --- | --- |
| `finished` | `succeeded` |
| `failed` | `failed` |
| `canceled` | `canceled` |

Toolang uses the single-l spelling `canceled` in all display text. A terminal
summary is not defined for `pending` or `running`.

### Success

A successful summary has no lifecycle diagnostic. It describes the result
using the result-state rules below. A recorded or empty durable result exposes
both inspect and save actions. An unavailable durable reference exposes its
output inspect action but not a save action. A run with no output reference
exposes the run inspect action and no save action.

```text
--- run_abc123 succeeded ---
Result: 8-item list
Inspect: toolang ./review.too inspect run_abc123/output
Save: toolang ./review.too inspect run_abc123/output --json > run_abc123-output.json
19.0s · 7 runs · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k · $0.50
----------------------------
```

### Failure

A failed summary shows `Error: MESSAGE` exactly once. `MESSAGE` is the same
selected actionable root cause already used by script progress: prefer the root
run error, otherwise use the owning failed step error, otherwise use
`Run failed.`. Nested-cause layout is owned by #260, but it must not introduce a
second root diagnostic.

A failed run always says `Result: not produced`. Completed child values,
streamed model deltas, and an output from a step that preceded the failure are
execution history, not the failed root result. The inspect action targets the
run path. A log action is shown when the run log exists. There is no save action.

```text
--- run_abc123 failed ---
Error: provider returned status 429
Result: not produced
Inspect: toolang ./review.too inspect run_abc123
Log: .toolang/agents/review/.runtime/logs/review/run_abc123.log
2.1s · 1 tool call · 2 model calls · ↑ ≥1.2k ↓ ≥86 · cost unavailable
-------------------------
```

### Cancellation

A canceled summary uses `Canceled: REASON`. A local script interrupt is
normalized to `Canceled: interrupted by user`. A nonempty, meaningful stored
reason is used for other cancellation sources. If no meaningful reason exists,
the fallback is `Canceled: run canceled`.

Cancellation always says `Result: not produced`. Transient streamed content
and completed child results are not promoted to a root result. The inspect
action targets the run path, the log action appears when available, and no save
action is shown.

```text
--- run_abc123 canceled ---
Canceled: interrupted by user
Result: not produced
Inspect: toolang ./review.too inspect run_abc123
Log: .toolang/agents/review/.runtime/logs/review/run_abc123.log
4.1s · 2 runs · 1 model call · tokens unavailable · cost unavailable
---------------------------
```

Generic variants such as `canceled`, `cancelled`, `run canceled`,
`run cancelled`, `operation canceled`, and `operation cancelled` are not
repeated as a reason. They select the `run canceled` fallback.


## Result States And Descriptions

The summary uses the result states defined for the canonical output view in
#258. The human summary does not add a second result-state vocabulary.

| State | Meaning | Summary wording |
| --- | --- | --- |
| `recorded` | an output reference resolves to one or more message parts | the best known shape, otherwise `Result: available` |
| `empty` | an output reference resolves to zero message parts | `Result: empty` |
| `not_recorded` | the run has no output reference | `Result: no value returned` on success |
| `unavailable` | an output reference exists but cannot be resolved | `Result: unavailable` |

The shape description reuses the existing execution-presentation vocabulary:

- a known item is `Result: 1 item`;
- a list with a known count is `Result: N-item list`, including
  `Result: 0-item list` and `Result: 1-item list`;
- a list without a reliable count is `Result: list`;
- a nonempty value without reliable shape facts is `Result: available`.

`1 item` and `1-item list` remain distinct. The summary never prints the
result value or a bounded content preview. The canonical value is already sent
to stdout, and its typed durable representation is available through inspect.

Only a terminal root output is a result. Partial model deltas, individual
message parts observed before cancellation, child-run outputs, and earlier
successful steps are not labeled as partial results. When the terminal output
reference itself cannot be fully resolved, the state is `unavailable` rather
than `partial`.

The event callback must not query SQLite. If accurate result-state resolution
requires durable data after completion, the script command resolves it through
`RunStore` after the handle returns and passes concrete state to summary
finalization. This preserves the boundary between ordered event rendering and
file-backed result resolution.


## Inspect And Save Actions

Actions depend on #258's canonical paths:

```text
RUN_PATH             run_abc123
RUN_OUTPUT_PATH      run_abc123/output
```

This feature does not accept, emit, or reinterpret legacy colon-and-dot step
paths. It waits for #258's canonical output target rather than adding a
script-specific result path.

The inspect action is a copyable command:

```text
Inspect: toolang SCRIPT inspect PATH
```

`SCRIPT` is the source argument usable by the current invocation, rendered
with safe POSIX shell quoting when needed. `PATH` is `RUN_OUTPUT_PATH` for a
successful run with an output reference and `RUN_PATH` otherwise. The run path
therefore remains useful for failure, cancellation, and no-value success. An
unavailable output reference still uses `RUN_OUTPUT_PATH`, allowing #258's
focused output view to expose its unavailable state and unresolved source.

The save action is a copyable, nonautomatic durable-history export:

```text
Save: toolang SCRIPT inspect RUN_OUTPUT_PATH --json > RUN_ID-output.json
```

It is shown only for `recorded` and `empty` successful results. The file
contains #258's lossless JSON output-inspection envelope, including its owner,
state, source, and typed `value`; it is not promised to be byte-for-byte equal
to the immediate stdout representation. The run-ID-derived filename is stable
and avoids collisions between ordinary runs. Toolang does not execute the
command, create the file, or introduce hidden filesystem writes.

Users who want only the immediate stdout representation can continue to use
ordinary shell redirection on the original script invocation. Keeping the
summary on stderr ensures that redirection is not contaminated by frame,
metric, or action text.

The summary implementation must not ship an inspect or save action before the
corresponding #258 path and JSON behavior are available. If the features land
separately, #258 is the implementation prerequisite for these action rows.


## Metric Contract

The metrics line uses this fixed field order:

```text
DURATION · RUNS · TOOL CALLS · MODEL CALLS · TOKENS · COST
```

Each field is defined over the effective root run tree represented by the
terminal script execution. An effective record is one that contributes to the
current execution projection; ejected retry history is not counted. A nested
record contributes to exactly one root total regardless of its depth.

| Field | Meaning |
| --- | --- |
| duration | nonnegative wall time from root `RunBegin.started_at` to root `RunEnd.finished_at` |
| runs | effective descendant runs that began, excluding the already identified root run |
| tool calls | effective tool invocation steps that began, regardless of terminal status |
| model calls | effective model invocation steps that began, regardless of terminal status |
| input tokens | sum of known `noted.tokens.input` values for counted model calls |
| output tokens | sum of known `noted.tokens.output` values for counted model calls |
| cost | sum of known `noted.cost` Decimal USD values for counted model calls |

Failed and canceled descendants and calls remain work that was attempted, so
they contribute to run and call counts. A model or tool step that never began
does not contribute. Flow, run, agent, human, loop, parallel, and system steps
do not contribute to either call count.

The root run is excluded from `runs` because its identity and outcome already
head the frame. This preserves the existing script meaning: `7 runs` means
seven descendant runnable invocations, not the root plus six descendants.

Zero run, tool-call, and model-call fields are omitted. Exact zero usage or cost
reported for one or more model calls is not omitted. When there are no model
calls, token and cost fields are omitted because there was no model accounting
to report.


## Metric Formatting

### Duration

Duration keeps the existing compact units but normalizes a rounded value into
the next unit instead of printing `1000ms`, `60.0s`, or `Xm 60s`. Each range
rounds half-up at its displayed precision:

| Raw range | Rounding and format | Examples |
| --- | --- | --- |
| less than 1 second | nearest integer millisecond | `820ms`; `999.5ms` becomes `1.0s` |
| 1 second to less than 60 seconds | nearest tenth of a second | `19.0s`; `59.95s` becomes `1m 00s` |
| 60 seconds or more | nearest whole second, then `divmod` by 60 | `1m 08s`; `119.5s` becomes `2m 00s` |

Rollover is based on the rounded display value, so the threshold examples are
canonical even when the raw duration belonged to the lower range. Minute
formatting remains unbounded rather than adding an hours unit. A negative clock
delta is clamped to zero and displays as `0ms`. Missing, invalid, or non-finite
timestamps omit duration instead of failing the summary or printing a guessed
value.

### Counts

Run and call counts use exact base-10 integers and normal singular/plural
wording:

```text
1 run · 2 runs · 1 tool call · 6 tool calls · 1 model call · 11 model calls
```

They are not abbreviated because exact operational counts are more useful than
saved width.

### Tokens

Tokens use directional arrows and no trailing `tokens` noun:

```text
↑ 10.3k ↓ 2.8k
```

Each value uses the existing base-1000 compact formatter: values below 1,000
are exact; thousands and millions use `k` and `m`, at most one decimal place,
and no trailing `.0`. A provider-reported zero is `0`.

Availability is evaluated independently for input and output:

- if every counted model call records the component, show the exact sum;
- if some but not all counted calls record it, show the known lower bound with
  `≥`, such as `↑ ≥10.3k`;
- if no counted call records one direction but the other direction is known,
  show `↑ unavailable` or `↓ unavailable` in that direction;
- if neither direction is recorded by any counted call, collapse the field to
  `tokens unavailable`.

Missing usage is never converted to zero. A lower bound is mathematically
valid because token counts are nonnegative; it is not an estimate of the
missing calls.

### Cost

Cost uses exact `Decimal` addition and a leading USD dollar sign. It never
passes through binary floating point.

- amounts of at least one cent round half-up to two fractional digits;
- positive sub-cent amounts retain four significant digits so they cannot
  render as zero;
- exact zero renders as `$0.00`;
- trailing fractional zeroes beyond the required two are removed.

Examples are `$12.35`, `$0.50`, `$0.003456`, and `$0.00`.

If every counted model call records cost, the total is exact. If only some do,
the known total is a lower bound and receives `≥`, for example `≥$0.50`. If no
counted model call records cost, the field is `cost unavailable`. Missing price
data makes cost unavailable even when token usage is complete.

Invalid durable cost text is treated as missing accounting and must not crash
summary rendering. Negative cost is invalid provider data and is likewise
treated as unavailable rather than subtracted from the total.


## Partial And Unavailable Data

Summary completeness is field-specific. One missing metric does not suppress
the result, actions, or the metrics that remain trustworthy.

| Condition | Behavior |
| --- | --- |
| duration timestamps missing or invalid | omit duration |
| some token components missing | show known per-direction lower bounds |
| all token components missing | `tokens unavailable` |
| some model costs missing | show known cost lower bound |
| all model costs missing | `cost unavailable` |
| result reference resolves to no parts | `Result: empty`; inspect and save remain available |
| successful run has no result reference | `Result: no value returned`; inspect the run only |
| result reference cannot resolve | `Result: unavailable`; inspect output, omit save |
| failed or canceled after partial work | `Result: not produced`; inspect the run only |
| malformed metric payload | treat that component as missing and continue |

A partial accounting marker applies only to the affected component. For
example, complete input tokens and partial output tokens render as
`↑ 10.3k ↓ ≥2.8k`. Cost availability does not change token availability, and
token availability does not imply cost availability.


## Visibility, Streams, And Exit Compatibility

The summary follows the current script progress visibility gate:

| Invocation context | stderr summary | stdout result |
| --- | --- | --- |
| stderr is a TTY, default verbosity | shown | unchanged |
| stderr is not a TTY, default verbosity | hidden | unchanged |
| `-v` or `-vv` | shown, including non-TTY | unchanged |
| `-q` | hidden | unchanged |

Failure and cancellation still emit command-level fallback diagnostics when no
summary is visible. Quiet mode suppresses routine progress and the terminal
summary, not the successful stdout value or a necessary nonzero-exit
diagnostic.

Summary content, inspect commands, save commands, and log paths are stderr-only.
The successful canonical result remains stdout-only. Failed and canceled runs
continue to produce no stdout value.

This presentation feature does not change process exit semantics:

- success returns 0;
- failure returns 1;
- a local interrupt that reaches the current interruption path returns 130;
- a canceled terminal record received without that local interrupt continues
  to return 1.

Setup, parsing, preparation, and validation failures that occur before an
accepted root `RunBegin` do not fabricate a run summary. They retain the
existing command-level error handling. A forced second interrupt that prevents
terminal cleanup may likewise fall back to interruption output rather than
claiming a complete summary.


## Cross-Feature Coordination

### Inspect Navigation (#258)

#258 owns `RUN_ID`, `RUN_ID/output`, output-state resolution, `--json`, and the
copyability guarantee. This feature consumes those decisions exactly. It does
not introduce an alias, output subpath, or second JSON envelope.

### Script Progress (#260)

This feature owns terminal root-summary semantics, field labels, field order,
and metric formatting. #260 owns terminal width, wrapping, truncation, resize
behavior, live progress, `let` statement layout, and nested error layout. If an
action wraps in a narrow terminal, its logical command must remain recoverable
without changing its path or field order.

### Chat TUI Summary (#261)

#261 should share `succeeded`/`failed`/`canceled`, result states, descendant-run
meaning, metric order, token arrows, cost formatting, and partial/unavailable
rules wherever Chat has the same durable facts. Chat may omit the script frame,
full shell commands, stdout language, and file-export hint because its response
and navigation surfaces differ. Those differences must be documented in #261
rather than redefining the shared metric semantics.


## Compatibility Constraints

- Preserve script command routing, runnable selection, `-q`, `-v`, `-vv`, and
  all current public run options.
- Preserve the stdout value bytes and JSON shape for existing successful
  outputs; summary work must not insert text into stdout.
- Preserve the absence of stdout for failed and canceled runs.
- Preserve default non-TTY silence and allow `-v` to request stable stderr
  presentation without ANSI control sequences.
- Preserve run logging and command-level fallback output when no terminal
  summary is available.
- Keep durable `finished` in records and JSON; `succeeded` is display-only.
- Do not add database migrations, execution-event fields, API response fields,
  pricing requirements, or provider calls for presentation.
- Do not query the execution store from the ordered event callback.
- Do not print secrets, raw model requests, full result values, or encoded media
  in the summary.
- Human stderr output is intentionally allowed to gain result, action, and cost
  lines. It has no line-for-line compatibility guarantee beyond the stream,
  visibility, status, and metric semantics defined here.


## Design Touchpoints And Likely Files

Implementation should keep aggregation terminal-independent and keep rendering
and shell-command presentation in the CLI.

- `src/toolang/cli/common/execution_progress/state.py`
  - replace zero-default token accounting with per-component known/missing
    counts;
  - add Decimal cost aggregation and exact/partial availability;
  - keep descendant metrics composable without double-counting.
- `src/toolang/cli/common/execution_progress/formatting.py`
  - format the fixed metric order, token arrows and lower bounds, Decimal cost,
    result states, and lifecycle wording;
  - keep pure helpers shared with Chat presentation where appropriate.
- `src/toolang/cli/common/script_progress/tracer.py`
  - retain the terminal root event and aggregate all effective descendant work;
  - expose enough completion state for one later summary finalization;
  - prevent duplicate terminal diagnostics.
- `src/toolang/cli/common/script_progress/blocks.py`
  - render the ordered root-summary fields and lifecycle variants;
  - leave width and wrapping mechanics compatible with #260.
- `src/toolang/cli/toolang/commands/script.py`
  - pass the reusable script target, run log, and resolved result state into
    summary finalization;
  - construct safely quoted inspect and save commands;
  - preserve stdout encoding, visibility gates, fallbacks, and exit codes.
- `tests/unit/cli/test_console_run_tracer.py`
  - cover lifecycle frames, nested aggregation, exact and partial metrics, and
    field ordering.
- `tests/unit/cli/test_script_command.py`
  - cover result-state decisions, action construction, quiet behavior, and
    duplicate-fallback suppression.
- `tests/integration/cli/test_script_local.py`
  - cover persisted result actions and stdout/stderr separation through a real
    local script run.
- `docs/api.md`, `docs/execution-presentation.md`, and
  `docs/execution-transcript.md`
  - replace stale root-summary examples and document actual quiet, action,
    metric, and availability behavior.

No change to `toolang.execution` should be necessary. If implementation reveals
that a required fact is absent, the feature must first prefer post-run reads of
existing records. Expanding durable schemas or events requires separate scope
approval.


## Acceptance Tests

1. **Lifecycle structure**
   - Snapshot successful, failed, and canceled summaries at one stable width.
   - Assert run path and status, diagnostic, result, actions, and metrics retain
     the defined order.
   - Assert each terminal run emits one frame and one selected diagnostic.

2. **Result descriptions**
   - Cover one item, zero-item list, one-item list, counted list, uncounted list,
     unknown nonempty value, recorded empty output, no output reference, and an
     unresolved output reference.
   - Assert result values and partial deltas are never copied into the summary.

3. **Lifecycle result behavior**
   - Assert success uses the resolved result state.
   - Assert failure and cancellation always use `Result: not produced` even
     after completed child work or streamed deltas.
   - Assert cancellation normalizes a local interrupt, preserves a meaningful
     external reason, and suppresses generic cancellation payloads.

4. **Inspect and save actions**
   - Assert output-bearing success uses `RUN_ID/output` and other outcomes use
     `RUN_ID`.
   - Assert an unavailable successful output reference still uses
     `RUN_ID/output`, exposes the unavailable state and source through inspect,
     and omits save.
   - Assert source arguments needing spaces or shell metacharacters are quoted
     safely.
   - Assert save appears only for recorded and empty successful results, uses
     `--json`, and selects a run-ID-derived filename.
   - Execute the equivalent inspect command against the persisted script store
     and assert the selected owner, state, source, and typed value match the run.

5. **Exact metrics**
   - Aggregate a root with nested successful, failed, and canceled descendants
     and assert every descendant run and model/tool step is counted once.
   - Assert the root run is excluded, zero count fields are omitted, singular
     and plural labels are correct, and field order is fixed.
   - Cover duration boundaries below one second, below one minute, and at or
     above one minute, including the canonical half-up rollovers from
     `999.5ms` to `1.0s`, `59.95s` to `1m 00s`, and `119.5s` to `2m 00s`.

6. **Token availability**
   - Cover exact nonzero usage, exact zero usage, one partially known direction,
     partial usage across calls, all usage unavailable, and no model calls.
   - Assert compact values and independent `≥` markers without converting
     missing data to zero.

7. **Cost availability**
   - Cover exact cents, rounding, positive sub-cent cost, exact zero, partial
     cost, entirely unavailable cost, missing price with known tokens, invalid
     Decimal text, and negative cost.
   - Assert Decimal addition is exact and partial totals use `≥`.

8. **Streams and visibility**
   - Assert a default TTY shows the summary on stderr and the canonical result
     on stdout.
   - Assert default non-TTY execution keeps the summary silent, `-v` restores
     stable stderr output, and `-q` suppresses summary/progress without
     suppressing successful stdout.
   - Assert failed and canceled executions never emit a stdout result.

9. **Fallbacks and exit behavior**
   - Assert a visible terminal summary suppresses duplicate command-level
     error, run, and log rows.
   - Assert pre-run failure and forced interruption retain actionable fallback
     output.
   - Retain exit assertions for success, failure, local interruption, and a
     canceled record without a local interrupt.

10. **Cross-feature compatibility**
    - Assert every printed inspect path is accepted unchanged by #258.
    - Reuse metric formatter tests in Chat where #261 consumes the same helper.
    - Keep #260 presentation snapshots responsible for narrow widths and
      wrapping without changing this feature's semantic order.

11. **Repository verification**
    - `uv run ruff check .`
    - `uv run ruff format --check .`
    - `uv run ty check`
    - `uv run pytest`


## Risks And Mitigations

- **Missing usage can look like zero.** Track contribution counts separately
  from numeric sums and test exact zero independently from unavailable data.
- **Nested aggregation can double-count work.** Give each run a composable
  subtree total and merge a child exactly once at its terminal boundary.
- **Failed calls can hide billing uncertainty.** Count attempted call steps and
  mark tokens or cost partial whenever their accounting is absent.
- **Decimal formatting can lose small real costs.** Sum as `Decimal`, retain
  significant sub-cent digits, and test rounding boundaries without floats.
- **Result actions can drift from inspect syntax.** Depend directly on #258's
  canonical run/output paths and do not maintain a script-specific parser.
- **A save hint can imply an automatic write.** Label it as a copyable command,
  use a run-specific filename, and never execute it from summary rendering.
- **Action commands can dominate narrow output.** Let #260 own wrapping while
  preserving the complete logical command and path.
- **Summary work can contaminate pipeline output.** Keep every summary field on
  stderr and retain integration coverage for redirected stdout.
- **Terminal cleanup can prevent finalization.** Emit a summary only after a
  terminal root event; otherwise retain the existing interruption and error
  fallback rather than claiming complete accounting.
- **Cross-feature definitions can diverge.** Treat #259 as the semantic owner,
  #258 as the path owner, #260 as the script-layout owner, and #261 as a
  consuming surface with documented exceptions.


## Open Questions

None. This definition selects the lifecycle vocabulary, ordered summary fields,
result states, actions, metric meanings and formats, availability rules,
compatibility behavior, implementation boundaries, and acceptance coverage
required for implementation.
