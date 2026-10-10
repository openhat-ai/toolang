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
port = 7000
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
too hub status                      # Endpoint and backend readiness.
too talk                            # List Team and Convos, including IDs and names.
too talk alice                      # Empty human-agent DM until the first send.
too talk alice hello                 # Send and exit after acknowledgment.
too talk alice,bob                   # Observe an existing agent-agent DM.
too talk dm_6x89kwxn                 # Open an existing DM by ID.
too talk gc_rcpya1zw                 # Open an existing GC by ID.
too talk alice -- hello -sdf         # Flags after target are literal message text.
too hub stop                        # Leaves agents and Redis/Valkey running.
```

`too hub serve` runs in the foreground. Hub binds `127.0.0.1`; clients discover its
actual endpoint and backend identity from the private root `.runtime/hub.json`
file. Calls assume trusted local clients; security authentication is deferred.
Start Hub before using Talk or `msg` tools. Agents use it for messages, presence,
and event export. Stopping Hub leaves agents running; communication fails until
Hub returns. Background communication resumes without an agent restart; uncertain
message sends are never automatically repeated.
Isolated guests without access to this local Hub report unavailability.

`too hub status` reports `starting` while waiting for the backend;
`too hub stop --force` can stop a stalled startup. Missing or corrupt initialized
conversation data fails closed; Hub does not reset counters or rebuild it.
Upgrade Hub and clients together and select a fresh Redis/Valkey dataset for this
schema. Old datasets remain untouched; history, drafts, and checkpoints are not
migrated or imported.

Hub `start`/`serve` ports resolve as `--port` > `TOOLANG_HUB_PORT` >
`teaming.hub.port` > `7000`. Resident agent `start`/`serve` uses `--port` >
`TOOLANG_AGENT_PORT` > home `[api] port` > recorded/available `7001`–`7999`.
Explicit ports must be `1..65535`; an occupied explicit port fails. Overrides do
not change configuration. Restart to apply configuration changes; reopen Talk
if its endpoint, backend, or human changes. Temporary agents keep their existing
port selection.

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
listed with the initial name `all`; open its ID from the directory. Its membership
is maintained by Hub, while its name can change.

Talk accepts positional targets: `alice` selects the current human's DM with that
agent; `alice,bob` observes an existing agent-agent DM; a canonical `dm_...` or
`gc_...` selects that existing conversation. Missing agent-agent DMs and unknown
IDs fail without creation. Bare names always select agents, including `all`;
conversation names are directory labels, not CLI targets. Selector flags such as
`--dm` and `--group` are not supported. Arguments after the target are literal
message text.

Opening a missing human-agent DM keeps its ID and pair locally with empty history.
The first accepted send atomically creates the conversation and appends its first
message. Closing without sending writes no conversation, counters, or events.
Explicit creation, rename, and name lookup are available through API/tools;
ambiguous name lookup returns candidate IDs rather than choosing one.

## Interactive Talk

`too talk TARGET` resolves the canonical ID, loads conversation metadata, displays
retained history, and follows new messages using a Stream-ID cursor. A member sees
an input box with `write a message`; an observer sees messages without a composer.
The composer and focus update when GC membership changes, preserving unsent drafts.
Enter sends, Ctrl+J inserts a newline, Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q
exits. Interactive input requires a TTY. Supplying message arguments sends once
and exits with a receipt or error.

The footer has two-cell side insets: conversation on the left, complete canonical
ID in the center, and the viewer's login on the right. Examples for login `brice`:

| Conversation | Left | Center | Right | Sending |
| --- | --- | --- | --- | --- |
| Direct with `alice` | `@alice` | Its canonical `dm_...` | `brice` | Allowed |
| Direct between `alice` and `bob` | `@alice,bob` | Its canonical `dm_...` | `brice` | Read-only; `@` is dim |
| Group `dev`, three members including `brice` | `#dev(3)` | Its canonical `gc_...` | `brice` | Allowed |
| The same group viewed by a nonmember | `#dev(3)` | Its canonical `gc_...` | The viewer's login | Read-only; `#` is dim |

Only the permission marker dims; names and counts use normal foreground. Conversation metadata and team deadlines refresh in the receive loop. The interactive footer does not display presence. The separate
`too talk` directory shows Team and Convos with IDs, names, participants, the current
agent presence snapshot, and message previews. It runs once without requiring a TTY.
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
The input, footer, and messages share the content limit of 120 cells by default,
configurable with `TOOLANG_PROGRESS_MAX_WIDTH` and capped by the terminal width.

## Terminal titles and tmux

Talk publishes the compact label (`@alice`, `@alice,bob`, or `#dev`) through OSC 0
and clears it on exit. iTerm2 and tmux can display it according to terminal settings.
The title is a display label; the footer retains the canonical ID for copying.

All Talk windows in one tmux server use the single session named `talk`. Windows
are reused by canonical conversation ID and connection context, including root,
backend, viewer, and Hub endpoint. Manual window renaming does not affect lookup.
A new window starts with the canonical ID as its name. Existing windows and shells
are preserved. `TOOLANG_TMUX=0` keeps Talk in the invoking terminal.

| Option | Scope | Value |
| --- | --- | --- |
| `@toolang_talk` | Session | `talk` |
| `@toolang_convo` | Window | Canonical conversation ID |
| `@toolang_context` | Window | Connection-context hash |
| `@toolang_pad` | Pane | `talk` |

`convo` abbreviates conversation. The marks support window and pane lookup;
OSC titles describe the displayed conversation.

## Agent messaging

