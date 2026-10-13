# Talk cursor storage

Status: proposed; implementation requires human approval.

## Goal and scope

Persist agent talk-handler progress in the backend, rename the team member key,
and remove obsolete data handling. Success means restart/relocation resumes from
backend cursors and talk generates no `.runtime/channels/` files or directories.

This plan ships no implementation. Human read positions, automatic handler
retries, consumer groups, other key renames, and unrelated legacy cleanup are
out of scope. Preserve handler selection, batch limits, round-robin polling,
message storage, and ordinary CLI/tool reads.

## Storage and API

With `P = too:teaming:v1`:

| Key | Type | Field → value |
| --- | --- | --- |
| `P:team:members` | Hash | Member ID → existing member/lease JSON; replaces `P:team`. |
| `P:agent:{name}:talk` | Hash | Conversation ID → cursor as a full Stream ID. |

Rename `TEAM` to `TEAM_MEMBERS` everywhere, preserving record formats and Lua
base-key ordering. Other existing keys remain unchanged.

A talk cursor means processed or skipped through that position. Missing fields
read as `0-0`. No TTL; retain progress across shutdown, disabled talk, membership
departure, and agent-name recreation. Only the talk handler advances it.

Add backend methods `talk_cursor(agent, convo_id)` and
`advance_talk_cursor(agent, convo_id, cursor, *, token)` without a new store layer.
Writes atomically require a live matching lease, membership, and a valid cursor
between the saved position and stream tail. Equal-value writes are idempotent.

Add agent-only Hub routes using existing authentication and lease headers:

| Route | Contract |
| --- | --- |
| `GET /agents/{agent}/talk/cursors/{convo_id}` | Check lease/membership; return `{"cursor": "<stream-id>"}`. |
| `PUT /agents/{agent}/talk/cursors/{convo_id}` | Accept and return the same shape; advance atomically. |

Reuse existing access/lease errors; invalid positions fail without writes.
Existing message reads and `/cursor?after=...` retention checks remain unchanged.

## Handler and filesystem

Read cursor → check retention → read the next stream batch → handle → advance
through the last fetched ID. Preserve advancement on failure, cancellation,
malformed entries, and own messages, plus existing retention warnings/errors.

Remove the `previous` envelope and saved messages/results. Pass current messages
and existing agent/conversation metadata; use `convo.peek` for history and the
execution store for diagnostics. Delete reply-receipt collection, including its
obsolete `msg__send` branch.

Keep unsaved progress in memory and retry transient cursor-write failures with
bounded backoff before handling another batch. Retry only the write. Authority
loss rejects the update. Keep batch connection pinning; discard pending progress
on backend change and resume from that backend's cursor. Shutdown stays bounded.

Delete local JSON loading/writing/path selection and `AgentLayout.channel_room()`
(only messaging uses it). Ignore existing local files without migration or
cleanup. Other runtime storage keeps its existing lifecycle.

## Obsolete data handling

Remove initialization's legacy-key checks for `P:msg:*`, `P:participants`,
`P:last_seen`, and `P:agent:*`. Add no old-team-key or local-JSON fallback,
migration, or dual write; development datasets require reinitialization.

Preserve current schema/type/record/lease integrity checks and partial-dataset
rejection, including missing conversation schema with residual talk keys. Remove
legacy-only test assertions while retaining current-data corruption coverage.

## Implementation touchpoints

- `src/toolang/teaming/backend/protocol.py` and `backend/valkey/`: key names,
  cursor methods, atomic guards, initialization, activity/event references.
- `src/toolang/teaming/{agent_api,agent_client,messaging,schemas,types}.py`:
  routes, payload validation, service methods, and receiver contract.
- `src/toolang/work/{messaging,teaming}.py` and `src/toolang/common/layout.py`:
  batches, pending writes, shutdown, and filesystem removal.
- Messaging unit/integration and layout tests; update `docs/messaging.md` and
  `docs/layout.md` during implementation. Generate Unreleased coverage with
  `too aide.too update_changelog`.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Isolation and recovery | Missing cursor is `0-0`; agents/convos are independent; restart/relocation resumes without local files. |
| Writes and authority | Equal writes succeed; invalid/backward/beyond-tail values, stale leases, and nonmembers fail without writes; no human cursor API. |
| Batch outcomes | Success, failure, cancellation, malformed entries, and own messages retain skip semantics; handler input has no `previous`. |
| Save interruption | Failure/lost response retries only the write; backend changes never receive another backend's progress; shutdown remains bounded. |
| Filesystem | Startup, processing, failure, cancellation, and restart create no channel directories/files and ignore old JSON. |
| Team rename | Registration, presence, roster, messaging, activity, and event publication use only `P:team:members`. |
| Initialization | Valid current data accepts talk keys; malformed/partial current data fails without writes. |

Use offline deterministic tests and run the repository's default Ruff, type, and
pytest checks for implementation. Plan-only verification checks source paths and
runs `git diff --check`.

## Risks and open questions

- Crash/forced stop before cursor persistence can replay messages; exactly-once
  execution is not promised. Skipped failures receive no automatic retry.
- Fresh cursors replay retained history. Custom talk runnables using `previous`
  must switch to explicit history lookup; reinitialization loses backend history.
- Open questions: none. Human confirmation of this definition is pending.
