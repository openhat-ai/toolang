# Large observation payloads

Status: proposed; implementation requires human approval. Depends on the model
observation metadata change in [PR #787](https://github.com/openhat-ai/toolang/pull/787).

## Goal and verified problem

Tool arguments, results, and completed Parts must not disconnect observers solely
because their bodies are large. Preserve progress, controls, complete execution
records, and complete user-visible answers.

Offline reproduction after #787 still overflows a canonical reader for a tool
`StepBegin`, text `PartEnd`, and value `StepEnd` containing 2 MiB. Snapshots also
read complete record bodies before applying their 16 MiB budget. A `PartEnd`
can precede its Step's persisted output, so visible-content references must be
readable before publication rather than depending on that later output.

This supersedes the oversized-single-event overflow rule in
[local-subscriptions.md](local-subscriptions.md#cache-and-fanout). Keep its cursor,
publication, genuine slow-reader overflow, and recovery contracts.

## Scope and budgets

Cover canonical observers, run/thread/agent HTTP streams, replacement snapshots,
and Chat/script presentation. Hub export forwards the same bounded observations.
Keep execution semantics, inspection commands, and provider requests unchanged.
No new tool-output viewer, provider retry policy, CLI tuning flags, or general
Flow statement projection is included.

Measure serialized UTF-8 JSON including metadata, and check encoded wire frames.

| Content or budget | Decision |
| --- | --- |
| Tool arguments/result data | Always `null` in observations, regardless of size; no previews or automatic retrieval. |
| Inline visible event | At most 256 KiB; larger visible content uses an immutable reference. |
| Text/reasoning delta | At most 64 KiB per encoded event, sent immediately without waiting to fill it. |
| Reader batch/cache | Keep 128 scanned events / 1 MiB and 4,096 cached events / 16 MiB. |
| Snapshot | Keep the 16 MiB, five-second, and entity-count limits; never expand omitted/referenced bodies. |
| Visible-content retrieval | At most 64 KiB per page, two concurrent requests and two queued pages per observer. |

Fragments preserve Unicode, JSON escapes, ordering, and exact concatenation;
each receives its own canonical cursor. Genuine retention or structural-count
exhaustion still requires recovery.

## Tool observation envelopes

Keep tool-result identity (`type`, tool/call IDs, name, family), status, existing
summary, and error information. Set the argument/result body slots to `null` in
an observation-only representation, with `data_omitted: true` distinguishing an
unsent body from an actual null execution value. Do not change the internal
`ToolCall` or `ToolResultPart` types: their input/output objects remain complete.

Progress needs a few result facts despite not displaying tool output. Extract
`cwd`, run/thread IDs, asynchronous-operation facts, and whether a handoff control
was applied into bounded observation metadata. Preserve the existing workspace
updates, spawn/run identity rows, exec handoffs, and failure rows. Never fetch a
result body to recover these facts. Large failure text follows the visible-content
reference policy; failure status remains available immediately.

Apply the same omission to tool arguments in model `ToolCallPart` observations
and `ToolCallDelta.text`, tool `PartEnd` results, tool `StepEnd` outputs,
and recovery snapshots. Keep argument-delta identity/lifecycle but send no argument
fragment text. A Run result
containing a tool-result Part also retains only its envelope in the observation
stream. Complete tool parameters/results remain available through existing
`inspect STEP call --json` and output-field inspection after persistence.

Chat/script progress neither displays nor downloads these bodies. Do not create
extra content blobs, download queues, or loading indicators for omitted tool
arguments/results. Internal persistence and the messages sent to models keep the
original data byte-for-byte. Observation codecs explicitly accept the nullable
body representation; do not silently turn it into an empty result object.

## Visible content and persistence

Model text and final user-visible output are never replaced by null or truncated.
Split large deltas before cache admission. Large completed text uses a
`ContentRef`, exact byte length, and media type, with Part identity and Output
binding preserved. Store visible text as raw UTF-8 in existing content-addressed
storage so pages render incrementally. Structured/binary Parts preserve their
complete canonical JSON and opaque metadata; they are fetched only when needed
for a visible result or explicit inspection. Multi-Part results use an ordered
manifest, itself paged if too large. No preview text is required.

Full internal events reach persistence before observation projection. Prepare
large visible bodies outside the agent-wide publication gate, then commit body,
root ownership, and required record changes before publishing references. Do not
mark a Step complete early or await observer/network work inside this transaction.
Tool `PartEnd` bodies need no premature storage merely for progress omission.

Persist bounded current Begin/End projections in `observation_records`, keyed by
entity/boundary with root, cursor, and event data. This is current structural
state, not an event journal. Retry/rewind invalidation updates it atomically with
records and cursors. Snapshots read these projections instead of loading full
`given`/`output` columns and then cutting them down.

Writable migration builds missing projections in bounded batches before stream
admission, without expanding model calls. Preserve records/cursors and associate
visible-content references with their root. Explicit root deletion removes its
projection/ownership rows; cache eviction, retry, disconnect, and restart do not
invalidate immutable body references. Add no content expiry or reclamation policy.

## Loading and UX

Add `GET /api/v1/runs/{run_id}/observation-content/{content_ref}` with nonnegative
byte `offset` and `limit` (default/maximum 64 KiB). Return BLOB slices, digest, and
total length without loading the full BLOB; reject missing/unowned references
with 404. Local observers use the equivalent store reader. Do not fetch arbitrary
URLs/paths from descriptors. Hub does not mirror blobs or add a proxy.

Commit progress metadata/cursors and snapshot checkpoints independently of
visible-content loading. Apply content as a presentation update, not a repeated
Begin/End or metrics contribution. Match owner, incarnation cursor, and digest;
discard stale responses after retry, rewind, replacement, or view closure.

Automatically append missing answer text in its original transcript position,
in bounded increments, preserving existing text and scroll position. Keep composer,
controls, and other run progress responsive. Mark the run complete immediately;
only a visible answer may show an in-place loading indicator. Do not reprint text
already received in deltas. Decode UTF-8 across page boundaries; spool structured
JSON until complete before typed decoding and preserve signed reasoning metadata.

Loading failure keeps progress intact and offers retry plus existing inspection.
Resume at the downloaded offset and verify the full digest before marking content
complete. It must not turn a successful run into a failure or a tool result into
an apparent empty execution result. Terminal polling must read compact status and
output descriptors rather than reconstructing every historical model call.

## Touchpoints and acceptance

Likely files: `execution/{types,events,schemas,store,stream,stream_client,
observation,subscriptions,remote}.py`, `execution/executor/{executor,_persist}.py`,
`api/routers/runs.py`, local `TraceObserver`, Chat/script presenters,
`cli/common/execution_progress/`, and teaming observation codecs/exporter.
Keep source event parsing and store ownership in their existing packages.
Update protocol docs and generate Unreleased through the changelog runnable
when implementing. Tests remain offline and deterministic.

1. Tool argument/result bodies of both 1 KiB and 20 MiB become null observations
   with omission markers; real invocations, persisted results, and model messages
   retain exact complete data. Cover giant streamed argument fragments as well
   as completed arguments. Progress performs zero body requests.
2. Workspace updates, spawn/run IDs, exec control handoffs, failures, and ordinary
   tool summaries remain correct with omitted bodies, including large unrelated
   fields in the same result.
3. Unicode/escaped frame boundaries stay within budgets. Giant single text and
   reasoning deltas split exactly; streaming/non-streaming answers above 16 MiB
   render completely once without size-driven overflow or disconnection.
4. Pause before StepEnd: a visible PartEnd reference is already readable while the
   Step remains running; an omitted tool result causes no retrieval. Preserve
   cancellation/persistence ordering.
5. Run/thread/agent replacement snapshots with large existing bodies fit budgets
   without body BLOB reads or call reconstruction. Test migrated records, cache
   loss, checkpoint behavior, and terminal-state polling.
6. Delayed/failed downloads cannot block input, controls, or another run's progress.
   Retry/resume, reused Step paths, rewind, restart, and out-of-order completion
   cannot duplicate text, metrics, or inject an old answer into a new incarnation.
7. Validate paging/ownership, missing bytes, malformed lengths, and digest errors.
   Complete inspection remains available after eviction/restart. Local, remote,
   script, and Hub structural observations agree; slow-reader checks still pass.
8. Default Ruff, formatting, type checks, and offline suite pass.

## Compatibility, risks, and open questions

Nullable tool bodies and visible-content descriptors change the observation
protocol. Update runtimes/clients together; new decoders accept legacy inline
events. Retain public commands, and document the omission marker and full-content
inspection path in Unreleased.

Derived projections/visible-body ownership require migration and additional disk
space; tools need no duplicate body storage. Preserve historical inspection and
retry boundaries, keep encoding outside the publication gate, and test late body
updates against transcript layout. No unresolved product decisions remain in this
proposal; human approval of the complete definition is pending.
