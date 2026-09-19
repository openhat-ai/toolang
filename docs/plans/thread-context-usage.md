# Define Thread Context Usage in the Chat TUI

## Work Type and Status

Feature definition. Do not implement until a human confirms this document.

## Goal and Success Criteria

Show how full the conversation is for the model call that this thread is about
to make, so a user can see the thread filling up before compaction silently
changes it.

The feature succeeds when:

- the Chat status bar shows the thread's context usage next to the model;
- the number is the size of one assembled Model Call, so it already reflects
  `recall`, `context:`, `instruct:`, bounded history, and compaction;
- only calls that actually carry thread history count, so utility calls cannot
  misreport a full thread as nearly empty;
- agic, flow, child runs, `hands`, `handoffs`, `execute`, and parallel lanes all
  behave predictably;
- an unknown model budget degrades to an absolute token count;
- `/context` explains the number from durable records;
- no new record kind, event kind, HTTP endpoint, or executor operation is added.

## Verified Current Behavior

- `plugin/models/budget.py:input_budget(model_info, output)` derives input
  capacity from `ModelInfo.context_window` minus the output reservation, minus
  `max(1024, 5%)`. `output_budget` resolves the reservation from the target.
- `execution/recall.py:recall_sources` resolves an agic's `recall` directive to
  `("far", "near")` for an omitted value or `auto`, and otherwise to the
  authored selection. `lang/validate.py` accepts exactly `none`, `far`, `near`,
  and `far, near`.
- `execution/executor/frame.py` resolves that selection into `_AgicFrame.recall`
  before the Model Call is assembled. A `recall = none` call therefore sends the
  agic's own messages and no thread history.
- The executor keeps one execution-local `InputEstimate`
  (`execution/executor/budget.py`). It counts a `ModelCall` locally, is
  calibrated by provider `input_tokens` after each call, and triggers
  auto-compaction when it exceeds the budget
  (`executor/steps/model.py:_boundary`). Compaction rewrites the assembled
  request and emits no separate event.
- Every completed Model Call records `ModelStepNoted.accounting.input_tokens`,
  provider-reported or calibrated, and reaches the CLI through `StepEnd.noted`.
- `ModelStepGiven` records only `model` and `call`. The resolved recall
  selection is not durable, and the public event codec exposes only the call, so
  a client cannot tell a history-carrying call from a utility call today.
- `cli/common/execution_progress/state.py:Metrics` sums `input_tokens` per run
  tree. Nothing tracks the newest call's size or the budget, and the Chat status
  bar has no context segment.
- `list_models` (local) and `/api/v1/models` expose ref, name, provider,
  reasoning, and price, but not `context_window` or `max_output_tokens`.
- Durable reads already deliver a breakdown: `RunDetail.steps[].noted` and
  `ThreadDetail.runs`. The remote chat client already fetches
  `GET /api/v1/runs/{run_id}`.
- Status-bar fitting reduces, in order, the differing default runnable, the
  runnable, the model, then the gap, so the model keeps the right edge.

## Core Logic

### Definitions

- **History-carrying call**: a completed Model Call whose resolved recall
  selection contains `near` or `far`. `recall = none` calls are *utility calls*
  and never count.
- **Run value**: for one Run, the `input_tokens` and model of its newest
  history-carrying call.
- **Active Run**: a Run whose `RunBegin` has no matching `RunEnd` in this Chat
  session.

### The displayed value

```text
if any active Run has a value:
    displayed = the largest such Run value
else:
    displayed = the newest history-carrying call in the thread
```

The value is a snapshot of one real call. Nothing is predicted, extrapolated, or
summed.

### Case 1: only one agic (the common case)

The thread runs one agic, so one Run is active and it makes one call at a time.
The displayed value is simply that agic's newest call, and the gauge is:

```text
used / input_budget(model)
```

```too
agic chat:
  You are a helpful assistant.
```

- Before the first call completes, or in a fresh thread, there is no value and
  the segment is absent.
- When that call completes, the segment appears with its input size.
- Every later call replaces it. Because each call re-sends the conversation plus
  its new content, the number normally grows.
- A new submission in the same thread is a new Run. Until its first
  history-carrying call completes, the previous value stays on screen, because
  that call is the last measurement of the same conversation.
- After compaction, the next call is smaller and the number drops. No separate
  compaction indicator is added; `/context` shows the drop between rows.

### Case 2: one agic with tool calls

A tool loop is still one Run. Each tool result enlarges the next assembled
request, so the value rises per model call and the gauge tracks that rise. No
extra rule is needed.

### Case 3: a `recall = none` agic

Its calls carry no thread history, so they are utility calls and are ignored.
This is required for correctness: inline agics used by `sort by:`, `keep if:`,
`map using:`, and `gather using:` are `recall = none`, and their requests are
small no matter how full the thread is. If they counted, a thread with 100k
tokens in context would display as a few thousand the moment one evaluator
finished.

