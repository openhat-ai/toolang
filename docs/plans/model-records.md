# Simplify model execution records

Approved in the PR #554 follow-up discussion; implementation starts from PR #564.

## Scope and contract

- ControlRecord run payloads store only model_request, with flat ref, reasoning,
  and max_output fields. Retry uses the same request type. Remove the redundant
  model string and the parameters wire wrapper.
- Model StepRecord given stores model, setup, and call. setup is the revision of
  the setup used for this call, not a foreign key to a new table. Persist effective
  call reasoning alongside existing call fields and retain incremental messages.
- Model StepRecord noted stores accounting and cont only. Remove duplicate
  tokens, price, cost, and reasoning accounting structures and consumer fallbacks.
- Accounting retains usage, pricing {plan, match}, and cost. Remove pricing
  source/revision. Numeric quantities, rates, denominators, and amounts are finite
  non-negative numbers; token totals remain integers. Denominators are positive.
- Cost selection is reported, estimated, zero, or unknown. zero requires a
  complete zero-rate estimate with no provider-reported cost. Missing pricing is
  unknown; incomplete estimates remain estimated with complete=false. Positive
  rates rounded to a zero total are still estimated. Provider reports retain
  reported selection, including reported zero amounts.
- Settle final amounts to six fractional USD digits. Preserve intermediate rate
  precision and exact decimal-rate settlement; accumulate budgets in micro-USD.
- Do not add a SetupRecord/table, historical setup restoration, or model-call
  replay feature. Records retain useful facts for future replay-oriented design.
- Execution database schema 46 rejects older stores without migration.
  No old-format compatibility. RunRecord, ThreadRecord, JobRecord,
  non-model steps, and PR #564 compaction semantics remain unchanged.

## Implementation and acceptance

Update model request codecs, execution types/records/store, model accounting,
executor, HTTP schemas, CLI progress, inspection, and their tests. Cover:

- Durable call round trips preserve setup revision, reasoning, messages, tools,
  output ceiling/schema, and continuation; reopening the store preserves them.
- Run/retry records use the flat request and reject legacy structures.
- Reported, estimated, free, unknown, partial, non-USD, and sub-micro costs retain
  their distinctions through records, progress, inspection, and budget recovery.
- Negative/non-finite/bool numbers and non-positive denominators are rejected.
- Default offline verification passes, including PR #564 compaction scenarios.

No open scope questions. Destructive schema incompatibility is intentional; a
setup revision is provenance only and cannot reconstruct historical routes.
