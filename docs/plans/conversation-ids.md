# Conversation identity, names, and storage

Status: definition confirmed on 2026-10-10. This definition ships no code.

## Goal and scope

Replace the group model with conversations throughout storage, APIs, tools,
CLI, and processing state. Success means deterministic DM lookup by participant,
unique GC allocation, editable names, resumable membership/presence events,
and statistics without scanning all conversations on reads.

Use `conversation` and `participants` in APIs; shorten storage to `convo` and
`members`. Agents read joined conversations; human observers may read others;
only participants may send or rename. Ordinary GC membership uses self-service
join/leave; any conversation participant may rename it. Invitations, removing
other members, and administrator roles are outside scope.

Exclude fuzzy search, directory pagination, participant renaming, conversation
deletion, message-ID changes, exact unread counts, time-window analytics,
per-participant aggregates, execution allocator changes, backward compatibility,
and data migration. Client-facing integration targets Talk; Top changes are
limited to removing the last-seen display, with no subscription or refresh changes.

## IDs and lookup

Both IDs use eight lowercase base32 characters from
`0123456789abcdefghjkmnpqrstvwxyz`, matching thread/run style. Reuse pure helpers
from [common/ids.py](../../src/toolang/common/ids.py); preserve execution encodings
and per-agent allocation state.

| Kind | Identity | Participants |
| --- | --- | --- |
| `dm_` | Hash of two canonical identities, e.g. `agent:alice`, `human:brice` | Exactly two distinct members; no addition, removal, replacement, or conversion to GC |
| `gc_` | Backend-wide time/sequence allocation, independent of name and members | Initially the creator; mutable |

### DM

Sort the two full typed identities by UTF-8 bytes, preserving case and Unicode.
Serialize `["toolang:dm:v1", a, b]` as compact UTF-8 JSON with literal Unicode.
Take SHA-256's first five bytes as a big-endian integer, encode eight base32
characters including leading zeroes, and prepend `dm_`. No pair index is needed.

Opening an occupied ID must verify its kind and exact member pair. Reuse a match;
reject corrupt membership or a hash collision without exposing messages,
overwriting data, or salting the ID. Forty bits cannot guarantee unique hashes.
`agent:alice` + `agent:bob` produces `dm_6x89kwxn` in either order.

### GC

Use epoch `2026-01-01T00:00:00Z`, an hourly 20-bit tick, and a 20-bit sequence:

```text
current_tick = floor((Valkey_TIME_seconds - epoch_seconds) / 3600)
tick = max(current_tick, last_tick)
seq = 0 if tick > last_tick else last_seq + 1
raw = (tick << 20) | seq
key = BLAKE2s(b"toolang:conversation:gc:v1").digest()
M = (1 << 20) - 1
F(i, R) = uint_be(BLAKE2s(uint8(i) || uint24_be(R), key=key).digest()) & M
(L, R) = (raw >> 20, raw & M)
for i in 0, 1, 2, 3:
    (L, R) = (R, L XOR F(i, R))
C = "gc_" + base32_fixed_width((L << 20) | R, 8)
```

BLAKE2s uses its full 32-byte digest. This is the existing four-round Feistel
primitive applied directly, without execution's affine transforms. Invert with
`(L, R) = (R XOR F(i, L), L)` for rounds `3, 2, 1, 0`. It is a bijection:
distinct reserved tuples remain distinct. The fixed public key and codec belong
to schema `2`; all writers use them unchanged. This is scrambling, not encryption.

Reserve/persist the tuple atomically using backend time, encode in Python, then
atomically create with known keys. Check authority before reservation and again
at creation. Never reuse abandoned reservations, including lost-response retries.
Reject occupied IDs or orphaned member/message keys; reserve again for at most
128 candidate conflicts. Uniqueness is within one backend; full prefixes separate
ID families.

Each GC creation request allocates a new ID. A retry after a lost success response
may create a second GC; this is accepted. Add no `request_id` or deduplication
record. System-role initialization still reuses its single role pointer.