Consequences:

- A thread whose only calls were utility calls never shows a segment.
- A thread that mixes them keeps the last history-carrying value, even while a
  utility call is the newest call overall.

### Case 4: a flow

A flow makes its own calls when it has authored messages, and it runs children
through `run`, `map`, `scatter`, `gather`, `settle`, `storm`, and `keep`/`drop`
filters.

- A child agic that recalls history produces a history-carrying call, so a flow
  that only delegates still shows the conversation size measured by its child.
  This is why the rule is not limited to the root Run.
- Child utility calls do not count, so `sort by:` and `keep if:` steps do not
  disturb the number.
- A finished child Run stops being active. Its value normally remains visible
  because it is still the newest history-carrying call in the thread.

### Case 5: concurrent children

`storm`, `map`, and `scatter` lanes, plus `hands` and `execute`, can have several
Runs active at once.

- Each active Run contributes its own newest value; the largest is displayed.
- A lane finishing removes its contribution, so the number never shrinks because
  a smaller sibling completed while a larger one is still running.
- When all Runs finish, the newest history-carrying call in the thread is
  displayed.

### Case 6: unknown budget or no measurement

- No history-carrying call yet: the segment is absent. Never show `0%`, `-`, or
  a placeholder.
- Budget known: `ctx NN%` with `NN = floor(100 * used / budget)`, and
  `ctx 100%+` when the call exceeds the budget.
- Budget unknown (`input_budget` returns `None`): `ctx 12.3k`, using the existing
  compact token style, with no percentage.

## Decisions

### 1. Denominator

`input_budget(info, output_budget(target, info))` — the same capacity that
triggers auto-compaction. The percentage then reads as distance to compaction,
which is actionable; raw `context_window` would understate the constraint by the
output reservation and the margin.

### 2. Recording the recall selection

The renderer must distinguish history-carrying calls. Decision: add the resolved
selection as `recall: tuple[str, ...]` on `ModelStepGiven`, the record that
already holds "facts known at `StepBegin`". The value is available at
`frame.py` build time and serializes as a small array.

This makes live rendering, durable seeding, `/context`, local Chat, and remote
Chat agree on one rule, and it needs no content resolution. It changes the
existing `steps.given` mapping, so the RunStore schema version advances and older
stores stay rejected and unmodified under the existing exact-version policy.

Rejected alternatives:

- Inferring history-carrying from the assembled messages: the first call of a
  fresh thread looks the same as a `recall = none` call.
- Limiting the gauge to the root Run: a delegating flow would show nothing.
- Sending recall only on the transient event: durable seeding and `/context`
  would need a second, weaker rule.

### 3. Display

One dim label with a normal value, placed immediately before the model:

```text
■ flow:research                                  ctx 35% · openai/gpt-5
◓ agic:chat 18s                   flow:research · ctx 35% · openai/gpt-5
```

- A call in flight does not change the value; it updates at `StepEnd`.
- Fitting keeps the model at the right edge. The reduction order becomes the
  differing default runnable, then `ctx`, then the runnable, then the model,
  then the gap. A zero-width `ctx` segment is dropped, not truncated to `c…`.
- `ctx` and its ` · ` separator use the existing dim right-side styling.
- Switching threads, or `/clear`, resets the value to none.

### 4. `/context`

A new slash command that explains the number from durable records:

```text
runnable          model          input   budget  use
agic:research     openai/gpt-5   12.3k   128k    9%  *
agic:chat         openai/gpt-5   3.1k    128k    2%
```

- One row per history-carrying Model Call, newest first, bounded to the most
  recent eight calls in the current thread's runs.
- Columns are `RUNNABLE`, `MODEL`, `INPUT`, `BUDGET`, `USE`, using the existing
  table conventions: per-result widths, one line per row, `…` truncation, `-`
  for an unavailable budget, `?` for an unknown share.
- The row backing the status segment is marked with the existing ` *` suffix.
- A summary line states the count and the thread, for example
  `2 model calls · thread term_ab12`.
- Utility calls are excluded from the table and counted in a trailing note only
  when the thread has some, for example `3 utility calls not shown`.
- Requesting `/context` before any model call prints one explanatory line and
  creates no run, like the other plural inspection commands.
- The command changes no session setting and creates no run.

### 5. Startup, recovery, and thread switches

Startup and recovery seed the value from durable records: the newest
history-carrying call in the thread, read once with its Step reference. Live
events then update it. A thread switch resets it and reseeds.

### 6. Data path

- Live: `StepEnd` already carries `noted.accounting`. `StepBegin` carries the new
  `given.recall`. The Chat presenter keeps a per-Run value keyed by `RunRef` and
  drops entries on `RunEnd`.
- Budget: extend the model payload with `context_window` and `max_output_tokens`
  in the local `list_models`, the `/api/v1/models` response, and the remote
  client decoding. The CLI computes `input_budget` locally so the reservation
  policy stays in one place.
