# Record model messages

## Goal and scope

Approved implementation definition, incorporating the decisions in
[model input protocol](model-input-protocol.md). Preserve exactly the normalized
adapter request while recording ordinary continuations as incremental messages.
Keep roles, Part boundaries, tool grouping, authorization, and output contracts.

## Durable format

Each Model Step stores a call-level format version and explicit message head:

```json
{
  "model": "provider/model",
  "call": {
    "version": 1,
    "instructions": "sha256_...",
    "messages": {
      "head": "run_ab12.0",
      "delta": [
        {
          "role": "user",
          "content": {"segments": [{"hash": "sha256_..."}]},
          "tag": "skill-guidance",
          "recall": {"ref": "skill/testing", "revision": "64 lowercase hex digits"}
        }
      ]
    },
    "tools": null,
    "output_schema": null,
    "cont": null,
    "max_output_tokens": null
  }
}
```

Delta is an ordered message array. Each message has role and content, with
optional tag and recall metadata. Content owns its segments: literal text,
canonical Parts, typed field references, or content hashes. Commit freezes the
actual assembled Parts as immutable content hashes; it does not resolve source
fields again. Preserve empty messages, duplicate occurrences, adjacent text
boundaries, multimodal Parts, and nested tool data exactly.

Recall is direct ref/revision metadata, not a pointer to a control. Visibility is
keyed by tag/ref: skill-trigger and skill-guidance share skill/testing without
satisfying each other's visibility. Revision zero is a tombstone; XML uses
removed="true". Withdrawal appends a new message and never rewrites prior calls.

Imported historical messages additionally carry source, the owning Run ref.
This marks reused context, not capability origin. History selection excludes
imported context from a Run's new contribution, including after rebasing.

## Assembly and persistence

Assembly consumes adopted State, setup, agic, bound input, and executor-supplied
history/control facts. It returns adapter components and recording metadata
without persistence callbacks or writes. State owns capability merging and
shadowing; model-facing refs contain no source scope.

Executor stages resolved messages and recording descriptions together. It
commits immutable content dependencies and Model Step begin atomically, then
adopts the staged buffer and dispatches the call. An established but interrupted
begin contributes its delta once; failed preparation changes no visible recall.
Public message serialization omits runtime metadata.

## Sequence and reconstruction

Head references a Model Step in the same Run. A self-head stores the complete
selected baseline. Continuations retain that head and store only additions.
Compaction, changed history selection, or a new execution sequence starts a new
self-head while preserving the current conversation.

Reconstruct by concatenating raw deltas from head through the requested call in
numeric Model Step order. Never recursively concatenate expanded calls. Batch
reads share resolved content. Head validation rejects missing, future,
cross-Run, and disconnected sequences.

Instructions, tools, output_schema, continuation, and token budget come from the
current row, not the head. Far/near is agic policy, absent from the durable call.
Record its selected messages explicitly; replay neither reselects history nor
reads State, controls, current templates, or deleted source fields.

Retry deletes its Step suffix as before. Surviving calls retain captured content
even if referenced execution fields are later removed or reused. Missing or
corrupt content hashes fail explicitly. Upgrade the store schema and reject
incompatible databases unchanged; no migration or dual reader.

## Acceptance

- Exact adapter/replay equality after restart: text, multimodal Parts, empty and
  duplicate messages, grouped tools, steer, cancel, repair, reload, execute,
  retry, and child Runs.
- Direct metadata survives replay; trigger/guidance, rules/workspaces, removal,
  replacement, restoration, and pending versus presented visibility stay distinct.
- Initial baselines, ordinary continuations, repeated compaction, history-policy
  changes, fork/rewind, numeric ordering, and nonduplicated history.
- Call-level version rejection, malformed records, invalid heads, content hash
  integrity, and reference-looking text remaining literal.
- Linear delta storage, shared batched expansion, no online reread of saved
  message bodies, and no State or control lookup during replay.
- Atomic Step begin and interruption recovery; all default offline checks pass.
