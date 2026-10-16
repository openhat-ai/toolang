# Talk cursor storage

Status: approved on 2026-10-13. This document records the final agreed scope.
The cursor contract remains current; admission, durable batch receipts, and
thread reuse now follow [conversation dispatch](conversation-dispatch-and-rules.md).

## Final goals

1. Persist talk-handler progress in the backend so restart/relocation resumes
   without local cursor files.
2. Use `P:agent:{name}:talk` as an agent-owned Hash, mapping conversation IDs to
   full Stream-ID cursors (`P = too:teaming:v1`). Only talk handlers advance it.
3. Use the term `cursor` and two agent-only Hub APIs through the existing backend
   interface/connection, without another store abstraction.
4. Read current batches from the conversation stream, query history through
   `convo.peek`, and retain diagnostics in execution records. Remove duplicated
   message/result/receipt state and the `previous` input field.
5. Rename the member directory to `P:team:members` and its constant to
   `TEAM_MEMBERS`, preserving member records and other keys.
6. Remove obsolete-format detection and local cursor I/O, including
   `AgentLayout.channel_room()`. Generate no `.runtime/channels/` directories/files.
7. Preserve current-data integrity, skip behavior, bounded save recovery, and
   cancellation; retries must not rerun handlers or mix backends.

## Cursor contract

Missing cursor fields read as `0-0`. Cursors have no TTL and survive shutdown,
disabled talk, departure/rejoining, and agent-name recreation. A cursor means
processed or skipped through that position, rather than guaranteed success.

| Interface | Contract |
| --- | --- |
| Backend `talk_cursor(agent, convo_id)` | Read the agent's cursor. |
| Backend `advance_talk_cursor(agent, convo_id, cursor, *, token)` | Atomically enforce a live matching lease, membership, and monotonic progress no later than the stream tail; equal writes are idempotent. |
| `GET /agents/{agent}/talk/cursors/{convo_id}` | Check lease/membership and return `{"cursor": "<stream-id>"}`. |
| `PUT /agents/{agent}/talk/cursors/{convo_id}` | Accept and return the same shape; use the guarded backend write. |

Invalid/denied writes leave storage unchanged and use existing error types.
Message reads and `/cursor?after=...` history-gap checks remain unchanged.

The processing sequence is: read cursor → check retention → read the next batch
→ handle → advance through the last fetched ID. Preserve handler selection,
20-entry batches, round-robin polling, and advancement on failure, cancellation,
malformed entries, and own messages. Own messages do not invoke another handler.
Each batch uses a separate talk thread with current messages and existing metadata.

Keep unsaved progress in memory and retry with bounded backoff before handling
another batch. Reconcile with backend progress: if already at or beyond pending
progress, clear pending state; otherwise retry its write. Lease/membership loss
rejects writes. Pin the batch connection, discard pending progress on backend
change, and resume from the selected backend's cursor. Cursor-save failures during
handler cancellation retain diagnostics and cannot replace the cancellation
signal. Shutdown must stay bounded.

## Initialization and transition

Remove obsolete `P:msg:*`, `P:participants`, `P:last_seen`, and blanket `P:agent:*`
checks. Retain current schema/type/record/lease validation and reject fresh
initialization with residual conversation/talk data. Healthy initialized datasets
need no obsolete-format namespace scan.

Rename the existing `P:team` key without fallback, migration, or dual write.
Stop writers and reinitialize development backend data when upgrading. Existing
local cursor files remain untouched and ignored. Human input history and other
runtime storage retain their existing lifecycle. Custom talk runnables using
`previous` must switch to explicit history lookup.

Human read positions, consumer groups, handler retries, other key renames, and
unrelated legacy cleanup are out of scope.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Ownership and isolation | A missing field is `0-0`; agent/conversation progress is independent and has no TTL; humans have no talk cursor API. |
| Restart and relocation | A fresh process with no local runtime consumes new/offline messages without replaying already recorded progress. |
| Authority and validation | Equal writes succeed; invalid/backward/beyond-tail positions, stale leases, and nonmembers fail without writes. |
| Batch behavior | Success, failure, cancellation, malformed entries, and own messages retain the agreed skip behavior; input has no `previous`. |
| Interrupted saves | Lost responses and newer backend progress never rerun the pending handler or block later work; backend switches never receive old pending state. |
| Cancellation | Saving cannot obscure cancellation or make shutdown unbounded. |
| Filesystem | Startup, processing, failures, cancellation, and restart create no channel directories/files and ignore old JSON. |
| Member directory | Registration, presence, roster, messaging, activity, and event publication use only `P:team:members`. |
| Initialization | Fresh partial data and corrupt current records fail without writes; valid existing datasets avoid obsolete-format namespace scans. |

## Touchpoints and verification

- `src/toolang/teaming/backend/protocol.py` and `backend/valkey/`: storage, keys,
  atomic guards, initialization, and activity/event references.
- `src/toolang/teaming/{agent_api,agent_client,messaging,schemas,types}.py`:
  routes, payloads, services, and receiver contract.
- `src/toolang/work/{messaging,teaming}.py`, `src/toolang/common/layout.py`,
  messaging/layout tests, `docs/messaging.md`, and `docs/layout.md`.

Keep default tests offline/deterministic. Run repository Ruff, type, and pytest
checks, plus `git diff --check`. Generate user-visible Unreleased coverage through
`too aid.too update_changelog`.

## Risks

A crash before cursor persistence can replay messages; exactly-once execution is
not promised. Fresh cursors replay retained history, skipped failures receive no
automatic retry, and development-dataset reinitialization discards backend history.
