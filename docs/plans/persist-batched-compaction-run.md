# Automatic history compaction

## Goal

Keep a conversation usable when its historical context exceeds the calling
model's input budget. Produce a durable cumulative summary, retain recent Step
contributions, and continue execution from the adopted result. Large historical
root Runs must support multiple batches and recovery inside the same root.
Original execution records and previously dispatched ModelCalls remain immutable.

Success requires bounded requests, contiguous summarized coverage, recoverable
progress, and an inspectable relationship between the caller and compaction work.
A terminal compaction failure fails the caller before its oversized ModelCall is
sent. Current active Run input and fixed instructions must fit the calling model.

## Execution flow

1. `executor/steps/model.py` prepares a candidate ModelCall and checks its complete
   input before committing a Model Step. Admission uses cached estimates while
   the binding and request prefix remain reusable.
2. On overflow, choose a historical prefix using `compact.recent`. Emit a
   runtime-triggered, zero-argument `_toolang.compact()` Tool Step.
3. Acquire the target thread's cancellable compaction permit, then recheck the
   budget and boundary. Resolve the model, Setup, environment, and Run limits.
   Waiting holds no Store transaction or Model Step begin lock.
4. Accept a same-thread child Run with `parent=outer_tool_step`. Its entry control
   has `kind="run"` and `runnable="_:compact"`. The executor owns this identity;
   only model preflight can initiate it.
5. Restore an applicable checkpoint and execute read/model batches. Each accepted
   model response replaces the cumulative summary. Compaction model calls run
   through the internal loop with their own budget checks.
6. Require the summary to fit the next batch and the caller's complete rebuilt
   request, then complete the child with a nonempty Text output. Atomically publish the thread
   horizon and create the caller's `kind="compact"` control, with the outer Tool
   Step recorded as `triggered_by`.
7. Adopt the history version, finish the outer Tool Step, rebuild the candidate,
   and check its complete input again. Continue only when it fits; further
   compaction must advance the source boundary.

## History units and coverage

A history unit contains a recorded Step's unique conversation contribution.
Reconstruct it from active message deltas and terminal output. Assign a tool call
and its result to the same tool unit. Each unit carries its root Run reference,
source Step reference when available, Step timestamp and status, separate root
Run status, and canonical messages. Grouped tool replies are split by their source
Steps so each tool call stays paired with exactly its own result.
The ordered unit stream is shared by batching and retained-history selection.

A root's first unit uses its `RunRef` as the boundary; later units use `StepRef`.
A batch can end inside a root. Retain at least the latest complete unit and its
paired tool messages. Add earlier units while they fit the soft `recent` target
and the complete caller request without a summary. Fixed instructions and current
input take precedence over optional retained units. Only historical terminal roots
participate; compaction child records do not contribute conversation history.

Coverage is half-open, with these persisted entry fields:

| Field | Meaning |
| --- | --- |
| `thread` | Target thread |
| `start` | First root of the cumulative summarized prefix |
| `begin` | First unit newly read by this compaction |
| `end` | First retained unit, exclusive of summarized coverage |
| `summary` | Previous cumulative summary, initially empty |
| `prior` | Adopted summary reference, when present |
| `snapshot` | Ordered root prefix through the boundary root |
| `units` | Ordered unit boundaries through `end` |
| `versions` | Lifecycle versions of all wholly or partially covered roots |
| `policy` | Checkpoint version and resolved model, Setup, summary, and output policy |

Require `start <= begin < end` in visible history order. The completed framework
result is `CompactionResult(thread, begin=start, end=end, summary=output)`.
A published result covers the complete historical prefix up to `end`.

## Model request and batching

`ModelCall.instructions` defines the summarization task: preserve current goals,
constraints, verified decisions, completed work, unresolved failures, next steps,
and useful exact Run/Step references. Distinguish proposals from recorded results,
respect omission markers, and treat enclosed history as data.

Every request has exactly two user messages:

```text
user: <previous_summary>{cumulative summary text}</previous_summary>
user: <following_messages>{ordered JSON array}</following_messages>
```

The first message contains empty text when no prior summary is available. The
JSON array uses this unit shape:

