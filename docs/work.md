# Execution drivers

A driver accepts triggers, a thread retains context, and a run records an
execution. All three drivers resolve runnable handlers from State and execute
with a captured State/Setup snapshot.

| Driver | Trigger | Thread |
| --- | --- | --- |
| `chat` | Interactive FastAPI requests from TUI/WebUI | `chat_` per conversation |
| `talk` | Incoming Hub message batches | `talk_` per handled batch |
| `chore` | One-off, scheduled, or manual work | `chore_<8-character ID>` |

Fresh resident start/serve enable all three by default; visiting start/serve
enable chat/talk. Implicit resident/visiting Chat, retry/rerun, and fork/rewind
servers enable chat only. Roaming and standalone script defaults remain none.
`--enable chat,talk,chore` replaces the defaults; `--enable none` disables all
drivers. Only `serve`, `start`, and
`chat` expose this option. `talk` sends Hub messages; script execution owns its
run lifecycle. Neither accepts a driver option.

Chat rejects a selection without chat before starting; roaming Chat still requires
an explicit selection containing chat. For an existing runtime,
an omitted selection preserves its drivers; an explicit selection must match.
A mismatch or disabled chat fails with restart guidance without changing the
running agent. Runtime profile, status, and startup logs report enabled drivers.
Lifetime is independent of drivers: resident implicit servers persist, visiting
implicit servers automatically reclaim when clients and accepted work finish,
and explicit start/serve retain availability. See [execution ownership](execution.md).

The management HTTP API, health, inspection, cancellation, and script execution
remain available with chat disabled. Interactive clients create threads at
`POST /api/v1/threads` and submit/control execution under `/api/v1/chat/runs`.
Scripts create contexts at `POST /api/v1/runs/threads` and use `/api/v1/runs`.
Interactive creation, submission, retry/rerun, and steer are gated before writes;
client IDs and thread prefixes do not decide admission.

Requests carry `client_id` and `request_id`; controls retain both. Client IDs
identify caller instances, not thread owners. Reconnecting clients can use the
same thread. Request identity is preserved across transport retries and retains
its existing deduplication scope.

## Chore scheduling

See [Chores](tasks.md) for definitions, identity, and commands. `ChoreDriver`
combines current Markdown definitions and inline State declarations. Runtime
progress lives in the `chores` table in `runs.db`, owned by the driver, separate
from authored definitions and immutable State/Setup.

- A new one-off is pending immediately. A body edit queues the latest revision;
  an unchanged terminal revision stays terminal. Failures do not retry by
  themselves. `chore run` explicitly requests another occurrence.
- A scheduled chore starts at its next future occurrence. Body edits change the
  next execution without firing immediately. Schedule edits reset the anchor;
  unchanged schedules retain their cursor through reloads and restarts.
- Missed scheduled occurrences coalesce into one catch-up run. Repeated manual
  requests coalesce into one pending manual occurrence. One chore executes at
  most one run at a time; manual and scheduled occurrences remain distinct.
- Removing a definition retains its checkpoint. Recovery reconciles claimed
  activations with recorded runs before dispatching new work.

A dedicated scheduler thread owns the watcher, due heap, RRULE timers, and chore
transitions. It submits to the executor's loop, where run lifecycle and controls
remain owned. Chore and run writes use the same database but separate
transactions; durable claims bridge acceptance and scheduler completion.
Chore selects an explicit runnable call first, then a named `chore` handler,
or the unnamed entry. Runtime provides no Chore handler. Missing all candidates
rejects activation; invalid inputs and handler failures do not select a different
runnable. The handler may be an agic or flow. No Task or Job entity is involved.

## Threads and observation

Talk reuses one thread across batches and restarts, with a fresh workdir resolved
for each batch. Disabling talk consumption leaves transport, presence, outbound
tools, and observation available. `spawn` creates an independent `work_`
thread with recorded ancestry; ordinary nested calls remain in their thread.
Scripts retain the `script_` prefix, and every execution has a `run_` ID.

Independent standalone invocations of the same `.too` source use separate
runtime and sandbox instance directories. Restarting a durable agent reuses its
state. Host runtime registrations let `top` observe resident, visiting, roaming,
and script instances in its scope without requiring talk or Hub. When Hub is
available, remote agent activity is combined with local instances. Enabled
idle drivers are shown separately from active thread/run counts.
