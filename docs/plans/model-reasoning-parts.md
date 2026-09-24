# Preserve Model Reasoning Parts and Events

## Status and Scope

Proposed feature definition; human approval is required before implementation.
Add adapter ingestion, canonical reasoning Parts/deltas, execution events, and
persistence across the four existing protocols. Reasoning presentation, new
configuration flags, cross-provider translation, and historical backfill are out
of scope.

Success means returned reasoning survives a completed or gracefully interrupted
Model Step, store reopening, and history reconstruction. Compatible assistant
history carries its native replay data without depending on the previous run's
in-memory continuation. Answers, tool execution, and human output keep their
existing behavior.

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
| `ReasoningPart` | `text: str`, `representation: Literal["text", "summary", "unknown"] = "unknown"`, `replay: PartReplay \| None = None` | `type="reasoning"` |
| `ReasoningDelta` | `text: str` | `kind="reasoning"` |
| `PartReplay` | `adapter: str`, `provider: str`, `model: str`, `data: dict[str, object]` | Nested value, not a Part or record |

Add `ReasoningPart`/`ReasoningDelta` to the existing unions. Add the same optional
`replay` field to `TextPart` and `ToolCallPart`: Gemini signatures can belong to
those Parts. `PartReplay.data` must be JSON-compatible, with adapter-owned
protocol parsing. Its scope uses the resolved adapter name, provider key, and
model ID. Store only the native contribution needed for replay, not the whole
response envelope, credentials, or request configuration.
Message validation permits reasoning only in the assistant role.

`representation` describes what the provider returned, not the wire format or
a claim of complete internal reasoning. Use `unknown` when the protocol/model
does not establish whether text is a summary. Preserve characters exactly and
keep independently identified blocks separate. Empty text is allowed when a
Part carries opaque reasoning replay data; token counts alone create no Part.

There is no reasoning-specific ID. Add required `part: int` to
`ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` for every kind, matching the
existing execution events. The adapter assigns zero-based call-local ordinals
on first canonical observation; the final message uses that order. Provider IDs
and block indices stay inside adapters and replay data. `tool_call_id` keeps its
existing tool association role. Representation and late native metadata are
final Part properties, not mutable delta identity.

## Adapter Normalization

| Adapter | Readable projection and native ownership |
| --- | --- |
| Chat Completions | Read `reasoning_details` text/summary entries, `reasoning_content`, and `reasoning`. Structured entry types determine representation; flat aliases default to `unknown`. Preserve structured IDs, indices, formats, and encrypted entries for replay. |
| Responses | Map reasoning `summary[].text` to `summary` and supported `content[].text` to `text`. Match delta/done events by item ID and summary/content index. Retain native item metadata and encrypted content. |
| Messages | Map thinking blocks and `thinking_delta` by content-block index. Use `unknown`: the generic thinking block does not distinguish summarized from full text. Retain signatures and redacted blocks; do not infer representation from model-name prefixes. |
| Generate Content | Map `thought: true` text to `summary`. Maintain a call-local active thought block across chunks; close it on a transition to another kind or a signed boundary. Retain signatures on the actual originating Part, including normal text, tools, and empty signed text. |

Gemini chunk-local array positions are not stable identities. Consecutive thought
fragments may extend one block; thought text after an intervening answer/tool
starts another. Streaming summaries and non-streaming summaries need not have
identical segmentation or wording. Both paths must preserve the data actually
received. Never merge signed native blocks or move a signature to another Part.

Chat Completions selects an alias policy once per call, using the resolved
provider route: Vercel/OpenRouter stream structured details; DeepSeek streams
`reasoning_content`. Buffer other aliases as fallback. If the primary produces
no readable data, choose one fallback at termination using whole-response
precedence: structured readable details, `reasoning_content`, then `reasoning`.
Unknown compatible routes buffer reasoning and use that same precedence.
Buffered fallback produces final-only Parts, also on graceful interruption.
Its delayed canonical observation may follow answer/tool events. This latency
tradeoff avoids changing emitted identity, text, or representation midstream.
Opaque structured data is retained regardless of which readable alias wins;
mirrored aliases never produce duplicate readable Parts.

Reconcile a protocol's final snapshots against its accumulated deltas; emit only
an unobserved suffix and reject contradictory content. Do not compare unrelated
blocks or infer identity from equal text. Final-only Parts need no fabricated
deltas. Late signatures/encrypted data produce no readable deltas.

Consume existing request configuration. Do not automatically enable summaries
or change effort/budget. Preserve explicitly supplied summary/include options
when normalizing canonical effort, which continues to own its specific field.

## Native Replay and Record Ownership

Each Part owns only its native contribution through `replay`. A readable native
block and its normalized text may both be stored; this bounded duplication
preserves exact replay. Split multi-block native items into contributions using
their native item ID/index; store shared metadata and opaque content once on the
first contribution. Adapters reassemble contributions within the owning message,
never across messages, and validate that the complete native unit is present.
Opaque-only reasoning uses an empty `ReasoningPart`.
Replay data preserves native order and boundaries even when a flat alias is the
readable projection. A signed Gemini text/tool Part owns the native block(s)
needed to replay that Part, including its own text/arguments.

