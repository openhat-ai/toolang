# Persist automatic compaction as an internal child Run

Status: Approved for implementation. This revision replaces the separate compaction thread and CLI producer design.

## Goal and success criteria

Before a ModelCall exceeds its input budget, compact complete historical exchanges into a durable cumulative summary. Execute compaction as an internal child Run in the caller's thread, with auditable read/model Steps and recoverable batch checkpoints. Continue the caller only after publishing and adopting the result; a terminal compaction failure fails the calling Run without sending the oversized ModelCall.

## Entry and execution identity

- ModelCall preflight is the only entry that can initiate compaction. It checks the prospective request before committing the Model Step or invoking the provider. After compaction, rebuild the request and check its budget again.
- Represent the operation as one runtime-triggered, zero-argument `_toolang.compact()` Tool Step. Resolve the current thread, allowed compact model, history boundary, environment, and limits from the caller. Recheck the boundary after acquiring the thread's compaction permit.
- Accept a child Run in the same thread with `parent` pointing to that Tool Step. Reuse the normal Run entry control (`kind="run"`), persisting `payload.runnable="_:compact"` and concrete execution inputs. Do not add a Run table column, control kind, or authored language declaration.
- `_:compact` uses a reserved internal namespace outside user runnable names and is an executor-owned identity. Public CLI/API Run requests and `_toolang.run/execute` cannot invoke it. Do not add it to public runnable discovery or the agic/flow language parser. Reject model-originated `_toolang.compact` calls at execution, not only by omitting its schema.
- Reuse child acceptance, active ownership, RunBegin/RunEnd, cancellation, accounting, and failure handling. Add the internal execution dispatch without manufacturing an AgicDecl or starting an independent executor. The compact Run never runs ordinary agic preflight or recursively compacts itself.

## Module boundaries

Keep two compaction implementation modules:

| Module | Responsibility |
| --- | --- |
| `src/toolang/execution/compaction.py` | Explicit compaction inputs and state; history reconstruction; whole-root batch selection; token estimation; reducer request construction; cumulative summaries; context-overflow batch reduction; checkpoint and result validation; compaction permit. No dependency on executor internals. |
| `src/toolang/execution/executor/runs/compact.py` | Runtime preparation and a short Run loop: restore progress, obtain batches, execute and record read/model Steps through executor facilities, feed results back into compaction state, and return the final summary. |

Keep one execution loop in `executor/runs/compact.py`; the core supplies state and operations rather than a second provider-invocation loop. Resolve setup and environment defaults at the runtime boundary, and pass concrete values into the core so its functions remain reusable.

`executor/steps/model.py` owns admission and retriggering. `executor/executor.py` owns shared Run lifecycle and history adoption. `executor/tool_runtime.py` is a thin bridge. `store.py` and `inspection/history.py` retain their persistence and read responsibilities, reusing core validation where appropriate.

Remove `execution/batched_compaction.py`, `execution/tools/compact.py`, and `execution/executor/compact.py` after moving their required behavior into these boundaries. Replace the legacy contents of `execution/compaction.py`; do not introduce another compaction package or generic execution framework.

## Batches and durable checkpoints

- Preserve the experimental whole-exchange algorithm and token estimator. Capture an ordered root prefix and use half-open coverage: `start` is the first covered root, `begin` is the first newly read root, and `end` is the exclusive retained root. No skipped roots or root-internal splitting. Exclude active roots and retain a terminal root.
- Persist the range, previous cumulative summary and horizon, captured root identities, covered-root lifecycle versions, and concrete reducer policy in the child Run's entry inputs. The retained boundary must remain visible in the same position, but its lifecycle version is not frozen because its content is not summarized.
- Greedily admit complete-root batches using the full ModelCall estimate, output reservation, model input budget, and safety margin. Adapt estimates from provider usage. On a recognized context rejection, record the failed attempt and shrink only at root boundaries. Unrelated errors propagate; an indivisible root that cannot fit fails clearly.
- Each batch has a runtime read Tool Step followed by a tool-free Model Step. The read records root identities and references the model's stored message content. The Model Step records the exact call, response, and provider accounting. Each call contains only the previous cumulative summary and the current batch's semantic messages, without automatically appending earlier reducer outputs or tool receipts.
- A successful read/model pair is the checkpoint: contiguous consumed coverage and a nonempty cumulative text summary. An unfinished or failed model attempt advances no coverage. Restore progress from existing Steps; do not add a checkpoint table. Restore relevant usage before further calls and do not repeat already committed successful work.
- Resume compatible abandoned pending/running attempts through their owning Tool Step and child Run. Never resume terminal failed/canceled attempts. A valid successful but unpublished result may be reused without model calls after validating its range, prior summary, and covered history. Reuse preserves the original parent relationship rather than reparenting a Run.