| Tool | Purpose |
| --- | --- |
| `msg/targets()` | Discover participants and joined conversations, including current metadata revisions for renaming. |
| `msg/send(target, body, in_reply_to?)` | Send immediately and return a receipt. |
| `msg/create_conversation(name?, participants?)` | Create a GC containing the caller, or explicitly create/reuse a two-participant DM. |
| `msg/rename_conversation(conversation, name, revision)` | Rename or clear the name with revision checking. |
| `msg/join_conversation(conversation)` | Join a GC. |
| `msg/leave_conversation(conversation)` | Leave a GC without deleting its history. |
| `msg/resolve(target, by_name=false)` | Resolve an ID/participant or explicitly look up a conversation name with `by_name=true`. |

Enabled agents poll joined conversations in serial batches of up to 20 entries through
`agic:msg`, or their default agic. Previous messages, handling results, and send
receipts provide context. Final model output is a summary; replies use `msg/send`.
Own messages do not trigger another handler. Failed/malformed batches are logged
and skipped. Agent origin records contain context-derived thread/run IDs.

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
retrying; check history before resending. Local checkpoints live under the agent's
`.runtime/channels/messaging/` in the `v2-<backend-identity>.json` file; Talk
drafts/history live under root `.runtime/talk-v2/`.

## Storage, events, and statistics

With `P = too:teaming:v1`, the messaging key spaces are:

| Key | Type | Contents |
| --- | --- | --- |
| `P:team` | Hash | Typed member ID → display name, owner, creation time, private lease JSON |
| `P:roster` | Hash | Agent ID → root ownership/discovery JSON (`root`, `managed`, `missing`) |
| `P:team:presence` | ZSet | Agent ID → lease deadline in Unix milliseconds |
| `P:team:events` | Stream | `data` → versioned team, conversation membership, or presence change JSON |
| `P:convos` | Hash | Conversation ID → kind, name, creator, timestamps, revision JSON |
| `P:convo:C:members` | Set | Authoritative typed participants |
| `P:convo:C:messages` | Stream | `data` → message JSON |
| `P:convo:name:N` | Set | IDs matching an exact name; `N` is unpadded base64url UTF-8 |
| `P:convo:schema` | String | Schema version `2` |
| `P:convo:id:gc` | Hash | Last allocated hourly tick and sequence |
| `P:convo:stats` | Hash | DM count, GC count, accepted message count |
| `P:convo:system` | Hash | `all` → system GC ID |

`P:team` is the global member directory. `P:roster` is an optional agent-only
subset that records which root manages discovery. It does not duplicate names,
owners, leases, or online state. Humans have no roster entry; unscoped agent
registration may have only a team entry. A scoped transient registration has
`managed: false, missing: 0`; resident discovery uses `managed: true` and counts
successful absent scans. Root claims cannot be adopted by another root.

Discovery creates roster/team entries atomically. Cleanup after two absent scans
and no live lease removes both entries and GC memberships, preserving DM
membership and history. Expiry alone only changes presence. `P:activity:*` and
execution `P:events:*` keep their separate roles.
Raw leases stay private; team responses expose only public metadata and deadlines.

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
`msg` tool outputs keep their conversation record shape.

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
use the `Backend` contract and its `events` and `activity` capabilities, sharing
one connection lifetime owned by Hub. Activity federation and subscription logic
remain outside the driver. The interface exposes explicit lease renewal/release
and normalized message field maps, without raw driver commands.

Conversation lists read metadata, membership, and previews in batches of at most
128 IDs. Each batch applies visibility atomically. Listing remains linear in the
directory size; ID and participant-pair lookup are constant in directory size.
GC allocation supports 1,048,576 reservations per backend per hour (about 291/s
averaged over an hour); this is capacity, not a measured throughput claim.

The [conversation contract](plans/conversation-ids.md) specifies record JSON,
atomic writes, event payloads, ID capacity, and fresh-dataset rollout.

## Activity views

`too top` requires Hub and observes the team; `too AGENT top` reads local agent
history without requiring Hub or a running executor. Header shows scope totals,
local time and window settings; Table has Agent/Thread/Run levels, and optional
Details sits above a fixed Status bar. `--refresh SECONDS` defaults to `0.1`;
keys repaint immediately. Source updates carry committed activity and elapsed
time independently for each agent. `--once` prints one snapshot.

Period defaults to the current/latest executor session. `--since DURATION` is a
rolling window; a timezone-aware timestamp selects a fixed start, and `all`
includes available history. MODEL, TOOL, IN, CACHED, OUT, SPEND and TIME+ share
that range; CACHED is cache-read input already included in IN. Recent
(`--recent DURATION|all`, default `30m`) independently controls visibility.
F5 displays the next view's name (Agents/Threads/Runs/Tree); F7 Recent and F8 Period
cycle the corresponding Header ranges. Both modes show the AGENT column;
the agent Header shows live session uptime independently of TIME+. The status bar
shows only function keys; F10 exits, and existing letter/Enter shortcuts remain
available through F1 Help. `--sort spend` accepts `cost` as an alias.
Enter opens full IDs, exact usage and Markdown results; Ctrl-P/N selects rows,
and PgUp/PgDn pages Details. Unavailable historical usage remains unknown.

The [Talk presentation plan](plans/talk-status-bar.md) records the original layout;
the [conversation contract](plans/conversation-ids.md) supersedes its IDs and targets.
[Team observation](plans/team-observation.md) and [activity views](plans/top-activity.md)
define Hub event subscriptions and `too top`.