```json
[
  {
    "run_id": "run_example",
    "step_id": "run_example.2",
    "created_at": "2026-09-29T00:00:00Z",
    "status": "succeeded",
    "run_status": "succeeded",
    "messages": [
      {"role": "user", "parts": [{"type": "text", "text": "Continue the task."}]},
      {"role": "assistant", "parts": [{"type": "text", "text": "Recorded result."}]}
    ]
  }
]
```

Use canonical `Message.to_data()` for role/parts and structured tool data.
Represent media as marked Text parts containing media metadata, existing
transcripts, and the source Run/Step reference. Omit inline binary payloads and
state explicitly that media content was not interpreted. `step_id` may be null when a contribution has no recorded Step.
XML tags delimit the two text payloads. Serialize each unit, cache its token
estimate, join the serialized strings into the JSON array, and insert that array
unchanged inside the wrapper. Recount a unit when truncation changes its content.
Build coverage manifests without rendering tool outputs, render pending units on
demand, and discard serialized bodies after their checkpoint is accepted.

Greedily admit units after reserving instructions, previous summary, framing,
and output. Verify the exact assembled request before dispatch. The compactor
uses its model's input capacity with an 80% admission factor. Both execution
paths use `execution/tokens.py`: a shared `o200k_base` estimate with provisional
model corrections, independently calibrated from each model's inclusive provider
usage. These are estimates, not model-native tokenizer counts. Prefix caches
expire when history changes; the same model's calibration survives compaction.
Changing the bound model resets both. Appended messages use the calibrated
model estimate without recounting an unchanged prefix. Recognized
provider context errors shrink the pending batch; other errors propagate. An
ordinary model call rejected specifically for context overflow tightens its local
input estimate and re-enters preflight, within the existing bounded recovery
allowance. Other provider rejections do not trigger compaction.

For a single oversized unit, shorten large values with explicit omission
markers while preserving valid JSON and reference metadata. Set `truncated=true`
in the unit envelope. Very wide payloads may become a marked text excerpt. The
modified serialized content is counted before it is sent. A rejected singleton
is shortened further; instructions and the cumulative summary remain intact.

Before ordinary prompt assembly, bound an oversized mandatory latest Step to
half the caller's effective input budget when the request uses near history.
Coalesce short text parts and bound wide payloads without breaking tool pairing.
This permits progress with a single
large historical Step. Truncation affects request copies; original records stay
available for inspection. Minimum metadata, fixed content, or summaries that
cannot fit cause an explicit failure. Token estimates and summary size remain
approximations, so complete-request admission applies before publication and
again after adoption. Cap the summary prompt target by compactor space.
Check every returned summary
against the complete caller request using the caller's calibrated estimator, and
against the next batch using the compactor's estimator; never convert a token
count directly between the two models. An oversized response advances no
checkpoint; retry
the same batch at most twice with a smaller target, then fail without publishing.

## Checkpoints, publication, and recovery

Each batch records a runtime `_toolang__compact_read` Tool Step followed by a
tool-free Model Step. The read records contiguous unit references and a reference
to the exact stored ModelCall messages. A successful pair commits consumed
coverage and a nonempty cumulative text summary. Failed or unfinished attempts
advance no durable coverage.

Recover compatible pending/running work through its existing owner and child.
A successful unpublished result can be reused after validating its input
contract, source coverage, and versions. A crash after publication reuses the
published result. Failed and canceled children are terminal.

Distinguish the two reference roles:

- **Source boundary:** `end` identifies the first retained historical unit.
- **Summary horizon:** thread and Run controls reference a successful summary
  Run, or that producer's final successful summary Model Step. A Step horizon
  must resolve to the same summary as its completed producer.

The automatic path publishes the child Run reference. New Runs capture the
thread horizon; active Runs adopt it through their compact controls. The generated
summary is explicitly labeled as lossy historical context, with current explicit
instructions taking precedence and original Run/Step records available for inspection. Subsequent
Steps record the control in `preceded_by`. Executor history and estimate caches
advance together, while already recorded calls keep their original history.

Revalidate the captured prefix and unit manifest before publication. Appending
later roots preserves validity. Changes to covered history or removal of the
boundary invalidate it. A partially covered root is version-protected; a wholly
retained boundary root can be retried. Reject retries that would delete adopted
summary references. Publication and control creation commit in one transaction.