## Publication and adoption

Complete the child Run first, with its final text output. Then reuse `_Execution.compact(step, child_run_ref)` to validate and publish the thread horizon and create the caller's existing compact control in one transaction:

- Child entry control: `kind="run"`, `runnable="_:compact"`; records the execution request.
- Caller compact control: `kind="compact"`, `payload.horizon=child_run_ref`, `triggered_by=outer_tool_step`; records the result available for adoption.

Do not publish inside the reducer or publish twice. Allow a successful internal child Run as a horizon; remove the requirement that the producer be a root Run. Keep the existing control schema, internal history-view adoption, and subsequent Step `preceded_by` association. Finish the outer Tool Step only after publication and adoption succeed.

A crash after child success leaves a reusable result. A crash after publication must not require another reducer call. Revalidate the effective published horizon and current visible prefix under the permit and transaction; an obsolete raw horizon after rewind must not permanently block a new valid result. Appended roots do not invalidate covered history. Changed covered roots or a removed boundary do invalidate it.

Compaction child Runs are execution records, not new root exchanges. Their internal read/model messages must not enter ordinary conversation history. Earlier ModelCalls remain immutable; future calls use summary plus retained history and current execution messages.

## Events and client presentation

Publish the normal child Run and Step events through the caller's event stream, including existing Part events when produced. Preserve parent relationships and standard accounting; do not add compaction-specific event types.

The default client view shows the outer `_toolang.compact` item with elapsed time and terminal status. Consume and track the internal subtree, but suppress its read/model text from normal conversation output. Execution inspection retains the complete subtree, outputs, usage, and errors. Present a propagated failure once at the outer operation rather than repeating the same error at each ancestor. Child success alone does not complete the outer operation; publication and adoption must also succeed.

## CLI removal

Remove the standalone CLI `compact` command, its registration, routing, help, documentation, and command-specific tests. Remove DEFAULT/FORGET/custom `.too` producer support and bundled `defaults/compact.too` and `defaults/forget.too`. Remove `compact_<thread>` creation, discovery, idle checks, and name-based special cases. Keep compact-model configuration and runtime compact progress presentation, which automatic compaction still uses. Existing stored history is not deleted by this change.

## Acceptance tests and verification

Offline acceptance tests must cover:

- Preflight creates a same-thread child under the runtime Tool Step with `_:compact`; all public/model invocation paths reject internal execution.
- Alternating read/model Steps preserve complete exchanges, exact calls, usage, nonempty summaries, and half-open coverage without accumulating previous batch inputs.
- Token admission, provider-usage calibration, context-only shrinking, oversized roots, and invalid model outputs.
- Failure and cancellation reach the child, outer Tool Step, and caller without dispatching the blocked normal ModelCall or publishing a partial result.
- Interruption around read/model completion, child success, publication, and adoption; committed checkpoints resume without repeated successful calls and valid completed results are reused with zero calls.
- Covered-root changes and rewind invalidate results; retained-root retries and appended later roots do not invalidate unchanged covered history or permanently block new compaction.
- Shared executor lifecycle and accounting include the child; durable replay reconstructs the event tree and stored calls. Default client output hides internal summaries while retaining compact progress and one failure explanation.
- The caller adopts the result through its existing compact control; subsequent Steps record causality. Internal child Steps do not leak into root history.
- CLI compact is absent; automatic compact-model configuration and ordinary agic/flow behavior remain intact.

Before every implementation commit, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`. Run `git diff --check`; before final PR handoff, rebase onto current `origin/main`, verify, push, and resolve review threads.

## Risks and deferred scope

Model-specific token factors are provisional; retain the safety margin and provider rejection handling. Child ownership, restart recovery, and replay must stay consistent when a process stops between durable boundaries. Removing the CLI is an intentional behavior change. Root-internal splitting, additional public compaction entry points, semantic quality evaluation, and a generic internal-runnable registry are out of scope. No design questions remain open for this implementation.