Require `0 <= tick, seq < 2^20`. Initialize `last_tick=last_seq=-1`; reject clocks
before the epoch. Rollback holds the last tick and advances its sequence.
Exhaustion fails without wrapping; exhausted sequences wait for a later bucket.
Missing/corrupt initialized allocator state fails without reset.

At `2026-10-10T02:00:00Z`, tick `6770`, sequences `0, 1, 2` produce
`gc_rcpya1zw`, `gc_apxp9knw`, `gc_zqgdw7cp`.

**Capacity:** 1,048,576 reservations/hour/backend, averaging at most
291.271 reservations/s with normal clock progression. At 1,000/s, a fresh bucket
starting on the hour lasts about 17m 29s. Gaps, conflicts, and system allocation
consume this budget; DMs do not. This is ID capacity, not measured throughput.
Capacity and operation complexity are sufficient for this delivery; no creation
throughput benchmark is required. Sustained 1,000 GC/s exceeds the accepted hourly
budget and would require revisiting the format.

### Names and resolution

Both kinds use optional `name: string | null`: 1–128 Unicode code points,
case-sensitive, no control characters, blank values, or surrounding whitespace.
Duplicates are allowed; `null` clears the name. Unnamed DMs display participants;
unnamed GCs display their ID. Opening an existing DM never implicitly renames it.

| Input | Resolution |
| --- | --- |
| Canonical ID | Exact metadata read and access check |
| Participant name | Validate identity, derive caller/participant DM ID, then read and verify the pair; opening is read-only, while sending may create an absent DM |
| Explicit participant pair | Validate both identities and caller authority, derive the pair's DM ID, and verify any existing record; an observer cannot create a missing DM |
| Conversation name (explicit API lookup only) | Read matching name-index Set, filter by access, resolve one match or return ambiguous candidates with IDs, kinds, and participant labels; Talk does not use this lookup |

ID/participant lookup never scans directories. Name-search cost depends on equal
name matches; full listing has no bounded-lookup guarantee. Lookup and opening
never create conversations: lookup defaults to `create=false` and returns no
existing DM when absent. Explicit creation or sending may create a DM only when
the authenticated caller belongs to the pair. Enforce this in the backend even
if an observer requests creation explicitly. Observation only reads an existing
DM; a missing pair returns not found without changing records, counters, or
events. Reject a nonmember send before creating anything. An absent canonical
ID alone cannot recover its pair and returns not found. Unknown identities and
identical-member pairs fail.

## Valkey model

Use one standalone backend per domain, `P = too:teaming:v1`, canonical conversation
ID `C`, and full typed member identities. Only storage keys abbreviate conversation;
record fields, event types, and ID-codec constants retain the full term.

| Key | Type | Value |
| --- | --- | --- |
| `P:team` | Hash | Member -> team JSON |
| `P:team:presence` | ZSet | Agent member -> lease deadline, Unix milliseconds |
| `P:team:events` | Stream | Field `data` -> team-change event JSON |
| `P:convos` | Hash | `C` -> conversation JSON; reserves IDs |
| `P:convo:<C>:members` | Set | Member identities |
| `P:convo:<C>:messages` | Stream | Existing message envelope in `data`; `MAXLEN ~ 10000` |
| `P:convo:name:<N>` | Set | Matching conversation IDs; `N` is unpadded base64url of exact UTF-8 name (`dev` -> `ZGV2`) |
| `P:convo:schema` | String | `2` |
| `P:convo:id:gc` | Hash | `last_tick`, `last_seq` |
| `P:convo:stats` | Hash | `dm_count`, `gc_count`, `messages_total`, initially zero |
| `P:convo:system` | Hash | Stable role `all` -> system GC ID |

