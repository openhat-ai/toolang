# Communication data model

Approved implementation scope: messaging only. One loop starts with the agent and calls `agic:msg` or a default handler. `coop/contacts` and `coop/send` use the existing tool mechanism; sending tools own the message envelope and immediate delivery, while final handler text is only a summary. The same tools work in spawned runs. Connect to externally started Valkey. No event/semaphore/board implementation or new plugin framework. See [messaging usage](../messaging.md). The remaining model is design-only.

`A/T/R/K` are agent/thread/root-run/resource IDs. `G` is a group ID. `P` is `too:agent:A` or `too:group:G`.

| Key / channel | Structure | Value |
| --- | --- | --- |
| `too:group:G:members` | Set | Member names |
| `too:group:G:msg` | Stream | `data` = message JSON |
| `too:agent:A:event` | Pub/Sub | Agent event JSON |
| `too:agent:A:event/thread:T` | Pub/Sub | Thread event JSON |
| `too:agent:A:event/thread:T/run:R` | Pub/Sub | Root-run event JSON, including synchronous children |
| `P:sem:K:config` | Hash | `capacity`, `lease_ms` as decimal integers |
| `P:sem:K:holders` | Sorted Set | Acquisition token → expiry in Unix milliseconds |
| `P:board:K` | Hash | Entry ID → entry JSON |

Values:

- Message: `{id, sender, body, in_reply_to, origin}`. Reply/origin may be null; origin preserves external source and original content. Group comes from the key.
- Event: `{id, agent, thread, run, type, tags, data}`. ID derives from agent/epoch/sequence; thread/run may be null. Child events carry their actual run ID. Data uses existing event serialization.
- Board entry: `{title, input, assignee, status, output, revision}`. Assignee is a member name or JSON null; status is `pending/running/finished`; input/output are JSON values. Revision starts at 1. Filter/sort entries in the reader.

Rules:

1. **Scope:** human/agent names are stable and unique within one Valkey instance. Agents access their own namespace and joined groups; owners can inspect owned agents. Management maintains membership. All chats use groups; DM has two members. `coop` handles messages, `coord` handles resources.
2. **Names:** `G = d_<a>_<b>` for agent-to-agent DM (raw names sorted by UTF-8 bytes), `h_<a>` for agent a and its owner, or `g_<name>` for a custom group. Percent-encode raw UTF-8 key components, preserving only ASCII letters, digits, and `-`; encode `_` too, so separators are unambiguous. Prefixes are system-owned; do not re-encode generated G.
3. **Messages:** each member independently reads the Stream by cursor. An idle agent polls its groups (e.g. every 500 ms), forms one bounded batch, and includes relevant prior messages and handling results per group. Own messages are context only. Spawn/reply/noted can combine; first-version failures are recorded and skipped. Advance only through handled/skipped messages.
4. **Events:** publish once; order per agent. Aggregate with `PSUBSCRIBE too:agent:*:event too:agent:*:event/*`. Other scopes use exact channel plus descendants. API filters tags and deduplicates IDs. Pub/Sub retains no history.
5. **Coordination:** board claim atomically changes unassigned/pending to claimant/running; completion verifies assignee/revision, records output, and marks finished. Release restores unassigned/pending; every mutation increments revision. Semaphore acquisition atomically removes expired tokens, checks capacity, and adds a fresh token; runtime renews/releases matching live tokens. Config conflicts fail.
6. **Storage:** keys appear on first write; channels require no creation. Group membership must exist before use. Valkey state is transient. Agents persist cursors, batch inputs/results, and execution records. Recent events remain in bounded memory; older records reconstruct begin/content/end events without lost deltas or invented completions.

Open: member discovery/registration; abandoned board claims; ownership and reconstruction of shared state after Valkey loss.

Touchpoints: [channels](../../src/toolang/base/protocols/channel.py), [events](../../src/toolang/execution/events.py), [records](../../src/toolang/execution/records.py), [relay](../../src/toolang/api/common.py), [plugins](../../src/toolang/plugin/).

Messaging acceptance: independent cursors; bounded serial batches; arrivals during handling remain for the next batch; prior context/results survive restart; own replies do not trigger handling; idle joined groups appear in the directory; tool sends validate membership and preserve sender/run identity; final text never triggers delivery; successful receipts survive later handler failure; failed batches are skipped; the loop starts/stops with the agent.
