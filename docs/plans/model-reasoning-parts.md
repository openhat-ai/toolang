# Preserve Model Reasoning Parts and Events

## Status

Proposed feature definition. Human approval is required before implementation.
The requested scope is adapter ingestion, canonical Parts and deltas, execution
events, and persistence. Reasoning presentation is deferred.

## Goal and Success Criteria

Preserve provider-returned reasoning text and summaries independently of answer
text and tool calls. Streaming observers receive typed reasoning events, and
completed or gracefully interrupted model Steps retain the collected reasoning
after reopening the store. Ordinary answers, tool execution, and terminal
presentation keep their existing behavior.

## Current Behavior

- `Part` has no reasoning variant; `Delta` contains text and tool-call deltas.
- Chat Completions reads only `reasoning_content`, attaching it to the first
  `ToolCallPart`; a response without tools loses this text. Vercel documents
  `reasoning` and `reasoning_details`, which this adapter does not read.
- Responses retains some native reasoning items in stateful continuation but
  ignores reasoning text and summary deltas. Messages retains thinking blocks
  associated with tool calls. Generate Content skips `thought: true` text.
- The executor persists final Step outputs and continuation. Part events are
  live observations, not a durable token-by-token journal.
- Generic output fallbacks can print unknown Parts as JSON. The terminal-output
  guard currently accepts any nonempty assistant Parts as visible output.

## Canonical Contract

Add the following types in `base/types/message.py`, including their explicit
codecs, `Part`/`Delta` unions, and literal discriminators:

| Type | Fields | Discriminator |
| --- | --- | --- |
| `ReasoningPart` | `id: str`, `text: str`, `format: Literal["text", "summary"] = "text"` | `type="reasoning"` |
| `ReasoningDelta` | `reasoning_id: str`, `text: str`, `format: Literal["text", "summary"] = "text"` | `kind="reasoning"` |

- IDs are nonempty and unique within one model Step, not globally. Adapters
  derive them from provider item/block IDs and content indices; when absent,
  use deterministic call-local ordinal IDs. Never use text content as identity.
- Keep independently identified reasoning blocks separate, including multiple
  summaries and interleaving with answers or tools. Preserve received characters
  exactly; do not trim, join with invented separators, or deduplicate repeated
  text from different blocks.
- `format` distinguishes provider-declared summaries from reasoning text. Do
  not claim summaries are complete internal reasoning. No text is inferred from
  token counts, encrypted data, signatures, or redacted blocks.
- Add optional `reasoning_id` and `reasoning_format` fields to `ModelPartStart`.
  Require them for `kind="reasoning"`; other starts retain existing behavior.
  Start, delta, and end identify the same reasoning block and format.
- Reasoning belongs to the assistant message and Model Step output even when
  that response has no tools or answer. It is not `TextPart`, a tool argument,
  or an alternative successful final answer.

## Adapter Ingestion

Both ordinary and streaming calls implement the same normalization contract.
Use existing factories and protocol modules; no new adapter or provider route.

| Adapter | Readable data to normalize |
| --- | --- |
| Chat Completions | `reasoning_content`, `reasoning`, and readable `reasoning_details` entries: `reasoning.text.text` and `reasoning.summary.summary`. |
| Responses | Reasoning item `summary[].text` and supported `content[].text`; handle `response.reasoning_summary_text.delta/done` and `response.reasoning_text.delta/done`, keyed by item and summary/content index. |
| Messages | `thinking` blocks and `thinking_delta`, identified by content-block index. |
| Generate Content | Text Parts with `thought: true`, identified by candidate/part position; preserve existing answer extraction for other text. |

Chat Completions structured readable details take precedence over their
co-emitted flat aliases; otherwise prefer nonempty `reasoning_content`, then
`reasoning`. Select one readable representation per frame, so a mirrored field
does not double the emitted text. Keep summary and text detail entries separate.
Use provider IDs/indices to append fragments and reconcile final snapshots;
do not append a complete final snapshot after already streaming its prefix.
For flat-only streams use one call-local reasoning block. If structured details
arrive later for that same flat stream, reconcile the accumulated prefix before
emitting any suffix; contradictory content fails normalization explicitly.

Emit reasoning updates as they arrive, including before the first answer or tool
delta. Final-only reasoning emits begin/end without invented delta chunks.
Late signatures and encrypted entries do not produce readable delta events.

This feature consumes data returned under the existing request configuration.
Do not automatically enable summaries, change reasoning effort/budget, or add
CLI/configuration flags. Explicit provider options enabling summaries must
survive canonical effort normalization; canonical effort still owns its field.

## Native Continuation and History

Readable Parts are for normalized records and observations; provider-native
payloads remain in adapter-owned `ModelCallResult.continuation` and durable
`ModelStepNoted.continuation`. Preserve signatures, encrypted/redacted blocks,
native IDs, formats, and block order without converting them to text.

Extend continuation to retain reasoning blocks independently of tool calls,
including final responses and opaque-only reasoning. Associate native blocks
with their owning assistant message and its reasoning IDs; identical IDs in
different messages must not collide. Use a separate call-local ID for opaque-only
blocks, retaining their message association. Keep opaque-only blocks out of `Part[]`.
Chat Completions adds native reasoning continuation for gateways, alongside the
existing protocol-specific continuation formats in the other adapters.

Outgoing adapters skip normalized reasoning as ordinary message content. For a
compatible provider/model context, replay the original native payload in the
protocol's reasoning fields exactly once. Native data takes precedence over
legacy `ToolCallPart.reasoning`; that field remains readable for old records
and existing DeepSeek replay. New responses use `ReasoningPart` as the sole
canonical readable copy. Do not fabricate signatures or convert reasoning into
an answer when replaying through another adapter.