Initialize schema, allocator, counters, and event feed atomically on a fresh
backend. Preserve them with records and presence in backups; missing/malformed
initialized state is an integrity error. These structures have no key TTL.
Messages appear on first send; empty GCs retain metadata even without a member
Set. Preserve message UUIDs, Stream IDs, independent cursors, and history-gap
notices. No Stream deletion/recreation, arbitrary `XDEL`, or `XSETID` is added.

Retain existing `P:events:*` for execution observation, `P:activity:<agent>`
for activity caches, and `P:roster` for discovery/management records. Do not rename
or move `P:roster` in this change.

`P:team` owns global member identity/owner/lease metadata. `P:roster` is an optional
agent-only subset for root ownership and discovery state: `{root, managed, missing}`.
Humans have no roster entry; unscoped agents may exist only in team. Presence is
independent. Roster discovery creates both records atomically, never adopts another
root's claim, and removes both plus GC memberships only after confirmed absence
and lease expiry. DM membership/history remain valid after directory removal.

Python owns strict persisted/wire schemas and protocol rules; Lua owns atomic
storage checks, lease authority, counters, allocation, and combined writes.
Python validates a read snapshot; Lua compares exact values and memberships before
mutation. Changed snapshots retry within a fixed bound; transport failures never
replay uncertain writes. Constants come from Python definitions. Pending Talk DMs
use a separate local model without fabricated persisted timestamps or revision.
Conversation directories load bounded batches of 128, checking visibility and
reading previews atomically in each batch. Direct-ID/pair lookup remains constant
in directory size; whole-directory reads remain linear without a new index.


### Team JSON

Example at `P:team[agent:alice]`:

```json
{
  "display_name": "alice",
  "owner": "human:brice",
  "created_at": "2026-10-10T02:00:00Z",
  "lease": {"token": "example-only-token", "endpoint": "http://127.0.0.1:8123"}
}
```

All four fields are required; identity/kind come from the Hash field.

| Field | Contract |
| --- | --- |
| `display_name` | String initialized from the exact bare identity name; not an identity/index key |
| `owner` | Canonical human owner for agents; `null` for humans |
| `created_at` | Immutable first-registration time, UTC RFC 3339 with `Z` |
| `lease` | Internal object with nonempty string token and string endpoint (empty allowed); may be expired pending cleanup; `null` before acquisition/after settlement and always for humans |

No profile/ownership editing is added. Registration preserves metadata; writers
read the current JSON inside their mutation script and modify only owned fields.
Do not duplicate deadlines or online flags. Public projections omit tokens;
endpoints follow existing routing permissions. Logs/events never expose tokens.

### Conversation JSON

Example at `P:convos[dm_6x89kwxn]`; both kinds share these seven required fields:

```json
{
  "id": "dm_6x89kwxn",
  "kind": "dm",
  "name": "Review",
  "created_by": "agent:alice",
  "created_at": "2026-10-10T02:00:00Z",
  "updated_at": "2026-10-10T02:05:00Z",
  "revision": 2
}
```

| Fields | Contract |
| --- | --- |
| `id`, `kind` | Immutable; ID equals Hash field and prefix agrees with `dm`/`gc` |
| `name` | Editable under the name rules |
| `created_by` | Immutable authenticated creator and initial participant; `null` only for system creation; remains historical after departure |
| `created_at`, `updated_at` | Backend UTC RFC 3339 timestamps with `Z`; initially equal; only effective rename changes `updated_at` |
| `revision` | Backend integer starting at 1; increment once per effective rename; controls concurrent metadata writes |

Reject unknown/missing fields, invalid types, ID/kind mismatches, and client-supplied
protected values. No-op rename, membership, messages, and presence leave metadata
timestamps/revision unchanged. Do not duplicate members, counts, previews, aliases,
or a system flag. The system role pointer survives renaming; registration retains
automatic enrollment under its existing system-managed membership policy.

### Atomic operations

Use backend-owned Lua scripts with explicit keys. Validate types, records,
permissions, leases, event epoch, counters, and overflow before the first write:
Lua atomicity does not provide rollback after runtime errors. Apply corresponding
events and counters in the mutation script; never report event/counter failure as
success. Preserve the no-automatic-retry rule for uncertain message sends.

