# Script Run Summary

## Goal

Give every terminal script run one compact summary that identifies the run,
describes a successful result, explains failure or cancellation, and reports
trustworthy aggregate work.

Implementation starts only after this definition is approved.


## Success Criteria

- Success, failure, and cancellation share one summary structure and vocabulary.
- The frame exposes the exact run ID needed for later inspection.
- Successful stdout remains directly saveable without summary text.
- Duration, run, call, token, and cost metrics have precise meanings and order.
- Partial accounting is marked; unavailable accounting is omitted, not shown as
  zero.
- The definition reuses #258 for inspect syntax and #260 for terminal layout.


## Current Behavior

ConsoleRunTracer writes a framed root summary to stderr:

~~~text
--- run_abc123 succeeded ---
1 item returned
8.2s · 4.6k/63 tokens · 1 model call
----------------------------
~~~

It already maps durable finished to succeeded, excludes the root from the run
count, and aggregates descendant model calls, tool calls, and tokens. It does
not show cost, treats missing usage as zero, and orders tokens before call
counts. Pass-through output can lack a result description.

The command writes a successful result to stdout as text or compact JSON.
Failed and canceled runs write no result. Failure handling can append separate
Run and Log rows after the frame, splitting one outcome across structures.

Default progress and the summary appear only when stderr is a TTY; -v also
enables them for non-TTY stderr. -q suppresses progress and the summary but
does not suppress successful stdout.


## Scope

This feature defines:

- root-summary fields, order, wording, and lifecycle variants;
- successful result descriptions;
- inspect and save affordances without instruction rows;
- duration, descendant-run, call, token, and cost metrics;
- exact, partial, unavailable, zero, and malformed accounting behavior;
- stdout/stderr, quiet, fallback, and exit compatibility;
- likely implementation files and acceptance tests.

It does not change:

- inspect syntax or presentation, which #258 owns;
- width, wrapping, resizing, live progress, let layout, or nested errors, which
  #260 owns;
- Chat TUI layout, which #261 owns;
- execution, controls, records, events, provider accounting, or run limits;
- the stdout encoding of existing successful results;
- public script flags or automatic file writes.


## Summary Structure

One accepted root run emits at most one terminal frame:

~~~text
--- RUN_ID STATUS ---
DETAIL
METRICS
---------------------
~~~

DETAIL and METRICS are optional; absent fields do not leave blank placeholders.
The title always contains the exact root run ID followed by one display status:

| Durable status | Display status |
| --- | --- |
| finished | succeeded |
| failed | failed |
| canceled | canceled |

Toolang uses the single-l spelling canceled. Pending and running states do not
produce terminal summaries.


## Lifecycle Wording

### Success

Success uses the result description as DETAIL:

~~~text
--- run_abc123 succeeded ---
8-item list returned
19.0s · 7 runs · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k $0.50
----------------------------
~~~

### Failure

Failure keeps one selected actionable error as an unlabeled detail and has no
result, inspect, save, or log row:

~~~text
--- run_abc123 failed ---
provider returned status 429
2.1s · 1 tool call · 2 model calls · ↑ 1.2k+ ↓ 86+
-------------------------
~~~

Prefer the root error, otherwise the owning failed-step error, otherwise
Run failed. The frame title supplies the failed status, so DETAIL has no Error:
prefix. A visible frame suppresses duplicate command-level error, Run, and Log
rows.

### Cancellation

Cancellation shows one meaningful reason as an unlabeled detail and has no
result, inspect, save, or log row:

~~~text
--- run_abc123 canceled ---
interrupted by user
4.1s · 2 runs · 1 model call
---------------------------
~~~

Normalize the local interrupt reason to interrupted by user. Preserve another
meaningful reason. The frame title supplies the canceled status, so DETAIL has
no Canceled: prefix. Omit DETAIL when the only payload is a generic variant of
canceled, cancelled, run canceled, or operation canceled.


## Result Description

Only success describes a result:

| Durable result state | Summary wording |
| --- | --- |
| one item | 1 item returned |
| counted list | N-item list returned |
| list with unknown count | list returned |
| nonempty value with unknown shape | result returned |
| output reference resolves to no parts | empty result returned |
| no output reference | no result returned |
| output reference cannot resolve | result unavailable |

1 item and 1-item list remain distinct. The summary never previews the value.
Partial deltas, child results, and earlier successful steps are history, not a
failed or canceled root result.

If durable resolution is needed after RunEnd, the command may read existing
records after the handle returns. The event callback must not query SQLite.


## Inspect And Save Affordances

The summary prints no inspect or save instruction rows.

The exact RUN_ID in every frame is the inspection handle. The approved
[inspect navigation definition](./inspect-navigation-and-presentation.md)
exclusively owns command syntax, output navigation, missing-value states, and
JSON export. This plan does not repeat its paths, so the definitions cannot
drift.

The successful value remains stdout-only and is immediately saveable through
ordinary shell redirection. Later export uses the run ID and #258's inspect
surface. Toolang does not automatically write a result file. Failure and
cancellation have no root value to save.


## Metrics

Fields appear in this order:

~~~text
DURATION · RUNS · TOOL CALLS · MODEL CALLS · USAGE
~~~

USAGE is one group with spaces, not centered dots:

~~~text
↑ INPUT ↓ OUTPUT COST
~~~

For example:

~~~text
19.0s · 7 runs · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k $0.50
~~~

Metrics cover the effective root run tree. Ejected retry history is excluded,
and each descendant contributes once.

| Field | Meaning |
| --- | --- |
| duration | root RunBegin.started_at through root RunEnd.finished_at |
| runs | descendant runs that began, excluding the root |
| tool calls | tool steps that began, regardless of terminal status |
| model calls | model steps that began, regardless of terminal status |
| input/output | sums of known model token components |
| cost | sum of known model-call USD costs |

