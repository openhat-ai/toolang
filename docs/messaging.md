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
- Input is JSON text: `{agent, groups: [{group, messages, previous: {messages, result}}]}`. Messages retain `id`, `sender`, `body`, `in_reply_to`, `origin`, and gain `stream_id`. Previous context stays within its group and keeps the latest 20 messages plus its last result.
- Return JSON `{replies: [{group, body, in_reply_to}], noted: {group: summary}}`; `noted` and `in_reply_to` are optional. `replies: []` means no reply. Replies are appended directly to their target group with the agent as sender and the handling run in `origin`. Any joined group can be a reply target. All reply shapes and memberships are checked before delivery.
- Custom handler example (the primary input may also be `Text` or `Part[]`):

```toolang
agic msg(_: Json) -> Json:
  Handle {{_}}. Return {"replies": [{"group": "target group", "body": "reply"}]}.
  Return {"replies": []} when no reply is needed. Ignore your own messages.
```

Cursors/context live under the agent's `.runtime/channels/messaging/`, separately per connection URL. Handling failures are recorded and skipped without retry; connection failures are logged and polled again. Graceful shutdown cancels unfinished handling and records it as skipped. Crashes can repeat a batch or a reply; delivery is not exactly once. Replies use approximate `MAXLEN 10000`; external writers should also bound retention. Reset the local checkpoint after resetting the Valkey Streams. This version does not auto-deliver spawned-run output or add messaging tools, APIs, group discovery, events, semaphores, or boards.
