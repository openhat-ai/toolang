# Production messaging plan

Approved scope; comma shortcuts are deferred. Add **two commands** and share one messaging core across CLI, tools, and hosted agents.

## Components

Paths are relative to `src/toolang/`.

| Component | Module | Responsibility |
| --- | --- | --- |
| `too text` | `cli/toolang/commands/text/` | Send-and-exit; interactive TUI; history; tmux window reuse. |
| `too team` | `cli/toolang/commands/team.py` | List conversations, participants, online status, and latest message previews. |
| Messaging setup | `setup/messaging.py`, `AgentSetup.messaging` | Resolve defaults, overrides, and human identity once; supply CLI, tools, and hosting. |
| Messaging core | `messaging/{config,schemas,client}.py` | Concrete configuration, protocol, directory, membership, presence, Valkey I/O. |
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
too team                 # List all existing conversations, including DMs.
```

- Canonical IDs resolve directly; ambiguous bare names require `--dm` / `--group` before the target. Unknown targets error.
- After the target, remove one optional leading `--`; join shell arguments as literal text. Quoted whitespace/newlines survive. Interactive mode requires a TTY.
- Team directory: Target, Participants, Latest message. Mark online/offline agents beside their names; humans have no presence marker. Pin `all`, then sort by newest message, with empty conversations last. Show a compact local timestamp and single-line preview; do not create unused agent pairs.

## Messaging contract

- IDs: `all`, `gc_<name>`, `dm_<agent>`, `dm_<a>_<b>`; sort agent pairs by raw UTF-8 name. Percent-encode components except ASCII letters, digits, and `-`. Names are case-sensitive and stable.
- Root configuration: `[human].name` defaults to OS username; `[messaging].url` defaults to `redis://localhost:6379/0`; `[messaging].enabled = false` opts out. Agent `[messaging].groups` declares custom memberships. Configuration changes require restart.
- Keys: `too:agents` Hash (agent → owner); `too:groups` Set; `too:agent:A:online` expiring token; `too:group:G:members` Set; `too:group:G:msg` Stream.
- Message: Stream field `data` contains `{id, sender, body, in_reply_to, origin}`. Only the shared core creates/validates envelopes and writes messages.
- Registration prepares `all` and owner DMs; agent DMs materialize on first use. Discover through Valkey, including remote agents. Preserve offline membership; token-check presence renewal/removal and reject duplicate live identities.
- Agent tools access joined groups; owners can inspect groups without joining. No access to other agents' threads.
- DMs have exactly two sending participants. Humans may observe agent DMs but cannot send into them; use a custom group for a three-person conversation. Keep existing messages and drafts. Enforce the same rule in messaging core, one-shot CLI sends, and interactive Text.
- Process bounded, serial batches with per-group prior context/results; skip own messages and record/skip failed handlers. Reply to the source group unless explicitly redirected; substantial work may spawn.
- Reconnect reads; uncertain sends report "not confirmed" without automatic retry. Preserve drafts and send receipts. Bound retention; expose checkpoint/history gaps. No exactly-once or permanent-history guarantee.

## Text features

- Align the current identity's messages right and every other sender left. Human messages have backgrounds; agent messages use model-output styling on either side. Body text stays left-aligned with two-cell horizontal padding, names above the outer edge, and a marker in the outer padding on the first body line.
- Human observers see both agents on the left, a read-only footer, and no input box. Owner DMs are also read-only for humans outside their two participants. Text's CLI identity remains human; an agent-identity CLI option is outside this change.
- Bottom live input; finalize into scrollback. Catch up retained history, then follow without duplicate or missing entries.
- Input, footer, and messages share the configured maximum width. Keep one external separator row and batch scrollback writes. Names use normal foreground, with agent names bold. Preserve message padding and first-body-line markers.
- Footer: `@alice`, `@alice ↔ @bob`, `#dev`, or `#all`, followed by `Read-only` when observing, connection state, transient `Sent` for two seconds, and applicable keyboard hints. Reconnection stays in the footer; actionable errors retain their detailed notice and draft. Normal status omits message UUIDs.
- tmux: session per root/connection/human, window per group; reopen by identity. Keep drafts/history independent. Outside tmux, run in the current terminal. Copy mode handles history browsing. Preserve existing Chat behavior.

## Delivery checklist

- [x] Extract the shared core; productionize `coop`, directory/presence, and agent lifecycle.
- [x] Add `text`, `team`, and Text TUI/tmux integration.
- [x] Test naming/escaping, literal command bodies, offline DMs, presence expiry, independent readers, full Stream IDs, reconnects, uncertain sends, narrow layouts, and window reuse.
- [x] Keep default tests offline; run isolated Valkey/tmux checks separately and complete [repository verification](../../AGENTS.md#verification).
- [x] Update usage docs and generate the changelog through `too aide.too update_changelog`; leave the Textual prototype outside the production branch.
- Follow-up acceptance: input backgrounds remain continuous through resize/clear; footer fits narrow terminals; reconnect does not add scrollback notices; directory includes existing DMs, sorts correctly, and tolerates malformed previews. Verify isolated terminal startup/history and repainting.
- DM acceptance: a human observer cannot send through CLI, TUI, or messaging core; member agents can still exchange messages. Existing history/drafts survive. Verify both agent messages align left for observers, self messages align right for either identity type, and names align with markers in narrow and wide terminals.

No events, `coord`, authentication, managed Valkey, or coordination guarantees. Experimental data is not retained or migrated. The previous prototype is reference material only; implementation starts from origin/main.