Keep native continuation bound to the existing adapter/provider/model scope.
Prune data whose owning messages are removed by history selection or compaction;
provider changes must not reuse incompatible opaque state. Historical call
reconstruction preserves the captured Parts and continuation independently of
current setup. No new cross-provider reasoning translation is included.

## Events, Persistence, and Completion

- Track reasoning buffers and stable Part indices by reasoning ID in the model
  executor. Emit existing `PartBegin(part_type="reasoning")`,
  `PartDelta(delta=ReasoningDelta(...))`, and `PartEnd(data=ReasoningPart(...))`.
  Existing text/tool indexing stays intact. New Part indices follow first
  observation; completed output uses that same order.
- Each Part has exactly one begin and end. Final text must extend streamed
  deltas and agree with any authoritative end. Reject changed identity/format
  and inconsistent terminal content; final reconciliation never duplicates a
  Part or its end event.
- Extend typed-value registries and record codecs for `ReasoningPart` and
  `ReasoningPart[]`, including nested `Part[]`, immutable message content,
  historical call reconstruction, and JSON inspection. Use the existing tagged
  value representation; no SQL layout change or historical backfill is needed.
  New readers keep reading old Parts and legacy tool reasoning unchanged.
  Older binaries do not gain support for newly written reasoning tags.
- On graceful cancellation or failure, close active reasoning Parts with the
  collected prefix and persist them in terminal Step output. Step status remains
  canceled/failed. Preserve existing crash durability: an abrupt process death
  before Step completion does not promise persistence of in-flight deltas.
- Reuse event serialization and API SSE transport. Do not add an event table,
  per-token database writes, or a promise to replay the original chunk stream.
- Reasoning-only terminal responses still follow the empty-visible-output error
  path. Text extraction, structured-output parsing/repair, and conversion to a
  text or structured answer must ignore reasoning. Raw `Part[]` output retains
  it. Reasoning plus tools continues tool execution normally. Usage/cost
  calculations remain unchanged.

## Presentation Boundary

No reasoning panels, labels, spinners, transcript entries, or new display flags.
Existing human output must ignore reasoning Parts and deltas, including nested
and parallel progress, Flow outputs, `/output`, and JSON fallback rendering.
Only compatibility filtering needed to preserve this behavior is in scope;
existing layouts and answer/tool rendering stay unchanged. Machine-readable
inspection and SSE intentionally expose the new typed data.

## Implementation Touchpoints

- `src/toolang/base/types/message.py`, `base/types/run.py`: canonical vocabulary,
  codecs, stream identity, and text-only helpers.
- `src/toolang/plugin/adapters/{chat_completions,responses,messages,generate_content}.py`:
  ingestion, native continuation, and outgoing protocol encoding.
- `src/toolang/execution/executor/steps/model.py`, `executor/runs/agic.py`:
  stream lifecycle, partial output, reconciliation, and terminal-output guards.
- `src/toolang/execution/{types,records,values,events,store,runnables}.py` and
  `src/toolang/lang/types.py`: Part registration, durable values, historical
  reconstruction, and existing event transport contracts.
- `src/toolang/cli/common/human_values.py` and
  `cli/common/execution_progress/`: compatibility filtering only.
- Existing adapter, message, event, model-Step, history, API streaming, and
  progress tests; `docs/models.md`, `docs/plugins.md`, and `docs/api.md` for
  the implemented data/event contracts.

## Acceptance Tests

1. All four adapters normalize reasoning in streaming and ordinary responses,
   with and without tools/answers. Vercel/OpenRouter fixtures cover flat aliases,
   summary/text details, mirrored fields, and encrypted-only entries.
2. Multiple/interleaved blocks retain IDs, order, exact Unicode text, and summary
   distinction. Empty deltas, final-only text, final snapshot suffixes, late
   metadata, and mismatched prefixes have deterministic outcomes.
3. Executor observers see one begin/end per reasoning Part and ordered deltas.
   PartEnd content equals the corresponding terminal Step output Part; existing
   answer/tool events retain their lifecycle and content.
4. Complete and gracefully interrupted reasoning survives closing/reopening the
   store, typed-value round trips, content hashing, thread history, and exact
   recorded ModelCall reconstruction. Existing records remain readable.
5. Event JSON and remote SSE round-trip the new types; local and remote observers
   receive equivalent reasoning data. No durable per-token journal is created.
6. Reasoning-only output still fails as empty visible output; reasoning plus
   tools executes tools. Structured answers, text outputs, accounting, and
   reasoning-token counts keep their current semantics.
7. Native replay preserves signatures and opaque blocks, including responses
   without tools. Replay does not duplicate legacy tool reasoning or leak native
   state after provider changes, compaction, fork/rewind, or history pruning.
8. Chat/Script progress, parallel/nested runs, Flow outputs, and human result
   fallbacks render identically with or without added reasoning. JSON inspection
   contains reasoning without needing a terminal presentation feature.
9. Default offline checks pass: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest`.
   Live-provider tests remain opt-in.

## Risks and Open Questions

- Providers may return only token counts or opaque state. Those calls correctly
  have no readable reasoning Parts; old runs cannot recover missing text.
- Gateway aliases and repeated terminal snapshots can duplicate content unless
  normalization and identity tests cover them explicitly.
- Added Parts affect value registries, completion guards, and JSON fallbacks;
  adapter-only tests cannot establish end-to-end correctness.
- Reasoning increases stored content size and appears in machine-readable
  exports. Use existing record access and retention behavior; do not add raw
  provider logging or change credential handling.
- No open design questions remain. Human approval of this definition is pending.

## Protocol References

- [Vercel Chat Completions reasoning](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/reasoning)
- [OpenRouter reasoning fields and native replay](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [OpenAI reasoning summary streaming event](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_text.delta)