- Breakdown: `GET /api/v1/runs/{run_id}` already returns steps for remote; the
  local client reads the same durable records. Add one read-only client method
  for the newest history-carrying calls of a thread.

## Scope

In scope:

- `recall` on durable `ModelStepGiven` and the RunStore schema version that
  follows;
- status-bar context segment and its width fitting;
- `/context`, its completion entry, and its help row;
- model payload addition (`context_window`, `max_output_tokens`) across local,
  HTTP, and remote client;
- per-Run value tracking in the Chat presenter, with durable seeding;
- tests and the presentation docs.

Out of scope:

- changing when compaction triggers, or its threshold;
- a compaction indicator, before/after comparison, or prompt viewer;
- estimating tokens before a call completes;
- Script progress, run footers, `inspect`, and non-Chat surfaces;
- per-directive cost prediction or a context editor.

## Likely Files

- `src/toolang/execution/types.py`: `recall` on `ModelStepGiven`.
- `src/toolang/execution/executor/frame.py`,
  `executor/steps/model.py`: supply the resolved selection.
- `src/toolang/execution/store.py`, `records.py`: the `steps.given` mapping and
  the schema version.
- `src/toolang/cli/toolang/commands/chat/widgets.py`: status segment, styling,
  fitting order.
- `src/toolang/cli/toolang/commands/chat/tui.py`: value state, seeding, reset.
- `src/toolang/cli/toolang/commands/chat/base.py`: one read-only
  thread-context method on `ChatClient`.
- `src/toolang/cli/toolang/commands/chat/local.py`, `.../chat/remote.py`: that
  method over durable records and `RunDetail`.
- `src/toolang/cli/toolang/commands/chat/slashes.py`, `.../completion.py`,
  `.../tables.py`: the `/context` command.
- `src/toolang/cli/common/execution_progress/state.py`: expose each model Step's
  accounting and recall to the presenter.
- `src/toolang/api/routers/agent.py`, `src/toolang/api/schemas.py`: the two added
  model fields.
- `docs/chat.md`, `docs/execution-presentation.md`, `docs/run-step-records.md`.
- `tests/unit/cli/test_chat_tui.py`, `tests/unit/cli/test_chat_command.py`,
  `tests/unit/execution/test_store_schema.py`,
  `tests/unit/plugin/test_model_catalog.py`,
  `tests/integration/api/test_streaming.py`.

## Acceptance Tests

1. Only an agic: after its first call completes the segment shows
   `floor(100 * input_tokens / input_budget)`, and a later call in the same Run
   replaces it.
2. Only an agic, before its first call: the segment is absent, and no `0%` or
   placeholder renders.
3. Tool loop: a call following a tool result raises the number.
4. `recall = none`: a completed call changes nothing, and a thread whose only
   calls were utility calls shows no segment.
5. Flow delegating to a child agic: the child's history-carrying call is shown.
6. Flow with a `sort by:` evaluator finishing last: the value stays at the last
   history-carrying call rather than dropping to the evaluator's size.
7. Two active Runs: the larger value is shown, and it does not change when the
   smaller Run ends.
8. All Runs finished: the newest history-carrying call in the thread is shown.
9. New Run in an existing thread: the previous value is shown until the new
   Run's first history-carrying call completes.
10. Budget unknown: the segment reads `ctx 12.3k` with no percentage.
11. A call exceeding its budget renders `ctx 100%+`.
12. Model segment right edge is unchanged at a fixed width when `ctx` appears,
    and a narrow width drops `ctx` before truncating the model.
13. `/context` lists history-carrying calls with `RUNNABLE`, `MODEL`, `INPUT`,
    `BUDGET`, and `USE`, marks the status row ` *`, notes hidden utility calls,
    prints the summary, and creates no run.
14. `/context` with no model call reports that nothing is measured and creates no
    run.
15. Startup on an existing thread seeds the segment before any new call.
16. `/api/v1/models` exposes `context_window` and `max_output_tokens`, and the
    remote client parses them.
17. The resolved `recall` selection round-trips through `steps.given`, and an
    older store version is still rejected unchanged.
18. The default repository verification passes.

## Risks

- `input_tokens` is not perfectly comparable across providers; switching models
  mid-thread can move the percentage without the conversation growing. Accepted,
  because the same number drives compaction.
- A provider that reports no usage falls back to the local estimate, which
  reserves a fixed amount per media part; the percentage is then approximate.
- Adding the recall field advances the RunStore schema, so existing stores are
  rejected exactly as they are today; no migration is added.
- Per-Run tracking must drop entries on `RunEnd` and on thread switch, or a long
  session grows in memory and can display a value from a finished Run.
- Adding a right-side segment can move the model's right edge or overflow narrow
  terminals; the fitting-order test is the guard.

## Open Questions

None. The measured call, the utility-call exclusion, the agic/flow/concurrency
rules, the label text, and the `/context` columns are decided above.
