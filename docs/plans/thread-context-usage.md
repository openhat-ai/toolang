# Define Thread Context Usage in the Chat TUI

## Work Type and Status

Feature definition. Do not implement until a human confirms this document.

## Goal and Success Criteria

Show how much of the current conversation the next model call will occupy, so a
user can see the thread filling up before compaction silently changes it.

The feature succeeds when:

- the Chat status bar shows the thread's context usage next to the model;
- the number is the size of one assembled Model Call, so it already reflects
  `recall`, `context:`, `instruct:`, bounded history, and compaction;
- it stays correct for agic, flow, child runs, handoffs, `execute`, and
  parallel lanes, without flapping or showing a stale zero;
- an unknown model budget degrades to an absolute token count;
- `/context` explains the number from durable records;
- no new record, event, HTTP endpoint, or executor operation is introduced.

## Verified Current Behavior

- `plugin/models/budget.py:input_budget(model_info, output)` derives input
  capacity from `ModelInfo.context_window` minus the output reservation, minus
  `max(1024, 5%)`. `output_budget` resolves the reservation from the target.
- The executor keeps one execution-local `InputEstimate`
  (`execution/executor/budget.py`). It counts a `ModelCall` locally and is
  calibrated by provider `input_tokens` after each call. Before a Model Call it
  auto-compacts when the estimate exceeds the budget
  (`executor/steps/model.py:_boundary`). Compaction rewrites the assembled
  request and emits no separate event.
- Every completed Model Call records `ModelStepNoted.accounting.input_tokens`,
  provider-reported or calibrated. It reaches the CLI through
  `StepEnd.noted`.
- `cli/common/execution_progress/state.py:Metrics` sums `input_tokens` per run
  tree. Nothing tracks the latest call's size or the budget, and the Chat status
  bar has no context segment.
- `list_models` (local) and `/api/v1/models` expose ref, name, provider,
  reasoning, and price, but not `context_window` or `max_output_tokens`, so a
  client cannot compute a budget.
- Durable reads already deliver a breakdown: `RunDetail.steps[].noted` and
  `ThreadDetail.runs`. The remote chat client already fetches
  `GET /api/v1/runs/{run_id}`.
- Status-bar fitting reduces, in order, the differing default runnable, the
  runnable, the model, then the gap, so the model keeps the right edge.

## Decisions

### 1. What is measured

The input tokens of **one assembled Model Call**: the most recent completed call
in the thread. This is the only number that already accounts for every authored
influence, because it is the request that was actually sent.

Rejected:

- Cumulative thread tokens: cannot be compared with a window.
- Predicting per-directive cost from `recall`/`context`/`instruct`: duplicates
  assembly and would disagree with the real request.
- The live `InputEstimate`: execution-local, not persisted, and absent from
  events.

### 2. Denominator

`input_budget(info, output_budget(target, info))` — the same capacity that
triggers auto-compaction. A percentage therefore reads as "how close this thread
is to being compacted", which is actionable; raw `context_window` would
understate the constraint by the output reservation and margin.

When `input_budget` returns `None` because neither `context_window` nor an
input limit is known, the segment shows tokens without a percentage.

### 3. agic, flow, and child runs

Context is per Model Call, so it is per executing agic. Policy:

- while one or more Model Calls are active, show the **largest** input among
  them, because that call is the one that can trigger compaction;
- when no Model Call is active, show the most recent completed call in the
  thread;
- child runs, `hands`, `handoffs`, `execute`, and inline adhoc agics all
  contribute equally; no runnable is treated specially.

A flow that delegates to children shows the child's occupancy, which is the
binding constraint. Taking the maximum also keeps the value stable when lanes
finish in a different order, instead of flapping between lane sizes.

### 4. Status-bar presentation

One dim label with a normal value, placed immediately before the model:

```text
■ flow:research                                  ctx 35% · openai/gpt-5
◓ agic:chat 18s                   flow:research · ctx 35% · openai/gpt-5
```

- Known budget: `ctx NN%`, with `NN = floor(100 * used / budget)`, capped at
  `100%+` when the call exceeds the budget.
- Unknown budget: `ctx 12.3k` using the existing compact token style.
- No completed call yet in this Chat session: the segment is absent; there is no
  placeholder, zero, or dash.
- A call in flight does not change the number; the value updates at `StepEnd`.
- Fitting: the model keeps the right edge. Reduction order becomes the differing
  default runnable, then `ctx`, then the runnable, then the model, then the gap.
  A zero-width `ctx` segment is dropped rather than truncated to `c…`.
- `ctx` and its ` · ` separator use the same dim label styling as the other
  right-side labels.

### 5. `/context`

A new slash command that explains the number from durable records:

```text
runnable          model          input   budget  use
agic:research     openai/gpt-5   12.3k   128k    9%  *
agic:chat         openai/gpt-5   3.1k    128k    2%
```

- One row per Model Call, newest first, bounded to the most recent eight calls
  in the current thread's runs.
