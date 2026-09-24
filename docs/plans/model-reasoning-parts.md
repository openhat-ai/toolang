# Preserve Model Reasoning Parts and Events

## Status and Scope

Proposed feature definition; human approval is required before implementation.
Add adapter ingestion, canonical reasoning Parts/deltas, execution events, and
persistence across the four existing protocols. Reasoning presentation, new
configuration flags, cross-provider translation, and historical backfill are out
of scope.

Success means returned reasoning survives a completed or gracefully interrupted
Model Step, store reopening, and history reconstruction. Compatible assistant
history carries its signatures and native metadata without depending on the
previous run's in-memory continuation. Answers, tool execution, and human output
keep their existing behavior.

## Current Behavior

`Part` and `Delta` have no reasoning variants. Chat Completions retains only
`reasoning_content` on the first tool call. Responses, Messages, and Generate
Content retain some native reasoning/signatures in tool-associated continuation;
readable reasoning is otherwise discarded. A new Agic run starts without that
continuation. Part events are live observations; only terminal Step output is
durable. Generic output fallbacks can render unknown Parts as JSON.

## Canonical Vocabulary

Define these values and explicit codecs in `base/types/message.py`:

| Type | Fields | Discriminator |
| --- | --- | --- |
| `ReasoningPart` | `text: str`, `signature: str \| None = None` | `type="reasoning"` |
| `ReasoningDelta` | `text: str` | `kind="reasoning"` |

Add these types to the existing unions. `signature` directly carries an opaque
provider signature or encrypted reasoning state; never decode it or treat it as
readable text. Add the same optional field to `TextPart` and `ToolCallPart`, since
Gemini signatures may belong to those Parts. There is no separate replay value.

These three Part types also have `provider: str | None = None` and
`provider_metadata: dict[str, object]` (empty by default). `provider` is the
resolved provider key. Nonempty metadata records `adapter` and `model` plus only
protocol fields needed for round trips, such as native IDs, indices, formats,
and block type. Adapters populate the origin for generated reasoning even when
there is no signature. A signature requires that origin; metadata is JSON-only,
parsed by its owning adapter. Do not copy canonical text/arguments, signatures,
whole response envelopes, credentials, or request options into metadata.
Reasoning is allowed only in assistant messages; native fields are only sent
for assistant history with a matching adapter/provider/model scope.

`text` preserves the readable reasoning returned by the provider, including
summaries, without classifying it or claiming complete internal reasoning.
Native block types and field names needed for reconstruction stay in provider
metadata. Preserve characters exactly and keep independently identified blocks
separate. Empty text is allowed when a Part carries a signature or native
reasoning metadata; token counts alone create no Part. Empty signed normal text
remains a `TextPart` in its original position.

There is no reasoning-specific ID. Add required `part: int` to
`ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` for every kind, matching the
existing execution events. The adapter assigns zero-based call-local ordinals
on first canonical observation; the final message uses that order. Provider IDs
and block indices stay inside adapters and provider metadata. `tool_call_id`
keeps its existing tool association role. Late native metadata is attached to
the final Part and does not change delta identity.

## Adapter Normalization

| Adapter | Readable projection and native ownership |
| --- | --- |
| Chat Completions | Read `reasoning_details` text/summary entries, `reasoning_content`, and `reasoning` into `ReasoningPart.text`. Put a detail's signature or encrypted `data` in `signature`; keep its type, ID, index, and format in provider metadata. Flat fields need no signature; record their field name to reconstruct them from `text`. |
| Responses | Read reasoning `summary[].text` and supported `content[].text` into `ReasoningPart.text`. Match delta/done events by item ID and summary/content index. Map `encrypted_content` to `signature`; retain item identity, status, native content types, source field, and indices in provider metadata. |
| Messages | Read thinking blocks and `thinking_delta` into `ReasoningPart.text`, tracking content-block index. Map `thinking.signature` to `signature`. A redacted block becomes an empty ReasoningPart with its `data` in `signature` and native block type in metadata. |
| Generate Content | Read `thought: true` text into `ReasoningPart.text`. Maintain a call-local active thought block across chunks; close it on a transition to another kind or a signed boundary. Map `thoughtSignature` to `signature` on its actual Part, including normal text, tools, and empty signed text. |

Gemini chunk-local array positions are not stable identities. Consecutive thought
fragments may extend one block; thought text after an intervening answer/tool
starts another. Streaming summaries and non-streaming summaries need not have
identical segmentation or wording. Both paths must preserve the data actually
received. Preserve signed Part boundaries, including adjacent normal text;
never merge independently signed blocks or move a signature to another Part.

Chat Completions selects an alias policy once per call, using the resolved
provider route: Vercel/OpenRouter stream structured details; DeepSeek streams
`reasoning_content`. Buffer other aliases as fallback. If the primary produces
no readable data, choose one fallback at termination using whole-response
precedence: structured readable details, `reasoning_content`, then `reasoning`.
Unknown compatible routes buffer reasoning and use that same precedence.
Buffered fallback produces final-only Parts, also on graceful interruption.
Its delayed canonical observation may follow answer/tool events. This latency
tradeoff avoids changing emitted identity or text midstream.
Opaque structured data is retained regardless of which readable alias wins;
mirrored aliases never produce duplicate readable Parts.

