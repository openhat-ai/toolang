# Messaging

Teaming connects agents through Hub HTTP APIs. Only Hub connects to an externally
running Redis or Valkey server. Local execution needs neither Hub nor that server.
`too` is an alias for `toolang`.

Root `<root>/config.toml` (normally `~/.toolang/config.toml`):

```toml
[teaming]
human = "brice" # Defaults to the OS username.

[teaming.backend]
url = "redis://localhost:6379/0"

[teaming.hub]
port = 7001
```

Teaming is enabled by default. Agents reconnect when Hub becomes available;
local execution and subscriptions work without it. To opt out, set
`<agent-home>/config.toml` and restart the agent:

```toml
[teaming]
enabled = false
```

Root settings cannot appear in home configuration; `enabled` is home-only.
Membership is stored only in the backend, never in configuration.

```sh
too hub start                       # Background Hub; requires Redis/Valkey.
too hub                             # Hub readiness and agent instance snapshot.
too talk                            # List conversations, previews, and human membership.
too convo list                      # The same conversation directory.
too convo create dev                # Create a group containing the configured human.
too convo join gc_rcpya1zw           # Join an ordinary GC; leave preserves rules/history.
too talk alice                      # Empty human-agent DM until the first send.
too talk alice hello                 # Send and exit after acknowledgment.
too talk alice,bob                   # Observe an existing agent-agent DM.
too talk dm_6x89kwxn                 # Open an existing DM by ID.
too talk gc_rcpya1zw                 # Open an existing GC by ID.
too talk alice -- hello -sdf         # Flags after target are literal message text.
too hub stop                        # Leaves agents and Redis/Valkey running.
```

`too hub` prints one Hub and agent snapshot. It includes installed resident
agents and discoverable active or unresolved visiting, roaming, and temporary
instances; finished nonresident instances and history are excluded. The Hub
endpoint is omitted. A ready Hub displays `hub <version>` (or `hub running` if
its version is unknown); otherwise it displays `hub not running`. Agents appear
in a headerless three-column tree: name, status, and Talk. A blank line and
summary follow a nonempty list, counting instances, confirmed running instances,
and running instances with the recorded `talk` driver. An empty list displays
`no agents` instead of the counts. The tree is a roster, not a connectivity or
message delivery indicator. Status and Talk use the inspected launch record,
not edited configuration or current Hub presence. Negative agent states (`not
running`, `disabled`, `-`) are dim; the Hub line, names, and positive states
remain normal. Incomplete inspection marks a nonempty summary `partial` and
prints diagnostics after the list, even when empty. Large rosters continue in
terminal scrollback. The snapshot is one-shot and does not start Hub or agents.

`too hub serve` runs in the foreground. Hub binds `127.0.0.1`; clients discover its
actual endpoint and backend identity from the private root `hub/hub.json`
file. Calls assume trusted local clients; security authentication is deferred.
`GET /info` reports the Toolang source `version` and `api_version: 0`;
`HUB_API_VERSION` also owns publication, staging and event wire versions.
Start Hub before using Talk or `convo` tools. Agents use it for messages, presence,
and event export. Stopping Hub leaves agents running; communication fails until
Hub returns. Background communication resumes without an agent restart; uncertain
message sends are never automatically repeated. Isolated guests without access to
this local Hub report unavailability.

Internally, a live Hub remains `starting`, `running`, or `unavailable` based on
readiness; the snapshot shows a version only when ready, and `not running`
otherwise. `too hub stop --force` can stop a stalled startup. Missing or corrupt initialized conversation data fails
closed; Hub does not reset counters or rebuild it. Upgrade Hub and clients together
and select a fresh Redis/Valkey dataset for hub store schema `4`, which owns both
messaging and event storage. Older schemas are rejected without
mutation. Preserve the old runtime/data before switching datasets. Old datasets
remain untouched; history, drafts, and checkpoints are not migrated or imported.

Hub `start`/`serve` ports resolve as `--port` > `TOOLANG_HUB_PORT` >
`teaming.hub.port` > `7001`. Resident agent `start`/`serve` uses `--port` >
`TOOLANG_AGENT_PORT` > home `[api] port` > recorded/available port. Automatic
allocation scans `7002`–`7999`. A recorded agent port of `7001` is not reused
without an explicit override; its next default start selects a new port.
Explicit ports must be `1..65535`; an occupied explicit port fails. Overrides do
not change configuration. Restart to apply configuration changes; reopen Talk
if its endpoint, backend, or human changes. Temporary agents keep their existing
port selection.

