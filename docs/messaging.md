# Messaging

Teaming connects agents through an externally running Redis or Valkey server.
Ordinary agent execution needs neither server. `too` is an alias for `toolang`.

Root `<root>/config.toml` (normally `~/.toolang/config.toml`):

```toml
[teaming]
human = "brice" # Defaults to the OS username.

[teaming.backend]
url = "redis://localhost:6379/0"
```

Enable each participating agent in `<agent-home>/config.toml`, then restart it:

```toml
[teaming]
enabled = true # Defaults to false.
```

Root settings cannot appear in home configuration; `enabled` is home-only.
Membership is stored only in the backend, never in configuration. Experimental
`[human]`, `[messaging]`, `coop`, and old conversation IDs have no compatibility
aliases or data migration.

```sh
too text                            # Conversations, membership, presence, previews.
too text alice                      # Private conversation with a unique target.
too text agent:alice hello          # Send and exit after acknowledgment.
too text human:alex hello
too text group:dev                   # Existing custom group.
too text all hello                   # Public group:all.
too text alice -- hello -sdf         # Flags after target are literal message text.
```

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
Hub, subscriptions, activity commands, and coordination are subsequent work.