Reconcile a protocol's final snapshots against its accumulated deltas; emit only
an unobserved suffix and reject contradictory content. Do not compare unrelated
blocks or infer identity from equal text. Final-only Parts need no fabricated
deltas. `ReasoningDelta` remains text-only. Adapters assemble signature fragments
internally and attach the complete value at PartEnd; there is no signature delta
event in the canonical contract.

Consume existing request configuration. Do not automatically enable summaries
or change effort/budget. Preserve explicitly supplied summary/include options
when normalizing canonical effort, which continues to own its specific field.

## Signatures, Continuation, and Record Ownership

Each Part owns its text/arguments, signature, and necessary provider metadata.
Reconstruct the native block from these fields instead of storing a second raw
block. When one native reasoning item contains multiple readable blocks, keep
their native item ID and indices in metadata, and store its shared signature and
item-level fields once on the first Part. Adapters assemble the complete item
within its owning message; never group identical native IDs across messages.
Opaque-only reasoning remains an empty `ReasoningPart`. Preserve native order
using metadata even when flat-alias fallback delays canonical observation.

Outgoing encoders interpret only compatible native fields and emit each block
once. Required fields and group completeness are validated by the adapter;
signatures are opaque and receive no local cryptographic validation. Content
transformations clear signatures, provider origin, and metadata. If scope
differs, omit reasoning and native fields while encoding ordinary Parts normally; do not send
reasoning as user/tool text or fabricate missing signatures. Remove
`ToolCallPart.reasoning` and the old tool-keyed reasoning/signature maps when
implementing this contract.

Keep the existing `ModelContinuation` vocabulary and
`ModelCallResult.continuation`/`ModelStepNoted.continuation` for call-level state,
such as Responses' `previous_response_id` and prefix validation. Per-Part native
fields must not be duplicated there. A valid cursor sends only its existing
suffix; without one, encode selected history from its Parts. Persisting Parts
must not depend on whether a response also returns tools.

Use existing Step output, tagged values, immutable message-content hashes, and
message deltas. No `ReasoningRecord`, separate table, message-ID registry, or
recovery scan is needed. Reopening, fork/rewind, and history selection retain or
drop Parts with their messages; compaction that rewrites content must discard
its signatures and metadata. Exact recorded ModelCall reconstruction remains
independent of current setup. Adding a response stores its new Parts, not copies
of all previous reasoning in successive call/Step records.
This follows the existing [message recording](model-message-recording.md)
contract: Parts are output, selected messages are captured input, and `noted`
does not duplicate either.

Existing budget estimation counts serialized Parts, including text, signatures,
and metadata, once. There is no second native text copy to discount or separate
signature estimate to add. Provider usage still calibrates the conservative byte
estimate. Billing and reported reasoning-token counts do not change.

## Events, Persistence, and Completion

- The executor tracks all streamed Parts by the adapter's ordinal and emits the
  existing `PartBegin`, `PartDelta`, and `PartEnd`. Each Part begins/ends once;
  streamed reasoning deltas concatenate to its text. Final-only Parts use their
  end payload. PartEnd equals terminal Step output; reconciliation must neither
  duplicate Parts nor end events. Reasoning uses `part_type="reasoning"` and
  `ReasoningDelta` within those existing events.
- On graceful failure/cancellation, close observed Parts and persist collected
  prefixes. Only provider-complete native units retain signatures and metadata;
  unfinished units retain text with `signature=None`, `provider=None`, and empty
  metadata. Adapters flush buffered reasoning through the existing callback
  before propagating the error. The Step remains failed/canceled. Abrupt process
  death has no new in-flight durability promise.
- Extend Part registries, tagged-value codecs, immutable content capture, JSON
  inspection, and existing SSE serialization for the direct Part fields.
  Do not persist token events or promise replay of their original chunking.
- Bump the execution-store schema version (currently 47; use the next available
  version at implementation). Follow the existing strict version gate: reject
  incompatible stores before decoding or writing, without modifying them. No
  automatic migration, reset, or legacy dual-reader is included. Document this
  data-format change and the required indexed adapter stream contract together.
- Reasoning-only or empty signed-text output follows the empty-visible-output
  error path. Reasoning plus tools still executes tools. Text extraction,
  structured-output parsing and repair, and child-run-to-user-context conversion
  exclude reasoning. Raw
  `Part[]` results and machine inspection retain it.
- Human progress/result paths ignore reasoning, signatures, and provider metadata,
  including JSON fallbacks and nested/parallel output. Only compatibility
  filtering is in scope; no new display, transcript item, label, spinner, or flag.

## Implementation Touchpoints

