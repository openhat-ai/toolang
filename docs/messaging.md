# Messaging

`too text`, `too team`, `coop`, and the agent message loop consume messaging settings resolved by `setup`. The default URL is `redis://localhost:6379/0`; no configuration is required for local Valkey. Start Valkey externally. Override defaults in the Toolang root (`~/.toolang/config.toml` by default):

```toml
[human]
name = "bryan" # Defaults to the OS username.

[messaging]
url = "redis://localhost:6379/0"
```

Optionally add custom memberships to each agent's `config.toml`:

```toml
[messaging]
groups = ["gc_dev"]
```

Set `[messaging].enabled = false` in root or agent configuration to opt out. Restart agents after configuration changes. Agents on different machines communicate by using the same reachable Valkey instance. Agent names must be unique there; human names cannot collide with agent names.

```sh
too start alice
too team                       # Public/custom groups, members, online agents, latest message.
too text alice                 # Owner DM; interactive input.
too text dev                   # Custom group gc_dev.
too text all hello             # Send and exit after acknowledgment.
too text alice -- hello -sdf    # Flags after the target are message text.
too text --dm alice hello      # Disambiguate an agent/group name collision.
too text --group alice hello
```

Interactive Text keeps a live input below ordinary scrollback. Enter sends; Ctrl+J inserts a newline; Ctrl+P/Ctrl+N browse sent input; Ctrl+Q exits. Failed sends preserve the draft. In tmux, each root/connection/human has a session and each conversation has a reusable window; switch windows to switch conversations. Use tmux copy mode for history. `TOOLANG_TMUX=0` keeps Text in the invoking pane.

Messages align within Chat's maximum content width (120 columns by default, configurable with `TOOLANG_PROGRESS_MAX_WIDTH`), capped by the current terminal width.

## Conversations and keys

| ID | Membership |
| --- | --- |
| `all` | Every registered agent and owner, automatically. |
| `dm_alice` | Alice and Alice's owner, automatically. |
| `dm_alice_bob` | Two registered agents; created on first use. |
| `gc_dev` | Agents declaring this custom group. Owners may inspect/send without joining. |

Names are case-sensitive. Encode each component as UTF-8 percent escapes except ASCII letters, digits, and `-`; `_` becomes `%5F`. Sort agent pairs by raw UTF-8 name before encoding. Use canonical IDs for names resembling reserved IDs.

| Key | Value |
| --- | --- |
| `too:agents` | Hash: agent → owner; retained while offline. |
| `too:groups` | Set of materialized conversation IDs. |
| `too:agent:<encoded-agent>:online` | Process token; 30-second TTL, renewed every 10 seconds. |
| `too:group:<id>:members` | Set of participant names. |
| `too:group:<id>:msg` | Stream; `data` contains `{id, sender, body, in_reply_to, origin}` JSON. |

## Agent handling

- Hosted agents poll every 500 ms, handling one batch of at most 20 entries at a time. Each group has independent context and a full Stream-ID cursor; groups rotate fairly.
- `agic:msg` receives JSON containing `agent`, `available_groups`, and `groups[{group, messages, previous}]`. Otherwise, the default agic receives handling instructions and that payload. Prior context includes up to 20 messages and the last result, send receipts, and summary.
- `coop/contacts` discovers agents and joined groups. `coop/send(group, body, in_reply_to?)` sends immediately. **Messaging core** owns UUIDs, validation, JSON, transport, and receipts; the toolset only supplies identity and arguments. Agent origins contain `{agent, run}`.
- Reply to the source group with `in_reply_to` set to the original message ID. Final model output is a handling summary, never automatically sent. Delegate longer work with spawn and give the worker the source group/message context.
- Own messages do not trigger another handler. Failed/malformed batches are logged and skipped; successful send receipts survive handler failure. Agent checkpoints live under `.runtime/channels/messaging/`; Text drafts/input history live under the root's `.runtime/text/`.
- Streams retain approximately 10,000 entries; Text initially shows up to 200. Readers reconnect from their own cursors and report retention gaps. An uncertain send reports its UUID without automatic retry; check history before resending. Delivery is not exactly-once or archival storage.

No comma shortcut, legacy-data migration, managed Valkey, authentication, event subscriptions, or coordination primitives are included.
