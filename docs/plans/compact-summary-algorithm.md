# Text-only compaction algorithms

Status: approved for implementation in conversation.

## Contract

- The bundled and external `agic compact(thread: Text, begin: Text, end: Text,
  previous_summary: Text) -> Text` read only the explicit half-open range and
  return summary text. All inputs are concrete; absent previous summary is `""`.
- The bundled source uses implicit user prose, no `instruct` or `user:` block.
  Keep isolated history tools, `recall = none`, and `context: none`.
- Callers choose coverage and validate previous results. Pass previous summary
  content directly; remove discovery, bare, null bounds, and identifier echoing
  from the model's responsibilities.
- Framework assembly constructs `CompactionResult` with the previous result's
  begin (or requested begin), requested end/thread, and nonempty returned text.
  Model output cannot select coverage. FORGET uses the same construction.

## Durable results

Preserve existing complete result objects and horizon summary field references.
Keep the producer's text output untouched. After successful generation and
validation, persist the complete result through a separate model-free result
flow in the same compact thread, linking its producer Run in recorded input.
Only this complete result is eligible for discovery/adoption. Both phases stay
inside the existing permit and cancellation lifecycle. Failed/empty generation
must never publish a result. Old structured results remain readable unchanged.

## Touchpoints and acceptance

Shared assembly and result-flow preparation belong to execution; CLI owns file
selection. Update automatic and CLI callers, the external algorithm signature,
model eligibility (tool support remains required, structured output does not),
and stale contract documentation. Keep CLI usage and budgets unchanged.

Offline tests cover concrete/full/incremental coverage, special-character text,
legacy result loading, invalid/empty output, durable result references, FORGET,
restart/replay, cancellation, history traversal, and stale queued requests.
Run all default verification. Run the opt-in real-provider compaction test with
an available configured model; report actual outcomes and any limits.

Risks: compaction now has separate producer/result Runs; persist their linkage
and distinguish them in inspection/tests. External algorithms must adopt the
new required text-only signature. Summary quality remains model-dependent.