When upgrading, stop an agent using `7001` and restart it without an explicit
port before restarting Hub on its new default. Running processes and saved
configuration are not moved automatically. An explicit Hub port of `7000`
continues to use `7000`; remove it to adopt the new default.

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
joined conversations. Sending and renaming require membership. The system GC is
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

`too convo` manages conversations as the configured human. `list` shares Talk's
directory and `JOINED` indicator. `create [NAME]` creates a GC containing the human;
`join <id>` / `leave <id>` change ordinary GC membership. DM/system membership is
fixed. Leaving preserves rules and history; deletion is not supported.

Hub owns one optional `MEMBERS.md` per conversation, separate from workspace
`AGENTS.md`. Human observers may read/edit it without joining. Agents require
membership; writes also require a live lease and human authorization
covering the change. Rules, forwarded agent text, and quoted requests cannot grant
that authorization. System-conversation rules apply only in that conversation.

```sh
too convo rules gc_rcpya1zw                 # Markdown on stdout, revision on stderr
too convo rules gc_rcpya1zw -o MEMBERS.md   # Download to a path chosen by the user
too convo rules gc_rcpya1zw -u MEMBERS.md --revision REVISION
too convo rules gc_rcpya1zw -u --revision REVISION < MEMBERS.md
```

`-o/--output` requires a path; `-` selects stdout. `-u/--update` accepts an optional
path; omitted or `-` reads stdin. They are mutually exclusive. Piped input without
`-u` remains a read. Editing and file locations are user-owned; there is no local
association, sidecar, synchronization, or editor integration.

Reads preserve exact UTF-8 Markdown up to 64 KiB. Missing rules are a successful
empty read, with `No rules set.` and revision `0` on stderr; a requested file becomes
empty. Failed reads preserve the destination. Empty uploads clear rules. A nonempty
document's revision is its lowercase SHA-256 digest. Upload `REVISION` is the prior
read version; Hub compares it atomically and computes the new version. Stale uploads
fail without mutation and require a fresh read and an explicit resubmission decision.

Human `GET` / `PUT /msg/conversations/{id}/rules` and agent equivalents under
`/agents/{agent}/msg` return `{conversation, filename, revision, content}`. PUT accepts
`{content, revision}`; conflicts are HTTP 409 / `conversation_rules_conflict`. Content
and revision are read together; corrupt storage is an error. Agent message sends
may include transport-only `rules_revision`, checked atomically with append and
first-DM creation; the runtime supplies it for `convo.write`. Human and diagnostic
sends retain their existing contracts.

## Interactive Talk

`too talk TARGET` resolves the canonical ID, loads conversation metadata, displays
retained history, and follows new messages using a Stream-ID cursor. A member sees
an input box with `Type a message`; an observer sees messages without a composer.
Talk requests a blinking block cursor and resets the cursor shape to the
terminal default on exit.
The composer and focus update when GC membership changes, preserving unsent drafts.
Enter sends, Ctrl+J inserts a newline, Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q
exits. Interactive input requires a TTY. Supplying message arguments sends once
and exits with a receipt or error.

A startup panel appears once before messages, with the local `Talk <version>`
as its caption and these rows:

```text
hub    v0.4.0a2
convo  gc_rcpya1zw
user   bryan
```

Only observers append ` · view only` to the user, in the same normal style.
The Hub version comes from `GET /info`; unsupported, failed, or malformed
inspection displays `unknown` after at most two seconds. The panel is an entry
snapshot that scrolls away; reconnects and Ctrl+L do not reprint it. It shares
Chat's responsive layout and uses the message width limit. Directory and
one-shot sends do not show it or request version metadata.

The footer has two-cell side insets: conversation on the left, complete canonical
ID in the center, and the viewer's login on the right. Examples for login `brice`:

| Conversation | Left | Center | Right | Sending |
| --- | --- | --- | --- | --- |
| Direct with `alice` | `@alice` | Its canonical `dm_...` | `brice` | Allowed |
| Direct between `alice` and `bob` | `@alice,bob` | Its canonical `dm_...` | `brice` | Read-only; `@` is dim |
| Group `dev`, one human and two agents, one agent online | `#dev(2/3)` | Its canonical `gc_...` | `brice` | Allowed |
| The same group viewed by a nonmember | `#dev(2/3)` | Its canonical `gc_...` | The viewer's login | Read-only; `#` is dim |

