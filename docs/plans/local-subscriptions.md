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
- The API supplies **256 queued events per client**, plus bounded framework
  buffers and in-flight writes. Preserve publication order per subscription and
  existing root-tree order; unrelated concurrent publishers have no wall-clock order.
- Overflow detaches that subscriber, clears its backlog, and latches its reason.
  Receive distinguishes events, timeout, closed, and overflow. Idempotent close
  clears queued items, wakes receives, and prevents further enqueue while preserving
  an existing overflow reason. A closed receiving loop detaches the subscriber
  without failing publication. Remove empty topics;
  publishing without listeners creates none. A closed bus rejects subscriptions,
  releases existing subscribers, and ignores late publications.

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
An agent observer joining mid-run resolves thread/ancestry through run detail;
canonical run events do not contain that routing context.
Preserve the [existing SSE contract](../api.md#run-and-thread-endpoints), including
validation, root termination, admission ordering, and 15-second keep-alives.
Agent/thread streams remain open until disconnect, overflow, or shutdown.
Closing any GET or POST stream never cancels execution.

Overflow emits `event: stream_error`, `data: {"code":"overflow"}`, then closes.
It can be the first POST frame if overflow precedes consumption; the accepted
`X-Toolang-Run-ID` remains valid. Check overflow and queued events again after
awaiting terminal-status lookups, before taking the empty-stream shortcut.
Clients handle this transport frame before canonical decoding; it never reaches
tracers or persistence. Frames already handed to transport may precede it.

Keep FastAPI's generator SSE support. Pure ASGI middleware limits each SSE `send`
to five seconds, including headers, without timing idle event waits or changing
other responses. The current FastAPI generator path constructs `StreamingResponse`
directly, so subclassing `EventSourceResponse` cannot enforce this deadline.
Timeout unwinds the request and dependencies; blocked sockets may close without
the error frame. Verify cleanup through the real ASGI path, including framework
prefetch and disconnect exceptions.

The API owns bus cleanup and composes it with any supplied lifespan. Hosting closes
the bus at shutdown before Uvicorn waits for streams; lifespan cleanup is also
idempotent. Disconnect, timeout, overflow, and shutdown release every affected
subscription, including one blocked in receive.

## Reconnect and compatibility

Keep live-only delivery: no SSE IDs, replay log, or `Last-Event-ID` resumption.
GET observers discard old partials, resubscribe, then refresh durable snapshots.
There is no atomic snapshot/stream boundary. During refresh, coalesce incoming
events into a dirty flag and refresh again if needed; never append buffered deltas
to snapshot output or regress terminal records. Live partials remain disposable
and may be incomplete; completed parts/outputs replace them. Update the existing
API reconnect guidance to describe this refresh policy.

POST streams never reconnect/retry automatically. On `stream_error` or premature
EOF, `execution/remote.py` reports incomplete observation and retains the accepted
run ID for inspection. Document overflow handling and snapshot refresh for external
clients; normal frames/results remain unchanged.

## Acceptance and touchpoints

| Scenario | Pass condition |
| --- | --- |
| Two clients at every scope | Both observe each matching event once and in publication order; closing one leaves the other active. |
| Routing | Unrelated threads/runs/apps stay isolated; descendant IDs survive; mid-run agent observers can resolve ancestry; a fork reaches both thread scopes and the agent once. |
| Slow client | At the bus boundary, capacity plus one publish detaches only the full subscriber; a concurrently draining client continues and execution completes. |
| Worker-thread burst | A paused receiving loop cannot bypass the capacity limit through scheduled callbacks. |
| Cleanup | Close/overflow/shutdown wakes blocked receives; no retained topics/listeners or re-enqueue after closure, including publication races. |
| ASGI lifecycle | Blocked header/body writes release dependencies within the deadline; idle streams survive it; disconnect and hosting shutdown release subscriptions with bounded prefetch. |
| HTTP compatibility | Existing stream tests pass; terminal attachment, validation, admission, and normal canonical payloads remain unchanged. |
| Overflow races | Overflow before the first POST frame or during terminal lookup produces an error, never a successful empty stream; accepted run IDs survive. |
| Recovery | Reconnect guidance discards buffered deltas; POST overflow/abrupt close reports incomplete observation without retry/cancel; fresh GETs observe subsequent events. |
| Standalone operation | Agent/thread/root streams work with teaming disabled and backend connection attempts forbidden. |

Implementation touches `common/pubsub.py`, `api/common.py`, `api/app.py`, the
agent/run/thread routers, `up/server.py`, `execution/remote.py`, dependency files,
`docs/api.md`, and focused fanout/API/remote-client tests. Update the changelog
through the existing runnable and run the [default checks](../../AGENTS.md#verification).

Risks: queue capacity bounds event count, not individual payload size; reconnect
cannot recover live-only deltas. No open protocol choices remain. Human approval
is required before implementation.
