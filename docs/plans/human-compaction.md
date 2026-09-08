# Human-triggered compaction

Design updated on 2026-09-08. The CLI foundation is implemented in PR1;
`--begin`, `--bare`, incremental defaults, and applicable-result lookup below
are the agreed target, not implemented behavior. Summary fixes remain in PR2.

## Goal and scope

Default compaction produces a cumulative summary usable by future root Runs.
Manual requests may also produce independent interval summaries for testing;
execution success does not imply suitability as far. Keep compact human/runtime
only, reject `compact_…` target Threads, and preserve model-call replay.

No chat slash command, model-triggered compact, Step-level bounds, record schema
change, multi-summary assembly, or memory plugin work.

## CLI

```sh
too <agent> compact THREAD [--begin RUN] [--end RUN] [--bare] \
  [--model MODEL_SPEC] [--limit LIMIT=VALUE]...

# Continue from the previous applicable summary with configured model/limits.
too a compact THREAD

# Summarize only [RUN3, RUN6), without a previous summary.
too a compact THREAD --begin RUN3 --end RUN6 --bare
```

Keep the command in Control commands, beside retry/rerun/fork/rewind.

| Argument | Meaning and default |
| --- | --- |
| `THREAD` | Required normal Thread. |
| `--begin RUN` | Inclusive reading start; default is the latest applicable compact output's `end`, or Thread start when unavailable. |
| `--end RUN` | Exclusive boundary; default is the latest terminal historical root, retaining it. |
| `--bare` | Do not reuse a previous compaction summary. Does not change range defaults. |
| `--model MODEL_SPEC` | Replace compact-model selection for this request, including parameters such as `effort=low`. |
| `--limit LIMIT=VALUE` | Override individual effective runtime limits using existing parsing. |

Without `--model`, use the runtime's resolved `compact.model`: runtime override
(including `TOOLANG_COMPACT_MODEL`), then agent config, then root config. When
unconfigured, select the first eligible model in the effective allowed order,
requiring tool calls and structured output. Never inherit the normal Run model
or change config. An attached server uses its own loaded configuration and
startup environment, not new environment variables in the client shell.

## Range and reuse

Bounds refer to root Runs in the target Thread's logical order, not lexical ID
order. `begin=null` denotes Thread start. Require `begin < end`, no active Run
inside the interval, and at least one terminal root at or after `end`. Reject
unknown/nonmember/child bounds and empty or reversed ranges before provider work.

Let `P` be the latest applicable compact output's `end`. If an explicit `end`
precedes `P`, an omitted `begin` falls back to Thread start. If the defaults
leave no new history (`begin == end`), report `nothing to compact` with nonzero
exit and create no Run; an explicit earlier `begin` can request recomputation.

| Effective begin relative to P | Reuse without `--bare` | Result coverage |
| --- | --- | --- |
| `begin == P` | Reuse the previous summary. | Complete prefix extended through `end`. |
| `begin < P` | Do not reuse or attempt to slice the old summary. | Exactly the requested interval. |
| `begin > P` | Do not reuse across the gap. | Exactly the requested interval. |

`--bare` always disables reuse, including equality. Less/greater cases remain
legal requests; do not search for a different old summary just to enable reuse.
A bare request starting at Thread start can still produce an applicable result.

## Input and output

Resolve defaults once, freeze the bounds and selected previous-output reference,
and persist them in the compact Run input. The compact program receives this
selection; it must not independently discover a newer previous summary.

```text
input:  {thread, begin, end, previous}
output: {thread, begin, end, summary}

previous = fixed compact-output reference, or null
input.begin  = start of newly read history
output.begin = previous.begin when reusing, otherwise input.begin
output.end   = input.end

previous.summary + [input.begin, input.end) -> cumulative output.summary
```

Retain the existing output shape. Validate the target Thread, expected coverage
start, exact end, and nonempty summary before recording success. Do not require
input/output `begin` equality when reusing. Invalid or incomplete output fails
the Run; a correctly produced partial-interval summary succeeds.

## Applicability and horizon

An output is applicable only when its Run succeeded, its summary is nonempty,
its Thread matches, and it covers a complete prefix of the current logical
history with a valid end that retains a historical root. Derive applicability
when selecting; do not persist a flag or judge it from `--bare` alone.

Search successful compact outputs newest first, skipping inapplicable results
until an applicable one is found. A newer successful interval test must not
hide an older usable summary. Failure/cancellation likewise cannot replace it.

- **Manual compact:** only persists its independent Run and output. It does not
  change existing target Runs or add compact controls to them.
- **New root Run:** executor selects an applicable output and persists its
  `FieldRef` as the initial run control's `payload.horizon`.
- **Automatic preflight:** retains budget checks/reuse, then adopts a result
  through the calling Run's compact control. Step `preceded_by` fixes adoption
  order. Replay follows these records, never a latest-result lookup.

No Thread horizon field, duplicated summaries, or new persistence mechanism.

## Execution and presentation

CLI and preflight share isolated `compact_<thread>` execution, authorized model
selection, read-only history tools, the per-target cross-process permit,
cancellation, and validation. Use the existing agent-server/client path.
Revalidate the frozen range after admission and before accepting output; rewind
may invalidate it, ordinary appends do not. Never wait inside a Store transaction.

Run manual requests even below the model input budget. Show ordinary script
progress on stderr: flow/child steps, model/tool activity and outputs, timing,
tokens, and cost. Successful stdout is JSON:

```text
{run, horizon, output: {thread, begin, end, summary}}
```

`horizon` refers to this output only if applicable, otherwise null; returning
it does not mutate any Run binding. Successful partial summaries exit zero.
Failures/cancellation exit nonzero and identify the Run when available. Do not
change events or add a separate test mode.

## Delivery and acceptance

- **PR1 foundation, implemented:** CLI/API/client entry points, shared execution,
  limits/model overrides, frozen ranges, cancellation, and invalid-output failure.
  Actual DeepSeek CLI execution over the first eight of ten fresh Runs returned
  `begin: ""` for null (`run_tc2fgb31`); exit 1 and no adopted horizon confirmed
  failure handling, not successful live compaction.
- **PR1 remaining CLI behavior:** implement `--begin`/`--bare`, fixed previous
  input and incremental defaults, expected output coverage, partial success,
  applicable-output selection, and CLI horizon reporting. Touchpoints: CLI
  `commands/thread.py`, execution/API request transport, `executor/compact.py`,
  `history.py`, and compact program inputs.
- **PR2 correctness and live acceptance:** fix null rendering and cumulative
  summarization in `executor/prompts/compact.too`; keep facts/corrections separate
  from traversal bookkeeping. Then verify successful CLI use before preflight.

Offline acceptance must cover defaults, all three begin/P relations, `--bare`
with omitted/explicit begin, earlier/equal end, invalid bounds, model/effort and
environment precedence, appends/rewind, cancellation, exact output coverage,
partial success without shadowing, active-Run isolation, and restart/replay.

For live acceptance, compact Runs 01–08, retain 09–10, then probe a new Run:
require adopted far, no raw 01–08 inputs, exact 09–10 inputs, preserved facts and
corrections, and dispatched/reconstructed ModelCall equality. Also exercise a
second cumulative compact and a bare interval test. Repeat through automatic
preflight only after CLI acceptance; verify Tool Step -> compact control ->
Model Step, no recursion, and no compact tool result in model conversation.

Default tests stay offline. Run Ruff check/format, ty, and full pytest before
commits. Risks: probabilistic summary loss, gaps mistaken for prefix coverage,
and a successful local test hiding an older applicable result.