Only the permission marker dims; names and counts use normal foreground.
Conversation metadata refreshes in the receive loop. GC presence refreshes
independently, using Hub team deadlines and presence notifications with a
half-second fallback and a one-second lookup timeout. Human members always count
as online; an agent requires a live Hub lease. Only this conversation's members
count. Agent presence starts unknown and becomes unknown after lookup failures,
displayed as `#dev(?/3)`; message following and sends continue. Empty groups show
`(0/0)` and human-only groups show `(m/m)`. The separate `too talk` directory
lists conversations with CONVERSATION, NAME, MEMBERS, UPDATED, and MESSAGE
columns, followed by a count. It runs once without requiring a TTY.
On narrow terminals, the footer prioritizes the login and hides a canonical ID
that cannot fit intact. Initial connection shows `Connecting…` on the right.

Errors replace the entire footer with Chat's red `!` row and the actual detail.
Failed sends preserve drafts and are not retried automatically. The user decides
whether to retry or reopen. Transient read failures show `Reconnecting…` in the
error row and resume from the committed cursor; terminal read errors stop the
receive loop. Reopening resolves the current
Hub configuration and conversation metadata.

Messages use terminal scrollback. Agent messages share Chat's Markdown renderer.
Their names and markers share a stable ANSI color and a header above the body;
a faint dashed rule separates agent messages. Human messages are literal text.
Messages use `TOOLANG_PROGRESS_MAX_WIDTH`, defaulting to 120 terminal cells.
`TOOLANG_INPUTBOX_MAX_WIDTH` independently limits the input and footer; when
unset, it falls back to the progress limit. Both settings accept positive
integers and are capped by the terminal width. Empty or invalid values are
configuration errors. The input-only setting does not affect the directory or
one-shot sends. Ctrl+L preserves visible output in native scrollback and
places the input box at the top without a leading blank row. Its usual gap
returns when a new message or notice is displayed.

## Terminal titles and tmux

