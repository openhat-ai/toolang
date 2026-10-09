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
Membership is stored only in the backend, never in configuration. Experimental
`[human]`, `[messaging]`, `coop`, and old conversation IDs have no compatibility
aliases or data migration.

```sh
too hub start                       # Background Hub; requires Redis/Valkey.
too hub status                      # Endpoint and backend readiness.
too talk                            # Conversations, membership, presence, previews.
too talk alice                      # Private conversation with a unique target.
too talk agent:alice hello          # Send and exit after acknowledgment.
too talk human:alex hello
too talk group:dev                   # Existing custom group.
too talk all hello                   # Public group:all.
too talk alice -- hello -sdf         # Flags after target are literal message text.
too hub stop                        # Leaves agents and Redis/Valkey running.
```

`too talk` replaces `too text` without a compatibility alias. Update command
invocations; targets, flags, conversation IDs, drafts, and input history are unchanged.

`too hub serve` runs in the foreground. Hub binds `127.0.0.1`; clients discover its
actual endpoint and backend identity from the private root `.runtime/hub.json`
file. Calls assume trusted local clients; security authentication is deferred.
Start Hub before using Talk or `msg` tools. Agents use it for messages, presence,
and event export. Stopping Hub leaves agents running; communication fails until
Hub returns. Background communication resumes without an agent restart; uncertain
message sends are never automatically repeated.
Isolated guests without access to this local Hub report unavailability.

`too hub status` reports `starting` while waiting for the backend;
`too hub stop --force` can stop a stalled startup. After an empty backend restart,
Hub restores the configured human's registration on the next request. Lost
messages and custom groups are not restored.

Hub `start`/`serve` ports resolve as `--port` > `TOOLANG_HUB_PORT` >
`teaming.hub.port` > `7000`. Resident agent `start`/`serve` uses `--port` >
`TOOLANG_AGENT_PORT` > home `[api] port` > recorded/available `7001`–`7999`.
Explicit ports must be `1..65535`; an occupied explicit port fails. Overrides do
not change configuration. Restart to apply configuration changes; reopen Talk
if its endpoint, backend, or human changes. Temporary agents keep their existing
port selection.

Targets use `agent:`, `human:`, or `group:`. IDs are case-sensitive Unicode
letters/numbers with combining marks and `-_.`; the first character must be a
letter/number. Bare names require a unique match; use a prefix to disambiguate.
Same-name agents and humans are distinct. Participant targets create one private
conversation per unordered pair; its stable `group:` ID appears in the directory.

Interactive Talk shows `write a message` in its empty input box and requires a
TTY: Enter sends, Ctrl+J inserts a newline,
Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q exits. Messages use terminal scrollback;
in tmux, each conversation has a reusable window. `TOOLANG_TMUX=0` keeps Talk in
the current pane. Drafts survive failed sends. Human observers can read private
conversations but cannot send unless they are one of the two participants.
Custom-group sends also require membership. Use `group:all` for a shared discussion.

| Tool | Purpose |
| --- | --- |
| `msg/targets()` | Discover participants and joined conversations. |
| `msg/send(target, body, in_reply_to?)` | Send immediately and return a receipt. |
| `msg/create_group(name)` | Create a custom group containing the caller. |
| `msg/join_group(group)` | Join an existing custom group. |
| `msg/leave_group(group)` | Leave without deleting its history. |

Enabled agents poll joined groups in serial batches of up to 20 entries through
`agic:msg`, or their default agic. Previous messages, handling results, and send
receipts provide context. Final model output is a summary; replies use `msg/send`.
Own messages do not trigger another handler. Failed/malformed batches are logged
and skipped. Agent origin records contain context-derived thread/run IDs.

Accepted event/activity reports renew presence; an independent heartbeat runs
every 5 seconds. Presence expires after 15 seconds; graceful stop releases it.
Hub scans resident homes at startup and every 5 seconds. Two successful scans
confirming absence plus no live lease remove an agent from the roster and ordinary
groups. DM mappings, DM membership and all historical data remain. A live process with a missing home
stays visible. A recreated name retains its DM identity and history.

Streams retain approximately 10,000 entries. Readers use independent full Stream-ID
cursors and report retention gaps. Uncertain sends report their UUID without
retrying; check history before resending. Local checkpoints live under the agent's
`.runtime/channels/messaging/`; Talk drafts/history live under root `.runtime/text/`.

`too top` observes the team; `too AGENT top` observes one running agent. The header
shows scope totals, the body shows Agent/Thread/Execution rows, and the bottom bar
contains navigation and Details. `--refresh SECONDS` defaults to `0.1`; keys repaint
immediately. SSE supplies committed activity and elapsed time independently for
each agent. `--once` prints one snapshot.

Stats defaults to the current executor session; `--since TIMESTAMP|DURATION|all`
changes the range for MODEL, TOOL, IN, CACHED, OUT, SPEND and TIME together. CACHED
is cache-read input already included in IN. `--recent DURATION|all` independently
controls visible activity (default `30m`). `--sort spend` accepts `cost` as an alias.
Enter opens full IDs, exact token counts and result text; PgUp/PgDn scroll Details.
Old records backfill token totals on startup; unavailable usage remains unknown.

The [teaming plan](plans/teaming.md) defines keys, values, and delivery stages.
