# Human-triggered compaction

## Goal and scope

Execute the built-in compact script manually, independent of the input budget.
Defaults produce a cumulative summary usable by future root Runs; explicit
ranges may produce independent summaries for testing.

No model-triggered compact, recursive compact, Step-level bounds, new record
schema, Thread horizon field, or ModelCall assembly changes.

## Invocation

```sh
too a compact thread=THREAD
too a compact thread=THREAD begin=RUN3 end=RUN6 bare=true --model MODEL
too a compact thread=THREAD --model "MODEL effort=low" --limit time=120
```

The fixed public signature is `compact(thread, begin?, end?, bare=false)`.
Use script-style named arguments and ordinary runnable type resolution.
CLI/API transport one authored `input` mapping, not per-parameter fields.
`--model` and `--limit` remain execution options; changing a script input
does not require transport changes. Keep the entry in Control commands.

| Input | Meaning and default |
| --- | --- |
| `thread: Text` | Required normal Thread; reject `compact_…`. |
| `begin?: Text` | Inclusive root Run; default is the latest applicable summary's end, or Thread start. |
| `end?: Text` | Exclusive root Run; default retains the latest terminal root. |
| `bare?: Boolean` | Default false. True excludes the prior summary without changing range defaults. |

Without `--model`, use the runtime's resolved `compact.model`: runtime override
(including `TOOLANG_COMPACT_MODEL`), agent config, then root config. If unset,
select the first eligible model in the effective allowed order, requiring tool
calls and structured output. Do not inherit the normal Run model. An attached
server uses its loaded configuration/environment, not new client-shell values.
Limits inherit effective runtime defaults, with per-field overrides.

## Range and reuse

Use root Runs in logical Thread order, not lexical ID order. Require a nonempty
`[begin, end)`, no active root inside it, and a terminal root at or after end.
Reject unknown, child, nonmember, empty, and reversed bounds before execution.

Let `P` be the newest applicable summary's end:

| Requested range | Selection |
| --- | --- |
| Begin omitted, end at/after P | Begin at P. |
| Begin omitted, end before P | Begin at Thread start. |
| Effective begin == P | Reuse the prior summary unless bare=true. |
| Effective begin < P or > P | Summarize the interval independently; do not slice, bridge gaps, or seek another old summary. |

If default begin equals end, report `nothing to compact`, exit nonzero, and
create no Run. An explicit earlier begin permits recomputation.
A bare request beginning at Thread start can still produce an applicable result.

## Execution inputs and output

The coordinator resolves the public inputs into the script's concrete internal
inputs before creating its independent Run:

```text
authored_input: {thread, begin?, end?, bare?}
input:         {thread, begin?, end, previous?}
output:        {thread, begin, end, summary}

previous = fixed compact Run output reference; absent when not reused
input.begin = new-history reading start; absent means Thread start
output.begin = null when reusing a full prefix, otherwise the reading start
output.end = input.end

previous.summary + [input.begin, input.end) -> output.summary
```

Normalize the first root to Thread start. Bounds and references are optional
Text inputs, not Json sources; output retains JSON null for Thread start.
This gives full-prefix output a canonical `begin=null`.

The script reads only the supplied previous output; it never discovers a newer
one. Preserve authored input separately from resolved input. Validate exact
target, expected coverage, end, and nonempty summary before recording success.
Invalid/incomplete output fails; a correct partial-interval summary succeeds.

Both CLI and automatic preflight use isolated `compact_<thread>` execution,
authorized models, read-only history tools, ordinary Run lifecycle/cancellation,
and a per-target cross-process permit. Freeze the range and previous reference
before waiting. Revalidate the frozen prefix after admission and before success:
rewind can invalidate it; ordinary appends do not. Do not wait in a transaction.

## Applicability and horizon

Select successful compact outputs newest first, skipping outputs that do not
cover a complete current prefix with matching Thread, nonempty summary, and a
valid exclusive end. A newer successful interval test must not hide an older
usable result. Derive applicability; do not persist a flag.

- Manual compact creates its own Run/output only, never a compact control in an
  existing target Run.
- A new root freezes the selected output reference in its initial
  `RunControlPayload.horizon`.
- Automatic preflight retains budget/reuse checks and adopts output through a
  compact control. `Step.preceded_by` records adoption.
- Replay follows recorded references/controls, never a latest-output lookup.

## Presentation

Reuse script progress on stderr: flow/child Steps, model/tool activity, outputs,
timing, tokens, and cost. Successful stdout:

```text
{run, horizon, output: {thread, begin, end, summary}}
```

A validated full-prefix result returns its output reference as horizon; an
interval result returns null. This reports the result, not a mutation of a
target Run. Partial success exits zero; failure/cancellation exits nonzero and
identifies the Run when available. Events are unchanged.

## Touchpoints and acceptance

PR1: CLI/API/client input transport, `executor/compact.py`, applicable-output
selection in `RunHistory`, and `prompts/compact.too` inputs/coverage contract.

Offline acceptance covers:

- Script argument collection, Boolean/type/name errors, model/effort and limits.
- Incremental defaults, begin before/equal/after P, bare with omitted/explicit
  begin, earlier/equal end, and explicit first-root normalization.
- Partial success without shadowing; incorrect coverage fails.
- Frozen selection during permit waits, append/rewind, cancellation, active-Run
  isolation, restart lookup, and dispatched/reconstructed ModelCall equality.
- Ordinary script parsing and automatic compact regression tests.

PR2: verify summary quality and live acceptance. Compact Runs 01–08, retain
09–10, and probe a new Run: require adopted far, no raw 01–08 inputs, exact
09–10 inputs, preserved facts/corrections, and replay equality. Exercise a
second cumulative compact and a bare interval. Verify CLI success before
automatic preflight, including Tool Step → compact control → Model Step.

The prior live attempt returned an empty string for null and was correctly
rejected; it does not establish successful live compaction. Default tests stay
offline. Run Ruff check/format, ty, and full pytest before commits.