- Columns are `RUNNABLE`, `MODEL`, `INPUT`, `BUDGET`, `USE`, using the existing
  table conventions (per-result widths, one line per row, `…` truncation,
  `-` for an unavailable budget, `?` for an unknown share).
- The row backing the status segment is marked with the existing ` *` suffix.
- A summary line states the count and the thread, for example
  `2 model calls · thread term_ab12`.
- Requesting `/context` before any model call prints one explanatory line and
  creates no run, like the other plural inspection commands.
- The command changes no session setting and creates no run.

### 6. Startup and reconnect

On Chat startup, and after a recovered run, the segment is seeded from durable
history: the latest Model Call in the thread, read once. Live events then update
it. This keeps an existing thread informative before the next call and matches
the durable truth after a reconnect.

### 7. Data path

- Live: `StepEnd` already carries `noted.accounting`; the Chat presenter records
  `(step, runnable, model, input_tokens)` per model Step and clears it when that
  Step's Run leaves the active set.
- Budget: extend the model payload with `context_window` and
  `max_output_tokens` in the local `list_models`, the `/api/v1/models` response,
  and the remote client decoding. The CLI computes `input_budget` locally, so
  the reservation policy stays in one place.
- Breakdown: `GET /api/v1/runs/{run_id}` (`RunDetail`) already carries steps for
  remote; the local client reads the same durable records directly. Add one
  read-only client method for the latest model calls of a thread.

## Scope

In scope:

- status-bar context segment and its width fitting;
- `/context` command, its completion entry, and its help row;
- model payload addition (`context_window`, `max_output_tokens`) across local,
  HTTP, and remote client;
- live tracking of per-Step input tokens in the Chat presenter;
- durable seeding on startup and recovery;
- tests and the presentation docs.

Out of scope:

- changing when compaction triggers, or its threshold;
- a compaction indicator, before/after comparison, or per-step prompt viewer;
- estimating tokens before a call completes;
- Script progress, run footers, `inspect`, HTTP schemas beyond the model
  payload, and non-Chat surfaces;
- per-directive cost prediction or a context editor.

## Likely Files

- `src/toolang/cli/toolang/commands/chat/widgets.py`: status segment, label
  styling, fitting order.
- `src/toolang/cli/toolang/commands/chat/tui.py`: state hook and seeding.
- `src/toolang/cli/toolang/commands/chat/base.py`: one read-only thread-context
  method on `ChatClient`.
- `src/toolang/cli/toolang/commands/chat/local.py`,
  `.../chat/remote.py`: that method over durable records and `RunDetail`.
- `src/toolang/cli/toolang/commands/chat/slashes.py`,
  `.../chat/completion.py`, `.../chat/tables.py`: the `/context` command.
- `src/toolang/cli/common/execution_progress/state.py`: expose the latest model
  Step's accounting to the presenter.
- `src/toolang/api/routers/agent.py`, `src/toolang/api/schemas.py` (or the
  existing models payload builder): the two added model fields.
- `docs/chat.md`, `docs/execution-presentation.md`: the current contract.
- `tests/unit/cli/test_chat_tui.py`, `tests/unit/cli/test_chat_command.py`,
  `tests/unit/plugin/test_model_catalog.py`,
  `tests/integration/api/test_streaming.py`.

## Acceptance Tests

1. After a model Step completes, the status bar contains `ctx NN%` immediately
   before the model segment, with `NN` equal to
   `floor(100 * input_tokens / input_budget)`.
2. A call exceeding its budget renders `ctx 100%+`.
3. With an unknown budget the segment renders `ctx 12.3k` and no percentage.
4. Before any model Step in the session, the segment is absent.
5. With two active Model Calls in parallel lanes, the larger input is shown and
   the label does not change when the smaller lane finishes.
6. A flow whose child agic holds the largest input shows that child's value.
7. The model segment's right edge is unchanged at a fixed width when `ctx`
   appears, and a narrow width drops `ctx` before truncating the model.
8. `/context` lists the newest model calls with `RUNNABLE`, `MODEL`, `INPUT`,
   `BUDGET`, and `USE`, marks the status row with ` *`, prints the summary line,
   and creates no run.
9. `/context` with no model call reports that nothing has been measured and
   creates no run.
10. Startup on an existing thread seeds the segment from durable history before
    any new call.
11. The `/api/v1/models` payload exposes `context_window` and
    `max_output_tokens`, and the remote client parses them.
12. The default repository verification passes.

## Risks

- `input_tokens` from different providers is not perfectly comparable across
  models; switching models mid-thread can move the percentage without the
  conversation growing. The plan accepts this because the same number drives
  compaction.
- A provider that reports no usage falls back to the local estimate, which
  contains a fixed media reservation; the percentage is then approximate. The
  segment must not claim more precision than the data supports.
- Adding a right-side segment risks moving the model's right edge or overflowing
  narrow terminals; the fitting-order test is the guard.
- Tracking per-Step values must clear finished Steps, or a long thread will grow
  unbounded in memory and show a stale maximum.

## Open Questions

None. The percentage denominator, the agic/flow policy, the label text, and the
`/context` columns are decided above.