| Operation | Writes |
| --- | --- |
| Create DM/GC | Metadata, initial members, optional name-index membership; GC candidate must be unused; identical DM reuse makes no changes |
| Rename | Check membership and expected revision, remove old index entry, update metadata, add new index entry; same name is a no-op |
| Join/leave | Add/remove only the caller in an ordinary GC with `SADD`/`SREM`; reject all DM membership mutations |
| Send | Check membership and applicable live lease; for an absent participant-derived DM, atomically create metadata/members and append the first message with all corresponding events/counters; otherwise append to the existing Stream |
| Ensure system GC | Allocate if needed, then atomically recheck/create the record and role pointer; losing reservations remain gaps |

Rename callers supply old/new index keys from the current name/revision;
a revision conflict requires reload before retry. Empty name Sets disappear.
For a first DM send, carry both canonical identities so the backend can derive
and verify the ID and sender's membership before writing. Do not split creation
and append into separate requests. Concurrent first sends create the DM once
and append each accepted message; validation failure leaves no new conversation.

## Team events

`P:team:events` contains only team-member, presence, and conversation-member
changes. Each entry has one `data` field holding serialized JSON; its Stream ID
is the event ID:

```json
{
  "v": 1,
  "epoch": "704f721735814c44a68966f2f37f430a",
  "type": "presence.offline",
  "conversation": null,
  "actor": null,
  "payload": {
    "member": "agent:alice",
    "reason": "expired",
    "effective_at_ms": 1791597600000,
    "observed_at_ms": 1791597601000
  }
}
```

`conversation` is the ID for conversation-member events, otherwise `null`.
`actor` is the caller or `null` for background/system changes. No endpoint
values or state snapshots are included.

| Event | Payload / trigger |
| --- | --- |
| `team.member_added`, `team.member_removed` | `member`; actual directory insertion/removal |
| `conversation.member_added`, `conversation.member_removed` | `member`; actual membership change, including creation, system enrollment, and roster cleanup |
| `presence.online` | `member`, `effective_at_ms`, `observed_at_ms`; lease acquisition after offline |
| `presence.updated` | Same plus `changed=["endpoint"]`; effective endpoint change |
| `presence.offline` | Same timing fields plus `member`, `reason` (`released` or `expired`); release or expiry reconciliation |

Emit once per changed member: two additions for a new DM, one for an ordinary GC.
No-op operations, heartbeat renewal, rename, allocation, and message append emit
no domain event. Names/statistics refresh through reads or operation responses;
messages use their own Stream.

