# Standalone compact command

## Goal and boundary

Run the built-in compact script as a local maintenance command against the
selected agent's history. Supersedes PR #511. Use ordinary `RunSpec` execution:
no new executor operation, lifecycle hook, record kind, client method, HTTP
endpoint, or event. Do not attach to or reconfigure a running agent server.

```sh
too a compact thread=THREAD
too a compact thread=THREAD begin=RUN3 end=RUN6 bare=true --model MODEL
```

Reuse script argument collection, type resolution, cancellation, and progress.
The script declares the inputs; the command supplies the internal `previous`
reference. Do not maintain a second synthetic runnable signature in Python.

## Behavior

- `thread` is required. Reject compact Threads and non-root/unknown bounds.
- `end` is exclusive; default retains the latest terminal root.
- `begin` is inclusive; default follows the latest applicable summary's end,
  or starts at the Thread beginning. If an explicit end precedes the old end,
  the default begin is the Thread beginning. Normalize the first root to null.
- Require a nonempty range, no active root inside it, and a terminal root at or
  after end. If defaults leave no new history, report `nothing to compact` and
  create no Run.
- Reuse the previous summary only when begin equals its end. Earlier/later
  begins are independent intervals. `bare=true` disables reuse without changing
  defaults. Freeze the selected reference; do not rediscover it in the script.
- `--model` uses existing compact-model parsing, including parameters. Otherwise
  resolve CLI-process environment and agent/root compact configuration, then
  eligible allowed models. Require tool calls and structured output. Limits use
  the existing environment/configuration defaults and `--limit` overrides.

Prepare isolated built-in State and read-only history tools. Resolve a concrete
RunSpec and execute it in `compact_<thread>` under the existing cross-process
permit. Persist authored and resolved input through normal Run records.
Recheck the frozen range after admission and completion. Never hold a database
transaction while waiting or executing a model.

Carry typed `{summary, position, complete}` progress between child Runs. Preserve
active task constraints and corrections across pages; keep traversal bookkeeping
in `position`. The final summary contains task notes, not an execution report.
History tools cache opaque cursors in existing `contents` and return fixed-length
content references. Execution facts remain read-only; existing self-contained
cursors remain accepted. No new table or pagination algorithm is needed.
For DeepSeek object output contracts, enable its JSON-object mode and retain
schema instructions and validation. Non-object contracts keep the prompt fallback.

## Results and selection

```text
resolved input: {thread, begin?, end, bare, previous?}
output:         {thread, begin, end, summary}

previous summary + [input.begin, input.end) -> output.summary
output.begin = null when reusing a full prefix, otherwise input.begin
```

The command validates output and exits nonzero on failure, without rewriting
RunEnd events. Run status describes script execution; result validity is checked
separately. Correct interval summaries succeed but are not usable as far.

RunHistory's compact lookup skips unsuitable outputs and retains an older
usable summary. Check both coverage and recorded inputs so a malformed interval
result cannot claim full-prefix coverage. Direct run/output reads still expose
all results. No active target Run or its horizon is changed by the command.

Show ordinary script progress on stderr and `{run, horizon, output}` JSON on
stdout. `horizon` is this output's reference for a valid full prefix, otherwise
null. Cancellation uses ordinary Run cancellation and releases the permit.

## Touchpoints and verification

CLI: a dedicated `commands/compact.py`, registration/routing, and small shared
script input/cancellation helpers. Program: optional Text bounds and the fixed
previous reference; adapt the existing automatic caller's optional input only.
Read side: applicable compact selection and history-tool cursor transport.
Adapter: DeepSeek JSON-object output mapping.
`executor.py`, API/client, records, and event definitions remain unchanged.

Offline checks: default/incremental/bare ranges, earlier/equal/later begin,
malformed arguments/output, partial-result fallback, model/effort/limit defaults,
permit wait/cancel, append/rewind, active-Run isolation, restart and replay,
ordinary script and automatic-preflight regressions. Run the full default suite.

Live summary quality remains a separate acceptance: compact eight of ten Runs,
retain the last two, then verify a new Run before testing automatic preflight.
The opt-in `test_compact_live.py` checks retained constraints, corrections, recent
facts, unknown values, horizon adoption, and persisted-call replay.
Risks are probabilistic summary loss and confusing execution success with an
applicable summary. No remote-only history access is added by this command.
