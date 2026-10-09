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
too talk                            # Conversations, membership, presence, previews.
too talk alice                      # Private conversation with a unique target.
too talk agent:alice hello          # Send and exit after acknowledgment.
too talk human:alex hello
too talk group:dev                   # Existing custom group.
too talk all hello                   # Public group:all.
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

## Conversations and targets

Talk is the messaging interface for a **conversation**. [Chat](chat.md) runs agent
work within an execution **thread**. Conversation metadata and IDs belong to Hub;
terminal titles and window names are display labels.

Targets use `agent:`, `human:`, or `group:`. IDs are case-sensitive Unicode
letters/numbers with combining marks and `-_.`; the first character must be a
letter/number. Bare names require a unique match. Use a typed target or place
`--dm`/`--group` before the target to disambiguate. Arguments after the target are
literal message text. Same-name agents and humans are distinct participants.

Both conversation kinds have canonical `group:<id>` IDs:

| Kind | Creation and membership |
| --- | --- |
| Direct | Targeting a participant automatically resolves the unique conversation for that pair. Its two participants are fixed. |
| Group | Explicitly created with `msg/create_group` or the Hub API; registered participants can join or leave. Hub maintains the public `group:all`. |

Sending requires membership. Human observers can read conversations without
joining. Use a custom group when a discussion needs additional participants.

## Interactive Talk

`too talk TARGET` resolves the canonical ID, loads conversation metadata, displays
retained history, and follows new messages using a Stream-ID cursor. A member sees
an input box with `write a message`; an observer sees messages without a composer.
Enter sends, Ctrl+J inserts a newline, Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q
exits. Interactive input requires a TTY. Supplying message arguments sends once
and exits with a receipt or error.

The footer has two-cell side insets: conversation on the left, complete canonical
ID in the center, and the viewer's login on the right. Examples for login `brice`:

| Conversation | Left | Center | Right | Sending |
| --- | --- | --- | --- | --- |
| Direct with `alice` | `@alice` | Its canonical `group:<id>` | `brice` | Allowed |
| Direct between `alice` and `bob` | `@alice,bob` | Its canonical `group:<id>` | `brice` | Read-only; `@` is dim |
| Group `dev`, three members including `brice` | `#dev(3)` | `group:dev` | `brice` | Allowed |
| The same group viewed by a nonmember | `#dev(3)` | `group:dev` | The viewer's login | Read-only; `#` is dim |

Only the permission marker dims; names and counts use normal foreground. Metadata
is loaded on entry. The interactive footer does not display presence. The separate
`too talk` directory shows the current agent presence snapshot with message previews.
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

## Activity views

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

The [Talk contract](plans/talk-status-bar.md) defines presentation and acceptance
checks. The [teaming plan](plans/teaming.md) defines messaging data and APIs.
[Team observation](plans/team-observation.md) and [activity views](plans/top-activity.md)
define Hub event subscriptions and `too top`.