Keep epoch in every entry and obtain tail from
[XINFO STREAM](https://valkey.io/commands/xinfo-stream/) `last-generated-id`.
One read script verifies that tail matches `last-entry` and extracts its epoch.
Fresh initialization writes `stream.initialized` with UUID4-hex epoch,
`v=1`, null conversation/actor, and empty payload; concurrent initializers reuse
it. This control entry changes no counts and may be trimmed. Use
`MAXLEN ~ 100000`, retaining at least one entry; no separate metadata Hash.
Missing/empty initialized feeds, malformed entries, or inconsistent tails fail
without silent recreation. Intentional dataset replacement uses a new epoch.

Cursor: `t1.<epoch>.<stream_id>`. Hub subscriptions use independent cursors and
[XREAD](https://valkey.io/commands/xread/). Capture tail before the state snapshot,
replay from it, deduplicate IDs, and refetch state on invalidation. Validate each
bounded replay batch atomically; blocking XREAD is only a wake-up hint.
Epoch mismatch, cursor ahead of tail, or cursor before the retained range after
trimming requires resync; `entries-added > length` distinguishes trimming.
A cursor immediately before a trimmed range may conservatively resync.

Team/presence visibility follows the directory. Conversation events require read
access, except a removed member may receive its own removal. Checkpoints advance
across filtered events; joining requires fresh state, leaving invalidates access.
The feed is bounded, not an audit log or source for lifetime totals.

The server provides the feed and state reads; consumers may subscribe or fetch
state as needed. No common client polling schedule is required. Talk adapts its
existing read loop to refresh conversation metadata and relevant presence
deadlines; message reads remain on the conversation Stream. Top does not adopt
this feed or a new refresh policy in this change.

## Presence

A 15-second lease, renewed by the client's five-second heartbeat, uses only the
ZSet deadline for online authority. Backend checks use Valkey TIME:

```text
deadline = ZSCORE P:team:presence member
online = deadline exists AND deadline > server_now_ms
```

Equality is expired even before cleanup. Score and non-null team lease must exist
together; malformed/orphaned pairs fail closed. A known agent with neither is
offline; an entirely offline team may have no ZSet. Human presence is excluded.

| Mutation | Atomic behavior |
| --- | --- |
| Acquire | Validate owner; reject a different live token; same live token follows renewal/endpoint update. Otherwise settle pending expiry, store a fresh token/endpoint, set deadline to now + 15,000, emit online |
| Renew | Require live matching token; set score to `max(old_score, now + 15000)` only; never revive an expired/missing lease |
| Change endpoint | Require live matching token; update current lease/renewed score and emit updated only on change |
| Release | Require live matching token; remove score, clear lease, emit released offline; stale/repeated releases cannot affect a replacement |
| Reconcile expiry | Re-read current time, score, and lease; if still due, remove score, clear lease, emit expired offline; absent or renewed scores are no-ops |

Retries of an active acquisition reuse its token. Protected messaging, execution,
and activity writes validate current score/token in the same script. Roster
removal instead checks offline state and existing owner/CAS rules. Prior reads
never authorize later writes; routing/observation share the deadline predicate.

### Worker ownership and lifecycle

Each Hub lifespan owns exactly one presence worker, whether or not roster
discovery is configured. Use the existing
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) integration,
with these module boundaries (paths relative to `teaming/`):

| Module | Responsibility |
| --- | --- |
| `backend/protocol.py` | Driver-independent storage interfaces; no commands, keys, Lua, or scheduling |
| `backend/valkey/` | Valkey driver, due-member query, and atomic expiry/state/event operations |
| `presence.py` (new) | `PresenceWorker` with a bounded `reconcile_once()` and cancellable `run()`; uses an injected backend, owns no connection pool or HTTP state |
| `lifecycle.py` (new) | Own shared client/backend lifetime and presence/optional roster tasks, startup readiness, failure observation, and cleanup; keep it specific to Hub services |
| `api.py` | Wire lifespan and routes, expose lifecycle health; no polling loop or detached tasks |

Lifecycle order:

1. Register resource cleanup, connect/check the backend, and initialize/validate
   schema, counters, and event feed before registration or other producer writes.
2. Register the human/system conversation, perform one bounded presence pass,
   and retain the existing best-effort initial roster scan when configured.
   Do not drain the entire expiry backlog before startup can finish.
3. Start owned worker tasks, verify startup succeeded, then call `on_ready`.
   Protect the whole startup path, including callback failure, with cleanup.
4. On shutdown, stop scheduling, cancel and await both workers, then close the
   shared client/backend. Propagate cancellation through sleeps and I/O; never
   leave tasks using a closed connection or waiting indefinitely on a retry.

Run presence scans immediately after startup and every second, at most 128 due
members per batch; drain further batches with cooperative yields. Use backend
operation timeouts and the existing Hub startup deadline. Transient
`BackendUnavailable` failures retry with backoff capped at five seconds, resetting
after success. Roster scan failures do not disable presence. Integrity errors or
unexpected worker exit are recorded and surfaced through failed readiness
(`/healthz` returns 503 until restart), never silently ignored or repaired by
resetting storage. App construction/import and request handlers start no workers.

Multiple Hubs need no leader lock: candidates are hints and each expiry script
rechecks current score/lease. Only the mutation removing a score emits offline,
including acquisition/roster settlement; reconnect emits old offline before new
online. Shutdown leaves deadlines and leases intact, without synthesizing member
offline events. A cancelled call may already have committed; atomic settlement
makes later reconciliation safe.

Overdue scores survive outages for later cleanup. Expiry is effective at the
deadline; event delivery has polling/scheduling delay with no hard bound.
For expiry, `effective_at_ms` is the removed score and `observed_at_ms` is cleanup
time; explicit transitions use backend time for both. Presence expiry never removes
members. Separate roster cleanup removes GC membership and directory entries while
preserving DM membership/history.

**Client reads:** return sanitized state and deadline; the client compares with
its clock and corrects only the local result/cache. Missing/elapsed deadlines
mean locally offline; fresh renewed deadlines restore the view. Reads never
write records/scores, request cleanup, renew leases, or emit events. Clock skew
or stale snapshots affect only this view; surface backend/integrity failures.

**Remove last-seen:** stop reading/writing `P:last_seen`; remove `last_seen` from
`ActivitySnapshot`, responses, and the offline display. Add no replacement key
or field. Keep `observed` for snapshot freshness, never as a fallback last-contact
label. Ordinary heartbeats write only the presence score.

## Statistics

| Metric | Source / atomic maintenance |
| --- | --- |
| Global `conversations_total` | `HLEN P:convos`, including system GC |
| Global `dm_count`, `gc_count` | Stats Hash; increment the kind counter once on first creation; sum equals total |
| Conversation `participants_count` | `SCARD` members Set |
| Conversation `messages_total` | `XINFO STREAM entries-added` |
| Conversation `messages_retained` | `XLEN` or XINFO `length` |
| Conversation `last_message_stream_id` | XINFO `last-entry` ID, or `null` |
| Global `messages_total` | Stats Hash; increment once per committed append |

Use native metadata, never XINFO `FULL` or request-path enumeration. An unused
conversation's absent message Stream means zero; require `entries-added` support,
never substitute retained length. Counts measure accepted appends, not attempts
or unique message UUIDs. DM reuse, allocation gaps, rename, membership changes,
and trimming do not alter conversation/lifetime-message totals.

Read each response in one authorized read script. Global totals require human
observer access; agents see only readable conversations' statistics. Audit
aggregates against records/Streams only offline with writes paused. Stream-ID
subtraction and event counts cannot provide exact unread counts.

## Integration and rollout

| Surface | Proposed contract |
| --- | --- |
| HTTP messaging | `/msg/conversations`, `/{conversation}`, and `/participants`, `/messages`, `/cursor` subresources; PATCH name with expected revision |
| Statistics | `GET /msg/stats`, `GET /msg/conversations/{conversation}/stats` |
| Team SSE | `GET /team/events?after=...`; without cursor, initial checkpoint precedes snapshot loading; gap emits `resync_required` and closes, then reconnect without cursor and reload |
| Tools/receipts | `conversation` / `conversations` fields; create, rename, join/leave, resolve through existing factories |

### Talk CLI

All Talk operations require the selected root's Hub to be running and reachable.
If stopped, exit nonzero with `Hub is not running; run 'too hub start'`; if still
starting, report that it is not ready. Talk never starts Hub automatically or
falls back to direct Valkey access.

With no arguments, `too talk` prints a one-shot directory and exits, including
when output is redirected. Show two labeled sections:

| Section | Contents |
| --- | --- |
| `Team` | All visible registered agents and humans, including members with no conversations; canonical member ID, display name, owner, and agent online/offline state; human presence is not applicable |
| `Convos` | Accessible conversations with separate ID and Name columns (`null` name shown as an em dash), kind, participants, and existing latest-message time/preview; users locate the desired name here and open its ID |

Keep both headings and usage hints when a section is empty. Listing does not
open an interactive session, create a DM/custom GC, or allocate a tmux window.
Apply existing visibility and client-local presence rules; never display lease
tokens or endpoints. Talk uses positional targets only:

| Target | Meaning |
| --- | --- |
| `alice` | DM between the current authenticated human and `agent:alice`; if absent, open an empty chat and create the DM only on the first accepted send |
| `alice,bob` | Existing DM between `agent:alice` and `agent:bob`; order-independent, opened by the human as a read-only observer; if absent, report that no conversation exists |
| Canonical `dm_...` or `gc_...` ID | Open that existing conversation directly, applying its read/send permissions |

Canonical-ID syntax takes precedence over bare names. A pair requires exactly
two nonempty, distinct, valid agent names; unknown agents or extra commas fail.
Bare names always select agents, never conversation names or a system-role
shortcut. Conversation names are labels for directory lookup by the user; they
are not Talk targets. Remove `--dm`/`--group`; add neither `--gc` nor `--name`.
Usage hints show the positional forms (`too` aliases `toolang`):

```sh
too hub start
too talk
too talk alice
too talk alice,bob
too talk dm_6x89kwxn
too talk gc_rcpya1zw
```

An empty human-agent chat retains the canonical pair and derived ID locally;
it creates no conversation record, members, statistics, or events and does not
appear in Convos. Closing without sending leaves the backend unchanged. While
waiting, read-only refresh may discover a DM created by the other participant;
verify and load it normally. Until then, show empty history without fabricating
persisted metadata or creating a conversation to obtain a message cursor.

The system GC remains listed and is opened by ID. GC creation and rename use the
shared API/tools; a separate CLI management command is out of scope. A target
without message text opens interactive Talk and requires a TTY; trailing words
are message text to send once and exit. Sending to an observed agent-agent DM
is rejected because the human is not a participant.

### Rollout

This breaks messaging schema/API compatibility. Remove old group routes, fields,
keys, targets, tools, CLI selector, and aliases; retain no private legacy layer.
Upgrade Hub and the affected clients together. Support fresh datasets only;
reject old/mixed schema as unsupported without converting or deleting it. Begin
with fresh local conversation state; do not import old history, drafts, or
checkpoints. No compatibility layer, migration tooling, or migration procedure
is included. Existing execution endpoints, events, and cursors remain unchanged.

## Implementation and acceptance

Paths below are relative to `src/toolang/`:

| Touchpoints | Work |
| --- | --- |
| `common/ids.py` | Expose/reuse pure codec helpers, preserve thread/run outputs |
| `teaming/{schemas,messaging,types}.py`, `teaming/backend/` | Records, IDs, name resolution, atomic writes, schema guard |
| `teaming/{messaging_api,client,agent_client}.py`, `plugin/toolsets/msg.py` | Conversation contracts and client-local presence projection |
| `teaming/{events,api,agent_api,roster}.py`, team-owned subscription module | Feed, replay, visibility, lease operations, and lifespan wiring |
| New `teaming/presence.py`, `teaming/lifecycle.py` | Bounded expiry worker and shared Hub task/resource ownership under the lifecycle contract above |
| `teaming/backend/valkey/{events,activity}.py`, `teaming/{activity,subscriptions}.py` | Shared deadline/token checks; preserve execution protocol |
| `execution/schemas.py`, `cli/common/activity_view.py` | Remove last-seen fields/display and observed-time fallback; preserve Top subscription/refresh behavior |
| `cli/toolang/commands/talk/`, `cli/common/tmux.py`, `work/messaging.py` | Team/conversation directory, usage hints, IDs, labels, receipts, window/checkpoint vocabulary; preserve running-Hub discovery through `cli/common/messaging.py` |

Update messaging, roster, activity, toolset, and Talk tests and relevant ID/messaging
docs. Generate the implementation's breaking-change Unreleased entry with
`too aide.too update_changelog`.

| Acceptance scenario | Pass condition |
| --- | --- |
| Records and lifecycle | Reject malformed/protected fields; preserve historical creator and token privacy; only effective rename changes metadata revision/time |
| DM derivation | Reversed/concurrent/restarted lookup and Unicode/namespace vectors agree; forced collision is isolated; all membership mutations fail |
| Creation intent and retries | Lookup/open creates nothing; first participant send atomically creates/reuses its DM and appends, concurrent sends count one DM, and rejected sends create nothing; observer lookup of a missing DM returns not found; explicit nonmember creation/send/rename fails; GC retries after a lost response may create a second distinct GC with correct counts |
| GC codec/allocation | Golden vectors and boundary/sample round trips pass; concurrent callers, rollback, rollover, lost responses, and conflicts never reuse reservations; execution vectors unchanged |
| GC limits/integrity | Exactly 1,048,576 reservations per bucket including gaps; exhaustion never wraps; initialized allocator corruption never resets |
| Lookup and rename | Duplicate/ID-shaped names, clear/no-op/stale rename, and permissions behave as specified; 10 versus 10,000 unrelated records have the same ID/participant lookup count |
| Atomicity and system GC | Concurrent create/rename preserves indexes/counters; validation leaves no partial writes; system rename/concurrent initialization preserves one role pointer |
| Events and recovery | Only specified effective changes emit; epoch survives initialization-entry trimming; independent replay handles snapshot races, duplicates, gaps, filtering, and removed-member visibility |
| Talk Hub requirement | Stopped/starting/unreachable Hub fails clearly for directory, open, and send; stopped-Hub error gives the start command; no implicit launch or direct-backend fallback |
| Talk directory | No arguments prints Team and Convos then exits without a TTY; includes members with no conversations, separate ID/Name columns, presence, and positional usage hints; empty sections remain explicit; no DM/custom-GC or window is created |
| Talk target grammar | A single name opens the human-agent chat; reversed two-agent pairs resolve identically and remain read-only for the human, with no creation on a miss; canonical IDs open directly; invalid pairs and selector flags fail; a conversation-name match never changes bare-agent resolution |
| Talk empty DM | Opening/closing a missing human-agent DM leaves records/counters/events unchanged and Convos omits it; interactive and one-shot first sends persist it under the atomic-send contract; a DM created by the other participant is discovered and loaded without duplicate creation |
| Talk integration | Opening/sending, participant resolution, renamed labels, and local deadline checks use the new contracts; reads never repair server state; Top has no new subscription or refresh behavior |
| Presence | Stale tokens and expired-but-unremoved scores grant no authority; renew/reconnect/competing cleanup/outage races record one offline transition per expired lease without changing membership |
| Hub lifecycle | No tasks before lifespan; one presence worker with or without roster; schema checks precede writes and readiness follows startup; failures at every startup stage unwind tasks/resources; shutdown awaits workers before backend close without expiring agents |
| Worker recovery | Fake-backend and controlled-wait tests cover bounded batches/backoff, cancellation during scan/wait, roster failure isolation, fatal-worker health failure, and repeated lifespan entry/exit without leaks; no wall-clock sleeps or live backend required |
| Client reads and last-seen | Clock skew and expired snapshots change only local views; repeated reads leave backend/events untouched; no last-seen access, response field, or label remains, including with observed snapshots |
| Statistics | Concurrent creates/appends and retention preserve native/global totals; missing counters fail; access checks and offline reconciliation hold without read-path enumeration |
| Rollout | Fresh keys/vocabulary match the design; old/mixed data is rejected without deletion or migration; old local conversation state is not imported; execution feeds/cursors and the roster key remain unchanged |

For this definition, verify examples/links against source and run
`git diff --check`. Implementation requires the repository's default checks;
use command-count assertions rather than timing tests. Remaining risks are
40-bit DM collisions, the accepted GC hourly budget and duplicate-create retry
behavior, ambiguous names, and fresh-dataset-only rollout. No design decisions
remain open.
