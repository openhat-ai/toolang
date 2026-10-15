# Execution drivers and chores

Status: approved in #758, incorporating the subsequent CLI, flat-directory,
identity, and inspection decisions. Backward compatibility is out of scope.

## Goal and scope

Independently select what triggers agent execution. A driver accepts triggers,
a thread retains context, and a run records execution. All drivers resolve
runnable handlers from State and submit captured State/Setup to the common
executor; a handler is a role, not another runnable type.

Scope: hosting and admission, unified chores, identities, and observation.
No generic driver plugin framework, live toggles, Task/Job entity, or migration.

## Drivers and CLI

| Driver | Trigger | Thread |
| --- | --- | --- |
| `chat` | TUI/WebUI interaction over FastAPI | `chat_` per conversation |
| `talk` | Incoming Hub messages | One durable `talk_` per agent |
| `chore` | One-off, scheduled, or manual work | `chore_<id>` per chore |

Chore handler order: explicit runnable call, named `chore` handler, configured
default runnable, unnamed entry. No available handler means a rejected activation.

| Fresh startup | Default drivers |
| --- | --- |
| Resident start/serve | chat, talk, chore |
| Visiting start/serve | chat, talk |
| Resident/visiting Chat, retry/rerun, fork/rewind | chat |
| Roaming or standalone script | none |

Resident/visiting server ownership follows
[the lifecycle contract](resident-visiting-server-lifecycle.md); driver selection
does not determine whether a server is persistent or automatic.

- Only `serve`, `start`, and `chat` expose `--enable`. A comma-separated set
  replaces defaults; `none` selects none and cannot accompany another name.
  Reject invalid selections before startup; resolve placement defaults at the
  call site and forward the concrete set through foreground/background and
  host/guest launches. Report the set in startup/status output.
- `chat` implicitly starts an inactive agent; reject a selection without chat
  before starting. For an existing runtime, omission preserves its selection,
  an equal explicit set permits connection, and a different set fails with
  restart guidance. Disabled chat always fails. Connection never changes the
  selection or restarts/duplicates a running agent.
- `talk` sends Hub messages without starting the recipient. Script execution
  owns one run lifecycle; it neither accepts `--enable` nor starts drivers.
  Attaching a script preserves an existing runtime's drivers. Use `serve` to
  activate drivers and inline chores in a `.too` app.
- Disabling chat retains management HTTP, health, inspection, authoring,
  cancellation, script execution, and other drivers. Interactive thread creation
  uses `/api/v1/threads`; submission/retry/rerun/steer use `/api/v1/chat/runs`
  and are gated before writes. Scripts use `/api/v1/runs`. Thread prefixes and
  client identity do not classify admission, even on another driver's thread.
- Disabled talk/chore drivers do not consume or dispatch triggers. Talk
  consumption is separate from transport, presence, outbound tools, and
  observation. Ownership includes cleanup after partial startup failure.

Examples (`too` aliases `toolang`):

```sh
too serve alice --enable chat,talk,chore
too start alice --enable talk,chore
too serve alice --enable none
too alice chat --enable chat,talk,chore
```

## Definitions, identity, and access

A chore is a pure definition in an immediate `chores/<name>.md` file or inline
`chore name:` declaration. The directory contains current definitions only;
there are no draft/ready/archive stages. Retain list/new/clone/edit/delete/run/cancel.

| Value | Rule |
| --- | --- |
| `name` | Declaration name or filename stem; preserve case and Unicode |
| `ref` | `chore/<name>`, following the `skill/a-b-c` convention |
| `id` | First 80 SHA-256 bits of the UTF-8 name, encoded as 16 Toolang Base32 characters |
| `thread_id` | `chore_<id>` |
| `title` | Optional display text, excluded from identity and activation |

Root files < home files < inline declarations; later inline declarations win.
Dispatch only the winner. Name determines identity within the agent's durable
state: shadowing or moving a definition preserves progress and context; renaming
creates a new identity. Different-name ID collisions fail. Files contain body
and optional title/schedule, never scheduler state or separately authored IDs.

Keep reads and writes with their owners; do not prepare handlers to list data:

- Chore CLI/API read current effective source definitions and join read-only
  checkpoints/run history. Expose source `path` and optional inline `line`.
  Execution fields live under `runtime`; `synced` compares body/schedule hashes,
  distinguishing an unobserved definition from its saved checkpoint.
- Chore authoring requires a resident agent. New home files may shadow root;
  inline conflicts fail with the source to edit. Edit/delete target the visible
  home file and reject root/inline winners. Clone copies any effective definition
  into a new home file/name. Removing a home override reveals its root fallback.
- Caps CLI resolves current declarations through existing materializers, caches,
  and allow rules without publishing executable State. Runtime caps API reads
  the latest accepted State; cap writes target the explicit root/home scope.
