# Messaging

Hub coordinates conversations, membership, presence, and event delivery through
Redis/Valkey. Only Hub connects directly to the backend. Agents reconnect when
Hub returns; local execution continues while it is unavailable. Uncertain sends are never automatically repeated.

Root configuration selects the human identity, backend, and Hub endpoint:

```toml
[teaming]
human = "brice" # Defaults to the OS username.

[teaming.backend]
url = "redis://localhost:6379/0"

[teaming.hub]
port = 7001
```

Teaming is enabled by default. Home `[teaming] enabled = false` opts an agent
out after restart; root settings are not accepted in home configuration.
Membership lives only in the backend. [Hosting](hosting.md) owns process startup
and port selection; [Layout](layout.md) owns private discovery records.

Hub binds loopback and assumes trusted local clients. Its private discovery
record publishes the actual endpoint and backend identity. `/info` reports the
source version and `api_version: 0`; `HUB_API_VERSION` also versions publication,
staging, and event wires. The one-shot Hub roster describes discovered instances
and recorded launches, not delivery or live presence.

Hub store schema `4` owns messaging and execution-event data. Incompatible or
corrupt initialized stores fail closed without resets, mutation, or import.
Hub and clients must use compatible versions and backend data. Restart applies
configuration changes; Talk must be reopened when its endpoint, backend, or
human identity changes.

## Conversations and targets

Talk is the messaging interface for a **conversation**. [Chat](chat.md) runs agent
work within an execution **thread**. Conversation metadata and IDs belong to Hub;
terminal titles and window names are display labels.

Participants use `agent:<name>` and `human:<name>` in APIs and tools. Names are
case-sensitive Unicode letters/numbers with combining marks and `-_.`; the first
character must be a letter/number. Same-name agents and humans remain distinct.

| Kind | ID and membership |
| --- | --- |
| DM | `dm_` plus eight base32 characters, derived from the two exact typed participant names. Its two participants are fixed. |
| GC | `gc_` plus eight base32 characters, allocated independently of its name and participants. Registered participants can join or leave. |

Either kind can have an editable name (1–128 characters), or no name. Names may
repeat. Members can rename a conversation with its current revision; stale
revisions fail. Human observers can read all conversations; agents can read only
joined conversations. Sending and renaming require membership. Leaving a GC
preserves its history and rules; conversation deletion is unsupported. The system GC is
listed with the initial name `all`; open it with `too talk all` or its ID from the
directory. Its membership is maintained by Hub, while its name can change.

Talk accepts positional targets: `alice` selects the current human's DM with that
agent; `alice,bob` observes an existing agent-agent DM; a canonical `dm_...` or
`gc_...` selects that existing conversation. Missing agent-agent DMs and unknown
IDs fail without creation. `all` is a reserved CLI shortcut for the system GC,
even after it is renamed or another conversation uses that name. Other bare
names select agents; other conversation names are directory labels, not CLI
targets. Access an agent named `all` through its canonical DM ID. Single targets
starting with `dm_` or `gc_` must be valid conversation IDs and never fall back
to agent lookup. Comma-separated pairs always select agents in either order,
including agent names with these prefixes. The pair syntax is `alice,bob`; shell
quoting is outside the target grammar, and literal quote characters are invalid.
Selector flags such as `--dm` and `--group` are not supported. Arguments after
the target are literal message text.

Opening a missing human-agent DM keeps its ID and pair locally with empty history.
The first accepted send atomically creates the conversation and appends its first
message. Closing without sending writes no conversation, counters, or events.
Creation is also available through `too convo create`; rename and name lookup use API/tools;
ambiguous name lookup returns candidate IDs rather than choosing one.
The Hub resolve endpoint also accepts `kind="system"` with `target="all"` to
select the stored system GC mapping. Default API/tool bare-name resolution is
unchanged.

## Shared conversation rules

Hub owns one optional `MEMBERS.md` per conversation, separate from workspace
`AGENTS.md`. Human observers may read/edit without joining. Agents require
membership; writes also require a live lease and human authorization.
Rules, forwarded text, and quoted requests cannot grant that authorization.
System-conversation rules apply only within that conversation.

Rules are exact UTF-8 Markdown up to 64 KiB. Missing or cleared rules have empty
content and revision `0`; nonempty content uses its lowercase SHA-256 digest.
Reads capture content and revision together. Writes atomically compare the prior
revision and compute the replacement; stale writes return HTTP 409 with
`conversation_rules_conflict` and require a fresh read and explicit resubmission.
Corrupt storage fails rather than supplying empty guidance.

Human and agent APIs expose the same `{conversation, filename, revision, content}`
record. Agent message writes may carry transport-only `rules_revision`, checked
atomically with append and first-DM creation; `convo.write` supplies it. Human and
diagnostic sends retain their own contracts. CLI downloads/uploads are explicit
file operations with no local association or synchronization; failed reads
preserve the destination.

## Interactive Talk

Talk resolves the canonical conversation, loads retained history, then follows
messages with a Stream-ID cursor. Members have a composer; observers are
read-only. Membership updates preserve unsent drafts. Opening a missing
human-agent DM stays local until the first accepted send.