Interactive Talk stays in the invoking pane and publishes `@alice`, `@alice,bob`,
or `#dev` through OSC 0, refreshing when the conversation label changes. The
footer retains the canonical ID. Pane-local marks identify `kind=talk`, root,
`convo`, and the connection `context`; no sessions or windows are created or
renamed. Directory listings and send-only commands publish nothing. See
[terminal configuration and marks](cli.md#terminal-titles-and-pane-marks).

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
messages, handling results, or reply receipts. Custom handlers using `previous`
must switch to explicit history lookup.

The default handling instructions belong to the runtime handler's `.too` Program;
the driver supplies only the JSON batch. Run records capture the runtime source
under a distinct qualified identity for inspection and restart. No global
runnable configuration selects a different messaging entry.

An independent heartbeat runs every 5 seconds. Agent presence is a ZSet deadline
in backend Unix milliseconds, renewed to at least 15 seconds ahead. Expired
leases grant no authority even before cleanup; graceful stop releases the lease.
Hub owns a cancellable presence loop, reconciling up to 128 due agents per batch
and checking every second. Heartbeats update only the score. Readers compare
returned deadlines with their own clocks and correct their local view without
writing to the backend. No last-seen timestamp is stored or displayed.
Hub scans resident homes at startup and every 5 seconds. Two successful scans
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

Agents no longer create `.runtime/channels/` cursor files. Existing local files
are ignored without import or cleanup. Stop writers and reinitialize development
backend data for the renamed team member key; old backend data is not migrated.
Human Talk input history remains under `<root>/human/talk-input/`.

Agent-only Hub routes `GET` and `PUT /agents/{agent}/talk/cursors/{convo_id}`
read and advance talk progress using `{"cursor": "<stream-id>"}`. Both require an
active agent lease and conversation membership. The existing
`GET .../msg/conversations/{convo_id}/cursor?after=...` only checks retained history.

## Storage, events, and statistics

With `P = too:teaming:v1`, the messaging key spaces are:

| Key | Type | Contents |
| --- | --- | --- |
| `P:team:members` | Hash | Typed member ID → display name, owner, creation time, private lease JSON |
| `P:roster` | Hash | Agent ID → root ownership/discovery JSON (`root`, `managed`, `missing`) |
| `P:team:presence` | ZSet | Agent ID → lease deadline in Unix milliseconds |
| `P:team:events` | Stream | `data` → versioned team, conversation membership, or presence change JSON |
| `P:convos` | Hash | Conversation ID → kind, name, creator, timestamps, revision JSON |
| `P:convo:C:members` | Set | Authoritative typed participants |
| `P:convo:C:messages` | Stream | `data` → message JSON |
| `P:convo:C:rules` | Hash | `document` → validated MEMBERS.md content/revision JSON; absent means no rules |
| `P:convo:name:N` | Set | IDs matching an exact name; `N` is unpadded base64url UTF-8 |
| `P:convo:schema` | String | Hub store schema version `4` |
| `P:convo:id:gc` | Hash | Last allocated hourly tick and sequence |
| `P:convo:stats` | Hash | DM count, GC count, accepted message count |
| `P:convo:system` | Hash | `all` → system GC ID |
| `P:agent:N:talk` | Hash | Conversation ID → talk handler cursor; `N` is the agent name |
| `P:events:meta` | Hash | Version, epoch, tail, retention floor, byte count, catalog revision; `pending` fences interrupted publication |
| `P:events:stream` | Stream | `kind`, `agent`, `data` fields for execution events and recovery transitions |
| `P:events:agents` | Hash | Agent ID → publication/recovery state, generation pointers, source cursor, last operation receipt |
| `P:events:agent:N:G` | Hash | Encoded entity identity → versioned entity JSON; `["manifest"]` → generation bounds and baseline |

`P:team:members` is the global member directory. `P:roster` is an optional agent-only
subset that records which root manages discovery. It does not duplicate names,
owners, leases, or online state. Humans have no roster entry; unscoped agent
registration may have only a team entry. A scoped transient registration has
`managed: false, missing: 0`; resident discovery uses `managed: true` and counts
successful absent scans. Root claims cannot be adopted by another root.

Discovery creates roster/team entries atomically. Cleanup after two absent scans
and no live lease removes both entries and GC memberships, preserving DM
membership and history. Expiry alone only changes presence. Execution
`P:events:*` metadata retains its separate publication role.
Raw leases stay private; team responses expose only public metadata and deadlines.

Conversation names are nonunique secondary indexes; member pairs resolve DMs
directly by ID. Membership sets and presence deadlines each have one authority.
Global conversation/message counters change in the same transaction as their
records; per-conversation message counts come from the Stream itself. Execution
event metadata remains separate because its feed needs byte accounting and
generation recovery. Active event generations persist, while staging generations
expire after 60 seconds.

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

Messaging routes use `/msg/conversations`, `/{id}`, and `/participants`,
`/messages`, `/cursor`, `/stats` subresources. `GET /msg/stats` exposes human-only
global totals. Python `HubClient` and `AgentClient` creation, lookup, and rename
methods all return a validated `Conversation`; use attributes such as `.id`
and `.participants`, or `dataclasses.asdict()` when a dictionary is needed.
Creation and rename no longer return untyped dictionaries. HTTP JSON fields and
`convo.create`, `convo.peek` metadata, and `convo.rename` retain this conversation record shape. Directory and message tools use the compact shapes above.

Conversation statistics use native Stream `entries-added` for
accepted appends and `length` for retained messages; trimming does not reduce
lifetime totals. Empty message streams count as zero. Global conversation totals
include the system GC.

Python defines and validates persisted records and public responses. Lua performs
atomic storage checks and combined writes, comparing validated snapshots before
mutation. A changed snapshot is reread within a fixed retry bound; uncertain
transport failures never replay writes. A pending Talk DM is local state with no
persisted creation time or revision.

Storage interfaces live in `teaming/backend/protocol.py`; the factory selects
`backend/valkey/`, which owns all keys, commands, Lua, and driver errors. Services
use the `Backend` contract and its execution `events` capability, sharing one
connection lifetime owned by Hub. Within Valkey, `keys.py` owns storage addresses;
`scripts.py` and `event_scripts.py` own atomic operations, sharing lease guards
and exact Stream-ID ordering through `lua.py`. Team record validation is shared
across messaging and execution publication in Python; Lua rechecks the validated
bytes and the current lease before mutation.
The interface exposes explicit lease renewal/release
and normalized message field maps, without raw driver commands.

Conversation lists read metadata, membership, and previews in batches of at most
128 IDs. Each batch applies visibility atomically. Listing remains linear in the
directory size; ID and participant-pair lookup are constant in directory size.
GC allocation supports 524,288 reservations per backend per hour; this is
capacity, not a measured throughput claim.

## Implementation evidence

- [Schemas](../src/toolang/teaming/schemas.py) own record JSON, validation, and API versions; [IDs](ids.md) owns identity rules and capacity.
- [Backend keys](../src/toolang/teaming/backend/valkey/keys.py) and [atomic operations](../src/toolang/teaming/backend/valkey/scripts.py) own storage and mutations.
- [Team events](../src/toolang/teaming/team_events.py) and [execution subscriptions](events.md#hub-subscriptions) own their separate cursor/recovery protocols.
- [Protocol tests](../tests/unit/messaging/test_protocol.py), [record validation](../tests/unit/messaging/test_contract_records.py), and [conversation IDs](../tests/unit/messaging/test_conversation_ids.py) verify these contracts offline.