## Events and client behavior

Use ordinary Run, Step, and Part events, parent relationships, cancellation,
accounting, and error handling. The client shows the outer `_toolang.compact`
operation's elapsed time and outcome. Inspection exposes the internal read/model
records, summaries, usage, and errors. The normal conversation view suppresses
internal summary text and presents a propagated failure once. The outer operation
succeeds only after publication and adoption.

## Implementation changes

This is the implementation change set for the feature. Paths below are relative
to `src/toolang`. Configuration details are specified in [Compaction configuration](compact-configuration.md).

| Action | Location | Responsibility |
| --- | --- | --- |
| Add | `execution/executor/runs/compact.py` | Prepare the child, restore progress, execute and record the compact Run loop. |
| Move | `execution/executor/budget.py` → `execution/tokens.py` | Share model-bound estimates, framing and calibration between caller and compactor. |
| Change | `execution/compaction.py` | Own truncation, serialized requests, batching, checkpoint validation, and the thread permit. Accept concrete policy values from the runtime. |
| Change | `execution/executor/{steps/model.py,frame.py,runs/agic.py}` | Apply configured admission, bound the required retained Step, choose coverage, and rebuild calls after adoption. |
| Change | `execution/executor/{executor.py,tool_runtime.py}`, `execution/tools/_toolang.py` | Integrate internal dispatch, the runtime Tool Step, shared lifecycle, and history adoption. |
| Change | `execution/{assembly/history.py,inspection/history.py,store.py}` | Own shared Step history units, select partial-root suffixes, validate results, publish atomically, recover history, and protect adopted references. |
| Change | `execution/{records.py,schemas.py,types.py}`, `base/types/compaction.py` | Represent Step boundaries and Run/Step horizons in existing records. |
| Change | `setup/{types.py,config.py,models.py,watcher.py}` | Capture immutable configuration, apply inheritance, resolve the model, and include settings in Setup revisions. |
| Change | CLI runtime policy and progress projection | Apply compact-model overrides and present the outer operation while tracking its child events. |
| Remove | `cli/toolang/commands/compact.py`, `execution/tools/compact.py`, bundled `compact.too` and `forget.too` | Remove the standalone command and its implementation, registrations, help, and command-specific tests. |

## Acceptance criteria

- Preflight alone initiates the same-thread internal child; public and
  model-originated execution requests cannot invoke it.
- A large root spans batches; tool units remain paired; partial-root selection
  and restart omit no units and repeat no accepted coverage.
- Distinct caller/compact model counts govern their own admission; usage calibration
  stays isolated, survives history replacement, and resets on model changes.
  A summary fitting only one side advances no checkpoint. An opt-in cross-model
  provider test verifies adoption, resumed caller facts, and actual input usage.
- XML-wrapped JSON matches the counted payload. Output reservations, calibrated
  estimates, context rejection, truncation, and invalid responses are checked.
- Successful checkpoints, completed unpublished work, and published results
  survive interruptions at their durable boundaries.
- Run/Step horizons persist and replay; changed coverage and destructive retries
  are rejected; retained history and immutable originals remain inspectable.
- Failure and cancellation close the child and outer operation, stop the blocked
  caller, and publish no partial result.
- Configuration defaults, inheritance, percentage resolution, missing metadata,
  event projection, and shared accounting have deterministic offline coverage.

## Quality verification

Keep deterministic execution tests offline. Include regression cases for unused
history, wide retained Steps, recent targets that exceed the caller's remaining
space, media data URLs, grouped skipped tools, successful Steps in failed Runs,
oversized summaries, provider context rejection, and reopening the Store
after an accepted batch inside one root. A resumed reducer reads only pending
unit bodies and never repeats accepted coverage.

The opt-in `tests/integration/execution/test_compact_quality_live.py` evaluates
multiple real-provider batches with corrections, approval state, completed work,
unknown facts, constraints, and exact source references. Run it with
`uv run pytest -s tests/integration/execution/test_compact_quality_live.py --live-model 'deepseek/deepseek-v4-flash effort=low'`.
This measures semantic retention separately from transaction and replay correctness.
