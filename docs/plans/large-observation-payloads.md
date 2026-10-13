# Large observation payloads

Status: proposed; implementation requires human approval. Depends on the model
observation metadata change in [PR #787](https://github.com/openhat-ai/toolang/pull/787).

## Goal and verified problem

Valid tool arguments, outputs, and completed Parts must not disconnect an
observer solely because their bodies are large. Progress and controls remain
responsive, complete content remains available, and execution records retain
their original values.

Current canonical readers reject an individual event above the 1 MiB batch
budget. Offline reproduction after #787 still produces `StreamOverflowError`
for a tool `StepBegin`, text `PartEnd`, and value `StepEnd` containing 2 MiB.
Snapshots also load complete record bodies before applying their 16 MiB budget.
`PartEnd` can precede persistence of its Step's output, so a reference to that
output alone cannot safely replace its body.

This plan supersedes the oversized-single-event overflow rule in
[local-subscriptions.md](local-subscriptions.md#cache-and-fanout). Its cursor,
publication, genuine slow-reader overflow, and structural recovery contracts
otherwise remain in force.

## Scope and budgets

Cover canonical observers, run/thread/agent HTTP streams, replacement snapshots,
and Chat/script presentation. Hub export forwards the same bounded observations
without expanding references. Retain full execution inputs, outputs, normalized
calls, and existing inspection commands. Do not add provider retries, change
run outcomes, raise retention budgets, or introduce CLI tuning flags.

Measure serialized UTF-8 event JSON, including its metadata, before admission
to the canonical cache; check the encoded transport frame as well.

| Budget | Decision |
| --- | --- |
| Inline event | At most 256 KiB; equality stays inline. Larger bodies become references. |
| Text/reasoning delta | At most 64 KiB per serialized event; emit immediately rather than waiting to fill it. |
| Reference preview | At most 4 KiB of UTF-8 text, explicitly labeled as a preview. |
| Reader batch | Retain 128 scanned events / 1 MiB. Covered payloads are bounded by projection before admission. |
| Canonical cache | Retain 4,096 events / 16 MiB. |
| Snapshot | Retain the current 16 MiB, five-second, and entity-count limits; exclude referenced bodies from reconstruction. |
| Body retrieval | At most 64 KiB per page, two concurrent requests and two pages awaiting presentation per observer. |

Splitting preserves characters, ordering, and exact concatenation, including
Unicode, JSON escapes, and reasoning text. Each delta fragment receives its own
canonical cursor. References replace bodies; they do not silently drop or
truncate the stored original. Genuine retention exhaustion or excessive
structural entity counts can still require recovery.

## Observation and persistence

Add observation-only payload references containing a `ContentRef`, exact byte
length, media type, and optional bounded preview. Store structured/binary Parts
as canonical UTF-8 JSON in the existing content-addressed `contents` storage;
preserve all typed Part fields and provider metadata inside it. Store visible
text as raw UTF-8 so pages can render without assembling a giant JSON string;
retain the enclosing Part type and Output binding in the descriptor. References are
immutable and never point to a mutable Step field at a reused retry path.

- Oversized tool Begins carry identity, plugin, trigger, summary, and an argument
  reference. Preserve the small runtime-operation facts required for existing
  `run`, `exec`, `spawn`, `await`, and asynchronous progress layouts.
- Oversized Step/Run Ends retain status, binding/type information, accounting,
  control relations, and an output reference. Keep completion independent of
  body retrieval. Large error text follows the same preview/reference rule.
- Oversized `PartEnd` retains Step/Part identity and original Part type with a
  body reference. Text already delivered through deltas is not retransmitted on
  the progress channel. Binary/structured Parts use references, not text chunks.

Represent multi-Part outputs as ordered Part descriptors, with a paged immutable
manifest if the descriptor list itself exceeds the inline budget. Keep full
authored Flow statement transmission outside this change.

Full internal events reach persistence before observation projection. For
disposable Parts, persist a referenced body before publishing its descriptor,
without marking the Step complete early. Content preparation must not hold the
agent-wide publication gate across encoding a large body or network I/O; commit
the prepared body, its ownership, and required record changes before making the
descriptor visible. No observer callback participates in that transaction.

Persist the compact current Begin/End projection alongside durable records in
an `observation_records` table keyed by entity reference and boundary, with
root identity, source cursor, and bounded event data. This is current structural
state, not an additional event journal. Retry/rewind invalidation updates it in
the same transaction as the corresponding records and cursor changes. Snapshots
read these projections rather than `SELECT *` followed by expansion of large
`given` or `output` fields.

Add an ownership association between referenced content and its root Run.
Writable migration builds missing compact projections before stream admission,
in bounded batches, without reconstructing model calls. Preserve existing
records and cursors. Explicit root deletion removes its ownership/projection
rows; cache eviction, disconnect, runtime restart, and retry do not invalidate
already-issued immutable content references. Content reclamation remains a
separate store concern; add no expiry policy in this change.

## Retrieval, ordering, and UX

Provide `GET /api/v1/runs/{run_id}/observation-content/{content_ref}` with byte
`offset` and `limit` query parameters; default and maximum limit are 64 KiB.
Validate run ownership, canonical reference syntax, and nonnegative bounds.
Return raw bytes with immutable digest and total-length metadata, and reject
missing/unowned content with 404. Read bounded SQLite BLOB slices without
loading the full value. Local observers use the equivalent store reader. Never
accept an arbitrary URL or filesystem path from a payload descriptor.

The receiver commits metadata/cursors independently of body downloads. Snapshot
replacement commits at its existing checkpoint without waiting for content.
Body hydration is a separate presentation update, not a second Begin/End or
another metrics contribution. Apply downloads only to the matching owner,
incarnation cursor, and content digest; discard stale responses after retry,
rewind, replacement, or closing the view.

Tool progress continues to show its existing summary; complete arguments and
results remain available through existing inspection/output actions. Model and
final text output loads automatically into its original transcript position in
bounded presentation increments, without requiring `/output` to repair it.
Preserve already displayed text and scroll position, append only missing text,
and keep later run progress and composer input usable while a body loads.
`RunEnd` records completion immediately; an in-place loading indicator may remain
until the referenced answer has finished loading. Preserve signed reasoning and
other opaque Part metadata exactly without rendering them as ordinary text.

Decode text pages incrementally across UTF-8 boundaries. Spool structured/binary
JSON pages until complete before decoding their typed value; do not eagerly
hydrate them for progress summaries. If retrieval fails, leave the original reference/preview visible and offer a
retry plus the existing inspection path. Report a content-loading failure rather
than a connection loss or run failure. Resume partial downloads from their byte
offset; verify the complete digest before treating the body as complete. An
unavailable runtime can delay content retrieval but must not erase progress.
Hub does not mirror blobs or add a content proxy in this scope; complete content
is retrieved from the originating Agent API or local inspection.

## Implementation touchpoints

- `execution/types.py`, `events.py`, and `schemas.py`: bounded observation
  vocabulary, codecs, and frame budgets; retain complete internal types.
- `execution/store.py`, `executor/_persist.py`, and `executor/executor.py`:
  content/projection ownership, migration, atomic publication, and delta splits.
- `execution/observation.py`, `subscriptions.py`, `stream.py`, and
  `stream_client.py`: compact prefill, bounded caching, cursor and hydration state.
- `api/routers/runs.py`, `execution/remote.py`, and local `TraceObserver`:
  paged content access and independent retrieval scheduling. Terminal-state
  recovery reads compact run/output descriptors rather than expanding all calls.
- `cli/common/execution_progress/`, `cli/toolang/commands/chat/`, and script
  presenters: reference-aware summaries and incremental transcript hydration.
- `teaming/` observation codecs/exporter: preserve projected fields and budgets.
- Focused tests in `tests/unit/execution/`, `tests/integration/execution/`,
  `tests/integration/api/`, and `tests/unit/cli/`; document the protocol and update
  `CHANGELOG.md` through `too aide.too update_changelog` during implementation.

## Acceptance tests

1. Boundary-sized Unicode/escaped payloads stay within serialized frame limits;
   above-threshold bodies produce valid references and exact inspection values.
2. A tool argument, result, text Part, and structured/binary Part above 1 MiB
   each complete without size-driven overflow, false disconnection, or altered
   tool invocation. Include a 20 MiB body above the total cache/snapshot budget.
3. A giant single provider text/reasoning delta becomes ordered bounded fragments;
   streaming and non-streaming responses render the complete text exactly once.
4. Pause between `PartEnd` and `StepEnd`: the issued reference is already readable,
   and the Step remains running. Cancellation retains the existing outcome rules.
5. Run/thread/agent replacements containing large existing outputs fit their
   budgets without reading body BLOBs or rebuilding calls; checkpoint and source
   cursor behavior matches live/cached replay. Test migrated records too.
6. Body download delay/failure does not delay another run's Begin/End, controls,
   terminal status, or composer input. Resume fetches without duplicate text or
   lost previews; retrieval failure never changes the run outcome.
7. Retry at a reused Step path, rewind, restart, and out-of-order fetch completion
   cannot inject an old body into a new incarnation or duplicate metrics.
8. Paging validates ownership/limits and bounded reads; malformed lengths,
   incomplete bytes, and digest mismatch produce explicit content errors.
9. Local tracers, remote Chat, script output, and Hub metadata observe equivalent
   structure. Complete calls/results remain inspectable after cache eviction and
   runtime restart. Existing slow-reader overflow tests continue to pass.
10. The default offline verification passes, with deterministic tests and no live
    provider traffic.

## Compatibility, risks, and open questions

Reference-bearing events are a protocol change: update runtimes, CLI clients,
and observer integrations together. New decoders accept previous inline events;
old clients cannot consume the new descriptors. Preserve public command names
and document complete-content access and this migration in Unreleased.

Derived projections and body ownership require a store migration and additional
disk space. Migration must preserve historical inspection and retry boundaries;
body encoding must not lengthen the publication critical section. Asynchronous
hydration introduces ordering and rendering risks covered above. The current
whole-Run detail endpoint must not become the status/content polling shortcut.

No unresolved product decisions remain in this proposal. Human approval of the
budgets, protocol/store migration, and automatic text loading is pending.
