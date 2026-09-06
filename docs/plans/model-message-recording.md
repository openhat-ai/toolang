# Record model message deltas (PR6)

## Goal and scope

Approved implementation definition. Record each Model Call's new messages once,
while retaining the exact normalized request received by the adapter. Follow
[bounded history](bounded-execution-history.md) and
[runtime tool results](runtime-tool-results.md).

Keep current history selection, roles, Part boundaries, tool grouping, and adapter
interfaces. Defer far/near/now selection, recall triggers, compaction, runtime
tools, and runspace. Keep instructions, tools, output_schema, and continuation
storage unchanged.

## Format

Each Model Step stores `ModelCallRefs.delta`:

```javascript
{
  version: 1,
  messages: [
    {role: "assistant", segments: [{"?": "run_ab12.0/output/value:Part[]"}]}
  ]
}
```

`MessageTemplate` contains `role` and `segments`; `MessageDelta` contains `version`
and `messages`. Segments use existing `str | Part | TypedRef` values. Serialize
Parts as their ordinary objects and references as `{"?": "typed-pointer"}`.
Ordinary strings are always literal, even when they look like pointers.

Version 1 expands strings and Text references into individual TextParts, Part
objects/references into individual Parts, and Part-array references in order.
Do not merge adjacent text, insert separators, recursively flatten tool data, or
reinterpret arbitrary JSON objects. JSON intended as text is encoded before
recording. Each template produces exactly one Message, including empty messages
and repeated equal occurrences. Unknown versions or invalid references fail.

The version specifies expansion semantics, independently of the store schema.
Existing versions retain their meaning; new runtime wording is already captured
as literal segments and does not require a format version change.

## Online execution and reconstruction

Maintain resolved messages and pending templates in one execution-local buffer.
Create templates at message-producing sites, not by comparing completed calls.
Model outputs and tool results reference their owning Step outputs; steer Parts
reference control input fields. Authored/context/repair messages and existing
legacy history selections remain literal templates in this PR.

Use one deterministic renderer with different resolvers: live values online,
durable fields during reconstruction. Online execution renders only additions;
it neither rereads nor reconstructs the saved prefix. Keep adopted values stable.
Persist the pending delta with Model Step begin before invoking the adapter.
An established but interrupted begin still contributes its delta exactly once.
Complete interrupted Step boundaries before continuing. Persist adopted outputs
before later deltas can reference them, including steer-skipped tool results.

Reconstruct deltas in numeric Step order within the owning Run, stopping at the
requested Model Step. Run/execute/retry boundaries start a fresh message sequence;
child Runs are independent. The boundary comes from existing control relations,
not a stored `previous`, `keep`, or prefix chain. Render each delta with its own
version. Batch reconstruction shares reads and expanded prefixes.

Public events continue to expose resolved ModelCalls. Recording metadata stays
internal; adapters and inspection receive ordinary Messages and Parts.
Direct record producers must supply a delta for continuations; an omitted delta
captures the supplied call's messages as literal templates for an initial call.

## Persistence and retry

Replace complete per-call message-hash lists with inline deltas and stop creating
`model_messages`. Increment the store schema; reject incompatible databases
untouched rather than migrating or keeping dual readers.

Retry continues deleting its Step suffix, including those Steps' deltas. Add no
reference snapshots, MVCC, redirects, or reference-protection policy. Replaying
surviving calls after their dependencies were deleted or overwritten is not
guaranteed; unresolved references fail explicitly.

## Implementation and acceptance

Touchpoints: execution vocabulary/record codecs, a small delta renderer, Store
capture/reconstruction, and executor message production and persistence.

- Compare captured adapter requests with reconstruction, including after restart:
  text, multimodal Parts, empty/duplicate messages, tool exchanges and grouping,
  steer, cancellation, repair, reload, execute, retry, and parallel child Runs.
- Verify reference/literal round trips, Part boundaries, unsupported versions,
  missing references, and reference-looking text/tool data remaining literal.
- Verify numeric ordering, linear delta metadata growth, long iterative reads,
  batched reconstruction, and no store reads to render an online saved prefix.
- Verify Step-begin atomicity and repeated interruptions during terminal cleanup;
  pending messages must neither disappear nor be duplicated in the next call,
  and completed ToolCalls must receive results before steer continues.
- Verify incompatible databases remain unchanged and run the default offline
  suite: Ruff, Ruff format, ty, and pytest.

The principal risks are mismatched live/durable values and incorrectly connected
execution boundaries. Exact request-equality tests are the acceptance criterion.