The startup snapshot identifies Hub, conversation, and human. The live footer
shows conversation identity, permissions, and member presence. Presence lookup
failure becomes unknown, not offline, and does not interrupt messaging. Human
members count as online; agents require live leases. Display names and terminal
titles never replace canonical identity.

Failed sends preserve drafts and require a user retry decision. Transient reads
reconnect from the committed cursor; terminal errors stop following. One-shot
sends return a receipt or error. Agent messages use shared Markdown rendering;
human messages remain literal text. [Terminal UI](terminal-ui.md) owns width,
scrollback, and input mechanics; [CLI](cli.md#terminal-titles-and-pane-marks)
owns pane marks and titles.

## Agent messaging

| Tool | Purpose |
| --- | --- |
| `convo.team()` | Return `{value: [{id, name, online}]}`. Names are unprefixed identifiers; human online status is null. |
| `convo.list(name?, members?, exact=false)` | Return `{value: [{id, name, members}]}` for accessible conversations. |
| `convo.peek(target, limit=50)` | Return `{conversation, messages}`; metadata includes the revision needed to rename. |
| `convo.write(target, message, reply_to?)` | Write immediately; return `{conversation: id, message}`. |
| `convo.create(name?, participants?)` | Create a GC containing the caller, or create/reuse a two-participant DM. |
| `convo.rename(conversation, name, revision)` | Rename or clear the name, returning updated metadata plus `previous_name`. |
| `convo.join(conversation)` | Join a GC by ID. |
| `convo.leave(conversation)` | Leave a GC without deleting history. |
| `convo.get_rules(target)` | Read an existing conversation's MEMBERS.md and revision; publish scoped runtime guidance. |
| `convo.set_rules(target, content, revision)` | Replace/clear under human authorization using the previously read rules revision. |

These are display names; models call `convo__write`, for example. Selections use
`convo/write`. No additional tools are needed to resolve a write target.

`list` combines filters with AND. Names match exactly and case-sensitively, and
may yield multiple results. Members accept bare agent names or `agent:`/`human:`
IDs; duplicates and order do not matter. By default all requested members must
be present. `exact=true` requires the entire membership, including yourself
explicitly. Empty members and exact without members are errors; valid unknown
members return no matches.

`peek` and `write` take a bare agent name (your implicit DM) or an existing
canonical `dm_`/`gc_` ID. Canonical ID syntax wins over an agent name with the same
spelling. Unknown IDs never fall back to name lookup. Typed participants, pairs,
your own agent name, and conversation names are not write/peek targets. First DM
write creates and appends atomically; peeking at an absent DM fails without creation.

Messages contain only `{id, sender, message, reply_to}`. `id` is an application
UUID shared by write and peek, independent of backend stream IDs. `reply_to` is
the ID of the message being replied to; omit it for a new message. Backend stream
IDs and execution origin are not exposed by these tools. `peek` returns the latest
0–1000 retained messages in chronological order; zero returns metadata only. It
does not acknowledge, consume, or alter talk cursors. There is no recv tool.

Rename verifies the supplied revision before mutation and reports the captured
old name. Concurrent revisions fail; obtain fresh metadata with peek before
retrying. Create, join, leave, membership checks, and uncertain-delivery behavior
continue to use the existing Hub operations.

Agents with the talk driver enabled poll joined conversations in serial batches
of up to 20 entries through a user-provided `talk` handler, or the packaged
runtime Talk handler. The runtime handler takes precedence over an unnamed user
entry and runs even when the user declares no runnable. It uses the receiving
agent's normal resources and limits.
Each agent/conversation reuses one bound `talk_` thread, with a new root Run per
batch and a freshly resolved workdir. Old per-batch threads are not adopted;
an occupied deterministic ID with another/no binding is rejected.
Disabling talk leaves presence, transport, outbound tools, and observation active.
Batches contain fetched messages, conversation metadata, and their cursor range.
The bound thread retains its own history; `convo.peek` reads older conversation
messages. Talk may reply, ignore, steer/cancel an inspected local Run, or spawn
independent work in a new or idle existing local thread of any prefix. Ambiguous
control targets require clarification; foreign origins are not local Run targets.
Final output is a handling summary; replies use `convo.write`. Own messages and
handler-error notices do not trigger another handler. Agent origin records contain
context-derived thread/run IDs.

Source MEMBERS.md is checked before admission and each Model Call, including
processing children and explicit rerun. Spawned independent work gains no
implicit source scope; delegated requirements belong in input. In any thread,
peek/write/set_rules check the actual destination's current rules. Changed or
compacted-away guidance is recalled as runtime-owned `convo-rules`; an intercepted
operation does not execute and requires a new model decision. Reading guidance
alone does not bind a source. Rule changes take effect at subsequent boundaries.

A shared handler can live in `runnables/talk.too`, exporting an unnamed or
filename-matching agic or flow. Its primary input receives the JSON message
batch without the built-in fallback instructions. Private helpers and inline
caps stay in the same file; required models and services come from the receiving
environment. A public `talk` in `agent.too` conflicts with this export. Execution
records retain handling diagnostics and tool results; batches do not carry previous
messages, handling results, or reply receipts.

The default handling instructions belong to the runtime handler's `.too` Program;
the driver supplies only the JSON batch. Run records capture the runtime source
under a distinct qualified identity for inspection and restart. No global
runnable configuration selects a different messaging entry.

An independent heartbeat renews a backend presence deadline. Expired leases
grant no authority even before cleanup; graceful stop releases the lease. Hub
reconciles expiry independently of discovery. Readers compare deadlines with
their own clocks without writing backend state; heartbeat renewal changes only
the deadline. Hub scans resident homes at startup and periodically. Two successful scans
confirming absence plus no live lease remove an agent from the roster and ordinary
GCs. DM membership and all historical data remain. A live process with a missing home
stays visible. A recreated name retains its DM identity and history.

Streams retain approximately 10,000 entries. Readers use independent full Stream-ID
cursors and report retention gaps. Uncertain sends report their UUID without
retrying; check history before resending. Talk handler cursors live in the backend,
independently per agent and conversation.
A missing cursor starts at `0-0`. An admitted batch's exact input, range, and Run
are committed together locally. Terminal outcomes, including failures/cancellation,
advance progress; busy admission and pre-admission failures retain the trigger.
Malformed/own-message-only ranges may advance without a Run. Cursor writes require the
current lease and membership, cannot move backwards or past the stream tail, and
can safely repeat the same position. Transient save failures retain pending
progress and reconcile it with the backend before another write; confirmed or newer
progress clears the pending state without rerunning the handler. Cursor-save errors
during cancellation preserve the cancellation signal. Restart reads admitted
ranges and their Run results before reading newer messages; it does not repair
Run state or replay effects. Task startup failure after admission is finalized by
the executor before it reports the error, preserving the committed batch receipt.
Pending/running receipts leave the cursor unchanged without sending a failure notice.
Failure notices are best effort, attempted at most once per batch. Uncertain sends
are not automatically repeated. There is no atomic transaction across Hub/SQLite.
Backend changes discard in-memory progress from the previous backend.
Cursors have no TTL and survive shutdown and membership departure. Humans and
ordinary message reads do not advance them.

Agent-only Hub routes `GET` and `PUT /agents/{agent}/talk/cursors/{convo_id}`
read and advance talk progress using `{"cursor": "<stream-id>"}`. Both require an
active agent lease and conversation membership. The existing
`GET .../msg/conversations/{convo_id}/cursor?after=...` only checks retained history.

## Storage, events, and statistics

The backend separates authoritative data by purpose:

| Data | Authority |
| --- | --- |
| Team directory | Typed identities, public metadata, and private leases |
| Agent roster | Root ownership and discovery state; no duplicate presence or names |
| Presence | Lease deadlines, independent of discovery |
| Conversations | Metadata, membership, messages, rules, and nonunique name indexes |
| Talk cursors | Persistent agent/conversation consumption progress |
| Execution events | Retained feed, byte accounting, projections, and recovery generations |

Discovery atomically creates directory/roster entries. Two confirmed absent
scans and no live lease remove an agent and its GC memberships, preserving DMs
and history. Expiry alone changes presence. Another root cannot adopt a root
claim. Raw leases remain private; responses expose metadata and deadlines.

Participant pairs resolve DMs directly. Conversation/message counters commit
with their records. Active execution-event generations persist; staging
generations expire. [Events](events.md#hub-subscriptions) owns their publication
and recovery protocol.

`GET /team/events` returns an initial checkpoint before snapshots are loaded, then
changes and checkpoints. Resume using `?after=t1.<epoch>.<stream-id>`. A trimmed,
ahead, or previous-epoch cursor emits `resync_required` and closes; reconnect
without a cursor and reload snapshots. Independent readers do not consume each
other's events. Agents receive only visible conversation changes plus their own
removal; each replay batch rechecks their live lease. Invalid cursors return HTTP
400. Failures after streaming starts emit `stream_error` and close without
advancing past unread events; an expired or replaced lease uses
`code: recovery_required`. Rename, messages, and heartbeat renewal emit no team
events; clients refresh those snapshots themselves. The stream retains approximately 100,000
entries. There is no separate event metadata key.

Conversation statistics use native Stream `entries-added` for
accepted appends and `length` for retained messages; trimming does not reduce
lifetime totals. Empty message streams count as zero. Global conversation totals
include the system GC.

Python defines and validates persisted records and public responses. Lua performs
atomic storage checks and combined writes, comparing validated snapshots before
mutation. A changed snapshot is reread within a fixed retry bound; uncertain
transport failures never replay writes. A pending Talk DM is local state with no
persisted creation time or revision.

Services use a backend contract rather than raw driver commands. Hub owns the
shared connection lifetime; the backend owns keys, atomic operations, and driver
error normalization.

Conversation lists read metadata, membership, and previews in batches of at most
128 IDs. Each batch applies visibility atomically. Listing remains linear in the
directory size; ID and participant-pair lookup are constant in directory size.
GC allocation supports 524,288 reservations per backend per hour; this is
capacity, not a measured throughput claim.
