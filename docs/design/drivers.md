# Execution drivers

A driver accepts triggers, a thread retains context, and a run records an
execution. All three drivers resolve runnable handlers from State and execute
with a captured State/Setup snapshot.

| Driver | Trigger | Thread |
| --- | --- | --- |
| `chat` | Interactive FastAPI requests from TUI/WebUI | `chat_` per conversation |
| `talk` | Incoming Hub message batches | One bound `talk_` per agent/backend/conversation |
| `chore` | One-off, scheduled, or manual work | `chore_<8-character ID>` |

Fresh resident start/serve enable all three by default; visiting start/serve
enable chat/talk. Implicit resident/visiting Chat, rerun, and fork/rewind
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
and explicit start/serve retain availability. See [server lifetime](hosting.md#server-lifetime-and-client-leases).

The management HTTP API, health, inspection, cancellation, and script execution
remain available with chat disabled. Interactive clients create threads at
`POST /api/v0/threads` and submit/control execution under `/api/v0/chat/runs`.
Scripts create contexts at `POST /api/v0/runs/threads` and use `/api/v0/runs`.
Interactive creation, submission, rerun, and steer are gated before writes;
client IDs and thread prefixes do not decide admission.

Requests carry `client_id` and `request_id`; controls retain both. Client IDs
identify caller instances, not thread owners. Reconnecting clients can use the
same thread. A non-null request ID is unique across Run and Thread controls in
the agent's execution store; it is not a general submission-replay key. Reconnect
accepted work with GET instead of repeating POST. Talk batches and runtime operations have separate
durable admission receipts; see [Execution](execution.md#run-execution).

## Handler selection

Driver entries resolve in this order: invocation selection, user handler named
after the driver, runtime handler named after the driver, then the user's unnamed
entry. Missing all candidates fails before run acceptance. Only absence permits
fallback; an invalid selection or a selected handler's failure is reported.

```text
script  explicit name, else unnamed entry, else file help
chat    chat, else unnamed entry, else reported failure
talk    user talk, else runtime talk
chore   chore, else unnamed entry, else reported failure
```

The chosen user entry may be an agic or a flow. Runtime ships a packaged `talk`
agic and no Chat or Chore handler. A file export such as `runnables/talk.too`
acts as a named public handler even if its declaration is unnamed. Existing
public-name conflicts with `agent.too` remain errors. Script exposes authored
declarations only and keeps its existing selection rules.

There is no global `[default].runnable`, `TOOLANG_DEFAULT_RUNNABLE`, or runtime
startup runnable default. Use a driver-named handler. Chat may select its own
session entry; that selection is passed as the invocation choice and affects
only that session. An explicit `_` can select the unnamed entry ahead of any
driver handler.

All surfaces resolve a complete typed input before accepting a Run. Shared
capture/coercion belongs to [Call Input](call-input.md); session choices belong
to [Chat](chat.md). Script routing is separate and described by [CLI](cli.md).

## Chore scheduling

See [Chores](chores.md) for definitions, identity, and commands. `ChoreDriver`
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
Chore uses the shared [handler selection](#handler-selection). Input or handler
failure rejects activation without choosing another runnable.

## Threads and observation

Talk reuses its bound conversation thread across batches and restarts, with a
fresh workdir resolved for each batch. Disabling talk consumption leaves transport,
presence, outbound tools, and observation available. Flow `spawn` creates a new
`work_` thread; [`runtime.spawn`](tools.md#spawn-independent-work) may instead
reuse an inspected idle local thread. Both record ancestry and an independent
root; ordinary nested calls remain in their thread.
Scripts retain the `script_` prefix, and every execution has a `run_` ID.

Independent standalone invocations of the same `.too` source use separate
runtime and sandbox instance directories. Restarting a durable agent reuses its
state. Host runtime registrations retain instance placement and enabled drivers
for runtime discovery.