Failed and canceled descendants and calls still count as attempted work.
Non-model/tool steps do not affect call counts. Zero run and call counts are
omitted. Known zero tokens or cost are displayed; unavailable values are not.


## Formatting

### Duration

Duration uses compact units and half-up rounding:

| Raw range | Format |
| --- | --- |
| less than 1 second | nearest integer millisecond |
| 1 second to less than 60 seconds | nearest tenth of a second |
| 60 seconds or more | nearest whole second, then divmod by 60 |

Rounded values carry into the next unit: 999.5ms becomes 1.0s, 59.95s becomes
1m 00s, and 119.5s becomes 2m 00s. Minutes remain unbounded. Negative deltas
clamp to 0ms; missing or invalid timestamps omit duration.

### Counts

Run and call counts use exact integers and normal singular/plural wording:
1 run, 2 runs, 1 tool call, and 2 model calls.

### Tokens

Token counts use base-1000 k and m, at most one decimal place, and no trailing
.0. Values below 1,000 are exact.

Each direction is independent:

- complete data shows the exact sum, such as ↑ 10.3k;
- some known and some missing data shows the known lower bound with a + suffix,
  such as ↑ 10.3k+;
- no known data omits that direction.

Known zero is 0; partial known zero is 0+.

### Cost

Cost uses exact Decimal addition and a $ prefix:

- at least one cent rounds half-up to two fractional digits;
- positive sub-cent values retain four significant digits;
- exact zero is $0.00;
- partial known cost adds +, for example $0.50+;
- no known cost omits cost.

Invalid or negative cost is treated as missing. Missing cost does not suppress
known token directions, and missing tokens do not suppress known cost.


## Partial And Unavailable Data

| Condition | Summary behavior |
| --- | --- |
| invalid duration | omit duration |
| some token values missing | show known total with + |
| all values for one token direction missing | omit that direction |
| some costs missing | show known cost with + |
| all costs missing | omit cost |
| malformed metric value | treat that value as missing |
| successful result empty | empty result returned |
| successful result absent | no result returned |
| successful result unresolved | result unavailable |
| failed or canceled after partial work | no result line |

An unavailable component is never printed as zero or as an unavailable metric.
The usage group disappears when input, output, and cost are all absent.


## Streams And Compatibility

| Context | stderr summary | stdout result |
| --- | --- | --- |
| stderr TTY, default | shown | unchanged |
| non-TTY stderr, default | hidden | unchanged |
| -v or -vv | shown | unchanged |
| -q | hidden | unchanged |

When the frame is hidden, existing command-level failure, run ID, and log
fallbacks remain actionable. Summary text never enters stdout. Failure and
cancellation emit no stdout result.

Exit behavior remains unchanged: success is 0, failure is 1, local interruption
is 130, and a canceled record without that local interrupt is 1. Pre-run errors
and forced termination do not fabricate a terminal summary.

Durable JSON keeps finished; succeeded is display-only. The feature adds no
database migration, event field, API field, provider call, or pricing
requirement, and it does not expose secrets or raw result data.


## Design Touchpoints

- src/toolang/cli/common/execution_progress/state.py: track known and missing
  token/cost contributions and merge descendant metrics once.
- src/toolang/cli/common/execution_progress/formatting.py: format duration,
  grouped usage, partial +, cost, and result descriptions.
- src/toolang/cli/common/script_progress/tracer.py: aggregate the effective tree
  and retain one terminal root outcome.
- src/toolang/cli/common/script_progress/blocks.py: render the compact lifecycle
  frame without action or log rows.
- src/toolang/cli/toolang/commands/script.py: resolve post-run result state,
  preserve stdout, and suppress duplicate visible-frame fallbacks.
- tests/unit/cli/test_console_run_tracer.py,
  tests/unit/cli/test_script_command.py, and
  tests/integration/cli/test_script_local.py: cover the contract.
- docs/api.md, docs/execution-presentation.md, and
  docs/execution-transcript.md: update public examples and stream behavior.

No toolang.execution change is expected. A missing durable fact requires
separate scope approval rather than an execution-contract change here.


## Acceptance Tests

1. Snapshot concise success, failure, and cancellation frames; assert one frame,
   one optional unlabeled detail, one metrics line, no Error: or Canceled:
   prefix, and no inspect/save/log rows.
2. Cover every successful result wording, including empty, absent, and
   unresolved output; assert failed/canceled partial work has no result line.
3. Assert every frame's run ID opens the intended run through #258 and stdout
   redirection saves only the canonical successful value.
4. Aggregate nested successful, failed, and canceled work; assert descendant
   runs and model/tool calls count once, exclude the root, and retain field
   order and pluralization.
5. Cover duration unit boundaries and the 999.5ms, 59.95s, and 119.5s
   normalized rollovers.
6. Cover exact, zero, partial, absent, malformed, and negative usage/cost;
   assert + suffixes, omission of unavailable components, and no centered dot
   between tokens and cost.
7. Verify TTY, non-TTY, -v, -vv, and -q stream behavior, duplicate fallback
   suppression, unchanged stdout encoding, and existing exit codes.
8. Verify #260 may change wrapping without changing this field order, and #261
   shares equivalent status and metric semantics.
9. Run uv run ruff check ., uv run ruff format --check ., uv run ty check, and
   uv run pytest.


## Risks

- Missing accounting may look exact: track contributor counts separately from
  sums and test known zero independently.
- Nested aggregation may double-count: merge each child subtree once.
- Summary and inspect syntax may drift: #258 remains the sole syntax owner.
- Summary output may contaminate pipelines: keep it stderr-only.
- Forced termination may prevent complete accounting: fall back instead of
  fabricating a summary.


## Open Questions

None.