Outgoing encoders use complete, compatible replay data exactly once in assistant
history. Replay replaces the corresponding canonical wire encoding; it does not
append a duplicate text/tool block. Adapters interpret only their own payloads
and apply their protocol's replay requirements. Content transformations clear
replay; stale or incomplete retained replay fails validation instead of encoding
different text/arguments. If scope differs, omit reasoning
and native metadata while encoding ordinary Parts normally; do not send reasoning
as user/tool text or fabricate missing signatures. Remove `ToolCallPart.reasoning`
and the old tool-keyed reasoning/signature maps when implementing this contract.

Keep `ModelCallResult.continuation` and `ModelStepNoted.continuation` for transport
state such as Responses' `previous_response_id` and prefix validation. Do not put
historical reasoning payloads there. A valid cursor sends only its existing
suffix; without one, encode selected history from its Parts. Persisting Parts
must not depend on whether a response also returns tools.

Use existing Step output, tagged values, immutable message-content hashes, and
message deltas. No `ReasoningRecord`, separate table, message-ID registry, or
recovery scan is needed. Reopening, fork/rewind, and history selection retain or
drop Parts with their messages; compaction that rewrites content must discard
its replay data. Exact recorded ModelCall reconstruction remains independent of
current setup. Adding a response stores only its new native payload, not copies
of all previous reasoning in successive call/Step records.
This follows the existing [message recording](model-message-recording.md)
contract: Parts are output, selected messages are captured input, and `noted`
does not duplicate either.

Budget estimation counts each Part's replay data instead of its duplicated
canonical text/arguments when present, and canonical data otherwise. Shared
native metadata is counted only where stored. This remains a conservative byte
estimate, including opaque data; provider usage still calibrates it. Billing and
reported reasoning-token counts do not change.

## Events, Persistence, and Completion

- The executor tracks all streamed Parts by the adapter's ordinal and emits the
  existing `PartBegin`, `PartDelta`, and `PartEnd`. Each Part begins/ends once;
  streamed reasoning deltas concatenate to its text. Final-only Parts use their
  end payload. PartEnd equals terminal Step output; reconciliation must neither
  duplicate Parts nor end events. Reasoning uses `part_type="reasoning"` and
  `ReasoningDelta` within those existing events.
- On graceful failure/cancellation, close observed Parts and persist collected
  prefixes. Only provider-complete blocks may carry replay; unfinished signed
  blocks retain readable text without replay. Adapters flush buffered reasoning
  through the existing callback before propagating the error. The Step remains
  failed/canceled. Abrupt process death has no new in-flight durability promise.
- Extend Part registries, tagged-value codecs, immutable content capture, JSON
  inspection, and existing SSE serialization. `PartReplay` stays a nested value.
  Do not persist token events or promise replay of their original chunking.
- Bump the execution-store schema version (currently 47; use the next available
  version at implementation). Follow the existing strict version gate: reject
  incompatible stores before decoding or writing, without modifying them. No
  automatic migration, reset, or legacy dual-reader is included. Document this
  data-format change and the required indexed adapter stream contract together.
- Reasoning-only output follows the empty-visible-output error path. Reasoning
  plus tools still executes tools. Text extraction, structured-output parsing
  and repair, and child-run-to-user-context conversion exclude reasoning. Raw
  `Part[]` results and machine inspection retain it.
- Human progress/result paths ignore reasoning and replay metadata, including
  JSON fallbacks and nested/parallel output. Only compatibility filtering is in
  scope; no new display, transcript item, label, spinner, or flag.

## Implementation Touchpoints

- `src/toolang/base/types/{message,run}.py`: vocabulary, codecs, stream ordinals.
- `src/toolang/plugin/adapters/{chat_completions,responses,messages,generate_content}.py`:
  ingestion, native ownership, outgoing encoding, and continuation cleanup.
- `src/toolang/execution/executor/{steps/model,runs/agic,budget}.py`: lifecycle,
  completion guards, and estimates; `execution/assembly/`: message preservation
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
   recorded calls. Verify exact reasoning/replay data, including a response with
   no tools. Verify incompatible schema versions fail without changing the DB.
5. Encode a subsequent call for each protocol after reopening and fork/rewind.
   Verify native item order, redaction/encryption, Gemini text/tool/empty-text
   signatures, and no duplicate content. Test scope changes, compaction, pruning,
   and Responses cursor/full-history paths. No removed message is resurrected.
6. Over a multi-call conversation, each new stored message contributes its own
   replay data; continuation contains no accumulated reasoning history. Budget
   estimates count one representation per contribution and calibrate as before.
7. Reasoning-only output fails; tools and structured answers behave as before.
   Human Chat/Script/Flow, nested/parallel output, and JSON fallbacks remain
   unchanged; raw Parts and machine inspection expose reasoning and replay data.
8. Run the repository's default offline checks: Ruff check/format, ty, and the
   full pytest suite. Live-provider tests remain opt-in.

## Risks and Approval

Native replay requirements vary by protocol and model; unknown payloads must not
be guessed into valid signed blocks. ID-only server references still depend on
provider retention; absent encrypted state cannot be reconstructed. Native
storage increases size and exposes
provider data through existing machine-readable exports and retention rules.
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
- [OpenAI summary streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_text.delta)
- [Anthropic thinking and signatures](https://platform.claude.com/docs/en/build-with-claude/thinking)
- [Gemini Generate Content thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
- [Gemini Generate Content signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