- `src/toolang/base/types/{message,run}.py`: vocabulary, codecs, stream ordinals.
- `src/toolang/plugin/adapters/{chat_completions,responses,messages,generate_content}.py`:
  ingestion, native ownership, outgoing encoding, and continuation cleanup.
- `src/toolang/execution/executor/{steps/model,runs/agic}.py`: lifecycle and
  completion guards; `execution/assembly/`: message preservation
  and child-result conversion.
- `src/toolang/execution/{types,records,values,events,store,runnables}.py` and
  `src/toolang/lang/types.py`: registration, schema gate, capture, reconstruction.
- `src/toolang/cli/common/human_values.py`, `cli/common/execution_progress/`:
  compatibility filters only. Existing adapter, Step, store, event/SSE, budget,
  and rendering tests; `docs/{models,plugins,api}.md` for public contracts.

## Acceptance Tests

1. All four adapters cover ordinary/streaming calls, with/without tools, multiple
   and interleaved blocks, Unicode, summaries, opaque-only and final-only data.
   Usage-only reasoning creates no Part. Gemini repeated chunk positions and
   thought/answer/thought sequences must not collide.
2. Gateway aliases cover mirrored fields, flat-before-structured arrival, late
   encryption, fallback, interruption, and unsupported routes. Assert one chosen
   readable source and preserved native data; contradictory terminal snapshots
   fail. Explicit summary/include settings survive effort normalization.
3. All Part kinds have stable ordinals and one begin/end, including interleaved
   tools and graceful failure. PartEnd equals persisted output; no incomplete
   signature is replayed. Local and remote SSE round-trip the same typed values.
4. Close/reopen the store, start a new run without continuation, and reconstruct
   recorded calls. Verify exact text, signatures, and metadata, including a
   response with no tools. Verify incompatible schema versions fail without
   changing the DB.
5. Encode a subsequent call for each protocol after reopening and fork/rewind.
   Verify native item order, summary/content types and source fields,
   redaction/encryption, Gemini text/tool/empty-text signatures, distinct adjacent
   signed Parts, and no duplicate content. Test scope changes, compaction,
   pruning, and Responses cursor/full-history paths. No removed message is
   resurrected.
6. Over a multi-call conversation, each new stored message contributes its own
   signatures/metadata; continuation contains no accumulated reasoning history.
   Assert metadata does not duplicate canonical text or signatures. Existing
   budget estimates count the serialized fields once and calibrate as before.
7. Reasoning-only and empty signed-text output fail; tools and structured answers
   behave as before. Human Chat/Script/Flow, nested/parallel output, and JSON fallbacks remain
   unchanged; raw Parts and machine inspection expose reasoning and native fields.
8. Run the repository's default offline checks: Ruff check/format, ty, and the
   full pytest suite. Live-provider tests remain opt-in.

## Risks and Approval

Native replay requirements vary by protocol and model; unknown payloads must not
be guessed into valid signed blocks. ID-only server references still depend on
provider retention; absent encrypted state cannot be reconstructed. Signatures
increase size and appear in existing machine-readable exports and retention rules.
Buffered alias fallback trades streaming latency for unambiguous normalization.
The schema bump deliberately requires a compatible runtime/store pair; this
plan does not authorize changing or resetting existing user databases.

No open design choices remain. Approval is still pending; no implementation or
presentation change is included in this definition.

## Protocol References

- [Vercel Chat Completions reasoning](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/reasoning)
- [OpenRouter reasoning and replay](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [OpenAI encrypted reasoning items](https://developers.openai.com/api/docs/guides/migrate-to-responses#4-decide-when-to-use-statefulness)
- [OpenAI summary streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_text.delta)
- [Anthropic thinking and signatures](https://platform.claude.com/docs/en/build-with-claude/thinking)
- [Gemini Generate Content thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
- [Gemini Generate Content signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)

## Implementation References

Pydantic AI's [ThinkingPart](https://github.com/pydantic/pydantic-ai/blob/f8a5fe56ff6978ee33aaac32e23b88fb93258d4a/pydantic_ai_slim/pydantic_ai/messages.py#L2188)
is the precedent for a direct optional `signature`, including encrypted reasoning
state. AI SDK's [reasoning Part](https://github.com/vercel/ai/blob/40231b6222a41f89987dd35fe63825598aef453b/packages/provider/src/language-model/v4/language-model-v4-reasoning.ts)
and [history conversion](https://github.com/vercel/ai/blob/40231b6222a41f89987dd35fe63825598aef453b/packages/ai/src/ui/convert-to-model-messages.ts#L208)
illustrate Part-owned provider metadata. These inform vocabulary and ownership;
Toolang keeps its existing records, events, and continuation contracts.
Like these types and LangChain's [ReasoningContentBlock](https://github.com/langchain-ai/langchain/blob/7622d3dce760ac4be6d9aef4c653277e06064aea/libs/core/langchain_core/messages/content.py#L456),
`ReasoningPart` uses one readable text field without a generic text/summary
classification.