- Language/catalog packages own source parsing. State may retain inline chores
  and runnable handlers; the driver combines accepted inline declarations with
  current file definitions. State/Setup contain no chore execution progress.
  Reject malformed definitions; a bad live edit must not stop future refreshes.

## Chore checkpoints and activation

The driver owns one `chores` checkpoint per derived ID in `runs.db`; normal runs
retain execution history. No separate jobs database or authored lifecycle state.

| Checkpoint fields | Meaning |
| --- | --- |
| `chore_id`, `name`, `body_hash`, `schedule_hash` | Identity and last reconciled definition |
| `status`, `error` | Scheduler status and scheduler/recovery error |
| `pending_at`, `pending_trigger` | One coalesced source/manual request |
| `active_run_id`, `active_trigger`, `active_due_at` | Claimed run and consumed occurrence |
| `schedule_anchor_at`, `next_scheduled_at` | RRULE anchor and next scheduled occurrence |
| `created_at`, `updated_at` | Checkpoint creation/change times; unchanged scans do not update them |

- No schedule means one-off: first discovery queues work; a body change queues
  the latest body. Unchanged completed/failed/canceled bodies do not rerun on
  refresh, removal/restoration, or restart. Title/location edits do not activate.
- RRULE schedules begin at the next future occurrence; exhausted schedules are
  terminal. Body edits wait for the next occurrence. A schedule edit resets the
  anchor/cursor; an unchanged schedule preserves them. Removing the schedule
  removes future scheduled occurrences, without itself queuing a source request.
- At most one run per chore. Missed schedule occurrences coalesce into one
  catch-up run. Repeated source/manual requests share one pending slot, preserving
  its first trigger/time; scheduled work remains distinct and executes serially.
- `run` explicitly queues work, including retrying a terminal one-off. `cancel`
  cancels active execution or clears pending source/manual work; future RRULE
  occurrences remain. Deletion stops dispatch but retains checkpoints/history
  and does not cancel active execution.
- Persist a claim before executor acceptance. On restart, release unaccepted
  claims, finish terminal runs, and retain ambiguous accepted runs without
  duplicate dispatch. Recovery preserves manual identity and the current
  schedule cursor; it must not restore an occurrence from a replaced schedule.
  The driver never writes execution state into Markdown or `.too`.

## Context, clients, and observation

- Thread prefixes are `chat_`, `talk_`, `chore_`, `work_`, and `script_`.
  `spawn` creates `work_` with recorded ancestry; ordinary nested calls
  retain their thread. Each execution has a `run_` ID.
- Talk reuses its thread across batches/restarts with normal history/compaction
  and a freshly resolved workdir. Conversation/backend checkpoints remain
  separate under the [transport contract](agent-hub-transport.md).
- Requests and controls carry `client_id` plus `request_id`, replacing client
  type. They identify the caller instance and request, not thread ownership or
  driver selection. Preserve identity across transport retries without changing
  existing deduplication scope.
- Independent instances of the same `.too` source do not share threads or
  progress; standalone scripts use distinct runtime/sandbox directories.
  Restarting the same durable agent reuses state.
- In its selected scope, `top` observes every running placement and script
  instance as instance -> thread -> run, independently of talk/Hub. Combine local
  registrations with available remote activity; distinguish idle drivers from
  active runs and tolerate independent stream reconnects.

## Acceptance and implementation touchpoints

Paths are relative to `src/toolang/`. Verify these scenarios, not just shapes:

1. `cli/`, `up/`, `api/`: all eight driver sets, placement defaults, CLI rejection
   and host/guest forwarding; inactive/existing chat with omitted/equal/conflicting
   sets; disabled interactive requests write nothing while management/scripts work.
2. `lang/`, `catalog/`, `state/`, `work/`: file/inline equivalence and validation,
   shadow/rename behavior, read access with unavailable handlers, visible authoring,
   and recovery from invalid live edits without changing authored files.
3. `work/`: restart and unchanged scans, body/schedule edits during active work,
   manual coalescing, cancellation, exhausted schedules, claim recovery before/after
   schedule replacement, and deletion/restoration with retained history.
4. `execution/`, `work/{messaging,teaming}.py`: canonical prefixes, durable context,
   worker ancestry, client/request identity, isolated instances, transport receipts,
   and partial startup/shutdown cleanup.
5. `up/{observation,activity}.py`, `teaming/`, `cli/`: every placement with talk
   disabled, concurrent instances, and independent remote recovery.

Run repository lint, format, type, and full offline tests. Keep CLI help and
focused docs accurate; update the generated changelog for uncovered behavior.
Risks are persisted-state and client-API breaks: remove old task/job/stage APIs
and aliases; do not import or rewrite old on-disk data. No open design questions.
