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
too text                            # Conversations, membership, presence, previews.
too text alice                      # Private conversation with a unique target.
too text agent:alice hello          # Send and exit after acknowledgment.
too text human:alex hello
too text group:dev                   # Existing custom group.
too text all hello                   # Public group:all.
too text alice -- hello -sdf         # Flags after target are literal message text.
too hub stop                        # Leaves agents and Redis/Valkey running.
```

`too hub serve` runs in the foreground. Hub binds `127.0.0.1`; clients discover its
actual endpoint and backend identity from the private root `.runtime/hub.json`
file. Calls assume trusted local clients; security authentication is deferred.
Start Hub before using Text or `msg` tools. Agents use it for messages, presence,
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
not change configuration. Restart to apply configuration changes; reopen Text
if its endpoint, backend, or human changes. Temporary agents keep their existing
port selection.

Targets use `agent:`, `human:`, or `group:`. IDs are case-sensitive Unicode
letters/numbers with combining marks and `-_.`; the first character must be a
letter/number. Bare names require a unique match; use a prefix to disambiguate.
Same-name agents and humans are distinct. Participant targets create one private
conversation per unordered pair; its stable `group:` ID appears in the directory.

Interactive Text requires a TTY: Enter sends, Ctrl+J inserts a newline,
Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q exits. Messages use terminal scrollback;
in tmux, each conversation has a reusable window. `TOOLANG_TMUX=0` keeps Text in
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

Presence expires after 30 seconds without renewal; membership survives disconnection.
Streams retain approximately 10,000 entries. Readers use independent full Stream-ID
cursors and report retention gaps. Uncertain sends report their UUID without
retrying; check history before resending. Local checkpoints live under the agent's
`.runtime/channels/messaging/`; Text drafts/history live under root `.runtime/text/`.

The [teaming plan](plans/teaming.md) defines keys, values, and delivery stages.
Subscriptions, activity commands, and coordination are subsequent work.
