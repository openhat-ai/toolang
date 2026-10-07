# Production messaging plan

Approved scope; comma shortcuts are deferred. Add **two commands** and share one messaging core across CLI, tools, and hosted agents.

## Components

Paths are relative to `src/toolang/`.

| Component | Module | Responsibility |
| --- | --- | --- |
| `too text` | `cli/toolang/commands/text/` | Send-and-exit; interactive TUI; history; tmux window reuse. |
| `too team` | `cli/toolang/commands/team.py` | List groups, members, online status, and latest message time. |
| Messaging core | `messaging/{config,schemas,client}.py` | Protocol, configuration, directory, membership, presence, Valkey I/O. |
| `CoopToolset` | `plugin/toolsets/coop.py` | Expose `contacts` / `send` through the shared core. |
| `MessagingLoop` | `work/messaging.py` | Batch messages into `agic:msg` or default; persist local cursors/context/results. |
| Terminal helpers | `cli/common/` | Share Chat input, Rich rendering, finalize output, and `libtmux` placement. |
| Hosted lifecycle | `up/server.py`, `setup/watcher.py` | Register agent, renew presence, start/stop loop; keep configuration consistent. |

## Commands

```sh
too text alice           # Open the owner DM with alice.
too text dev             # Open gc_dev.
too text all             # Open the public group.
too text alice hello     # Send and exit; do not wait for a reply.
too team                 # List all and custom groups.
```

- Canonical IDs resolve directly; ambiguous bare names require `--dm` / `--group` before the target. Unknown targets error.
- After the target, remove one optional leading `--`; join shell arguments as literal text. Quoted whitespace/newlines survive. Interactive mode requires a TTY.

## Messaging contract

- IDs: `all`, `gc_<name>`, `dm_<agent>`, `dm_<a>_<b>`; sort agent pairs by raw UTF-8 name. Percent-encode components except ASCII letters, digits, and `-`. Names are case-sensitive and stable.
- Root configuration: `[human].name` defaults to OS username; `[messaging].url` selects external Valkey. Agent `[messaging].groups` declares custom memberships. Configuration changes require restart.
- Keys: `too:agents` Hash (agent → owner); `too:groups` Set; `too:agent:A:online` expiring token; `too:group:G:members` Set; `too:group:G:msg` Stream.
- Message: Stream field `data` contains `{id, sender, body, in_reply_to, origin}`. Only the shared core creates/validates envelopes and writes messages.
- Registration prepares `all` and owner DMs; agent DMs materialize on first use. Discover through Valkey, including remote agents. Preserve offline membership; token-check presence renewal/removal and reject duplicate live identities.
- Agent tools access joined groups; owners can inspect groups without joining. No access to other agents' threads.
- Process bounded, serial batches with per-group prior context/results; skip own messages and record/skip failed handlers. Reply to the source group unless explicitly redirected; substantial work may spawn.
- Reconnect reads; uncertain sends report "not confirmed" without automatic retry. Preserve drafts and send receipts. Bound retention; expose checkpoint/history gaps. No exactly-once or permanent-history guarantee.

## Text features

- Human: control-bar style on the right. Agent: model-output style on the left. Opposite-side gutters; body text stays left-aligned.
- Agent DM: model-output style on both sides; sorted participants have fixed left/right positions.
- Bottom live input; finalize into scrollback. Catch up retained history, then follow without duplicate or missing entries.
- tmux: session per root/connection/human, window per group; reopen by identity. Keep drafts/history independent. Outside tmux, run in the current terminal. Copy mode handles history browsing. Preserve existing Chat behavior.

## Delivery checklist

- [x] Extract the shared core; productionize `coop`, directory/presence, and agent lifecycle.
- [x] Add `text`, `team`, and Text TUI/tmux integration.
- [x] Test naming/escaping, literal command bodies, offline DMs, presence expiry, independent readers, full Stream IDs, reconnects, uncertain sends, narrow layouts, and window reuse.
- [x] Keep default tests offline; run isolated Valkey/tmux checks separately and complete [repository verification](../../AGENTS.md#verification).
- [x] Update usage docs and generate the changelog through `too aide.too update_changelog`; leave the Textual prototype outside the production branch.

No events, `coord`, authentication, managed Valkey, or coordination guarantees. Experimental data is not retained or migrated. The previous prototype is reference material only; implementation starts from origin/main.
