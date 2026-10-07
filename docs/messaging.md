# Group messaging

Start Valkey externally. In the agent's `config.toml`, opt in with:

```toml
[messaging]
url = "redis://localhost:6379/0"
groups = ["h_alice", "g_dev"]
```

The root `config.toml` can supply defaults; agent values override them. Restart the agent after changes. `too alice start` starts the loop (`too` is an alias for `toolang`). Embedded script execution does not start it.

Provision membership and send messages using ordinary Valkey commands:

```sh
valkey-cli SADD too:group:h_alice:members alice owner
valkey-cli XADD too:group:h_alice:msg '*' data '{"id":"hello-1","sender":"owner","body":"Hello","in_reply_to":null,"origin":null}'
```

- Keys: `too:group:G:members` is a Set; `too:group:G:msg` is a Stream with one `data` JSON field. Use the [group naming rules](plans/agent-communication.md). Membership must exist before reading or replying; this is a functional check, without authentication.
- Every 500 ms while idle, read configured groups independently by cursor. Each batch has at most 100 entries, at most 20 per group, with rotating group order. Own messages only provide context. No consumer groups or background intake queue.
- Call `agic:msg` when present; otherwise invoke the configured default agic or unnamed agic with built-in handling instructions. The agent must have one of these entries. Each batch gets a fresh local thread/run using normal executor limits, tools, and persistence.
- Input is JSON text: `{agent, available_groups: [{group, members}], groups: [{group, messages, previous: {messages, result}}]}`. The directory includes configured, joined groups even when idle or beyond the batch limit. Messages retain their fields and gain `stream_id`; previous context keeps the latest 20 messages and handling result within each group.
- `coop/contacts()` refreshes the directory. `coop/send(group, body, in_reply_to?)` immediately sends to a joined group and returns a receipt. The tool validates membership and creates the UUID, sender, origin agent/run, and JSON envelope. Both tools use `[messaging]`, respect tool allow rules, and work in ordinary and spawned runs; unconfigured or unavailable Valkey fails the call. Model-facing names are `coop__contacts` and `coop__send`.
- **Handler migration:** call `coop/send` for every message; returning `{replies: ...}` no longer sends anything. Final text is only a handling summary. Successful send receipts are preserved even if later handling fails; no successful sends are replayed by the loop. A single-group summary is included in that group's next batch; multi-group summaries remain in the run record. The default handler routes directly from the directory and prefers spawning substantial work without waiting; the worker must call `coop/send` itself to deliver results.
- Custom handler example (the primary input may also be `Text` or `Part[]`):

```toolang
agic msg(_: Json):
  Handle {{_}}. Use coop/send to send messages to available_groups.
  Ignore your own messages. Finish with a brief handling summary.
```

Cursors/context live under the agent's `.runtime/channels/messaging/`, separately per connection URL. Handling failures are recorded and skipped without retry; connection failures are logged and polled again. Graceful shutdown cancels unfinished handling and records it as skipped. Crashes or uncertain network outcomes can repeat delivery; there is no exactly-once guarantee. Sends use approximate `MAXLEN 10000`; external writers should also bound retention. Reset the local checkpoint after resetting the Valkey Streams. This version does not auto-deliver final run output or add messaging APIs, group discovery, events, semaphores, or boards.
