# Local subscriptions

Proposed protocol for stage 4 of [teaming](teaming.md); implementation follows
human approval. Stages 1–3 are merged as #709, #708, and #710.

## Goal and scope

Clients independently observe an agent, thread, or root run with descendants,
without Redis/Valkey or teaming enabled. Slow/disconnected clients must neither
stall execution nor disrupt other observers; queues and cleanup are bounded.

This stage adds the agent stream and replaces the existing API relay's queue
plumbing. Hub event forwarding, global subscriptions, activity commands, replay,
and coordination remain in stage 5 or later.

## Shared fanout

- `common/pubsub.py` uses [`pypubsub>=4.0.7,<5`](https://pypi.org/project/Pypubsub/4.0.7/)
  for registration/dispatch, with an isolated `Publisher` per bus. Values are
  generic; execution, HTTP, and backend dependencies stay outside this module.
- One subscription selects one exact string tuple. Encode its compact UTF-8 JSON
  as URL-safe Base64 without padding, prefixed with `t_`: one flat library topic,
  without wildcard matching or accidental hierarchy from dots/Unicode in names.
- Serialize publisher operations per bus. Callbacks only enqueue into bounded
  standard-library queues; retain callbacks strongly and coalesce asyncio
  wake-ups. The bound applies before scheduling across threads, including while
  the receiving loop is paused.
- The API supplies **256 events per client**. Preserve publication order per
  subscription and existing root-tree order, without ordering unrelated
  concurrent publishers by wall-clock time.
- Overflow detaches that subscriber, clears its backlog, and makes receive report
  overflow. Close is idempotent, wakes pending receives, and prevents late delivery.
  Remove empty topics; publishing without listeners creates none. Bus close
  releases every subscriber.

## Routing and HTTP

Keys use the hosted agent's literal name and stored IDs. Each Agent API owns a
separate bus, even when two apps host equal names.

| Scope | Exact internal topic | HTTP endpoint |
| --- | --- | --- |
| Agent | `("agent", agent)` | New `GET /api/v1/stream` |
| Thread | `("thread", agent, thread)` | Existing `GET /api/v1/threads/{thread_id}/stream` |
| Root run | `("run", agent, root_run)` | Existing `GET /api/v1/runs/{run_id}/stream` |

`api/common.py` retains the execution-aware relay. Internal deliveries carry
agent, thread, root run (null for thread mutations), and canonical event. Publish
each run event once per applicable scope, preserving descendants' actual run IDs.
Thread mutations reach their thread and agent; forks also reach the source thread,
with one agent delivery.

All scopes emit the canonical event only, using `execution/events.py` serializers.
Preserve the [existing SSE contract](../api.md#run-and-thread-endpoints), including
validation, root termination, admission ordering, and 15-second keep-alives.
Agent/thread streams remain open until disconnect, overflow, or shutdown.
Closing any GET or POST stream never cancels execution.

Overflow emits `event: stream_error`, `data: {"code":"overflow"}`, then closes.
This transport-only frame never reaches tracers or persistence. A thin adapter
around FastAPI's SSE response limits each write to five seconds; blocked sockets
may close without delivering the error frame. Disconnect, timeout, overflow,
and shutdown all release the subscription.

## Reconnect and compatibility

Keep live-only delivery: no SSE IDs, replay log, or `Last-Event-ID` resumption.
GET observers resubscribe before refreshing durable run/thread snapshots; discard
old partial deltas and treat the connection as a fresh observation.

POST streams never reconnect/retry automatically. On `stream_error` or premature
EOF, `execution/remote.py` reports incomplete observation and retains the accepted
run ID for inspection. Document overflow handling and snapshot refresh for external
clients; normal frames/results remain unchanged.

## Acceptance and touchpoints

| Scenario | Pass condition |
| --- | --- |
| Two clients at every scope | Both observe each matching event once and in publication order; closing one leaves the other active. |
| Routing | Unrelated threads/runs/apps stay isolated; descendant IDs survive; a fork reaches both thread scopes and the agent once. |
| Slow client | Capacity plus one publish detaches only that client; a draining client continues and execution completes. |
| Worker-thread burst | A paused receiving loop cannot bypass the capacity limit through scheduled callbacks. |
| Cleanup | Close/overflow/shutdown wakes blocked receives; no late delivery or retained empty topics/listeners, including races with publication. |
| HTTP compatibility | Existing stream tests pass; terminal attachment, validation, admission, and canonical payloads remain unchanged. |
| Recovery | Overflow/abrupt close reports incomplete observation without retry/cancel; a fresh GET observes subsequent events. |
| Standalone operation | Agent/thread/root streams work with teaming disabled and backend connection attempts forbidden. |

Implementation touches `common/pubsub.py`, `api/common.py`, `api/app.py`, the
agent/run/thread routers, `execution/remote.py`, dependency files, `docs/api.md`,
and focused fanout/API/remote-client tests. Update the changelog through the
existing runnable and run the [default checks](../../AGENTS.md#verification).

Risks: queue capacity bounds event count, not individual payload size; reconnect
cannot recover live-only deltas. No open protocol choices remain. Human approval
is required before implementation.
