# Events

Execution events are value-only subscriber notifications. They contain compact
presentation facts, not persistence commands or copies of execution records.
[Records](records.md) owns durable truth; [Executor](executor.md) owns execution.

## Commit and delivery

Execution writes records, content, control effects, and cursors directly through
RunStore. A successful commit publishes staged notifications to the canonical
memory stream. Failed transactions publish nothing. Independent subscribers
forward events to tracers, HTTP clients, and optionally the Hub. Tracer delivery
is serialized on the owning loop; subscriber failures do not fail the Run.

Event construction uses already available values. The encoder serializes the
completed Event without reading the Store, rebuilding a ModelCall, or resolving
arbitrary historical pointers. Explicit reconnect snapshots may read records
under bounded time/size limits. Retained live replay is bounded; no durable
exact-event journal exists.

## Payloads

The source progress families are `run_begin`, `step_begin`, `part_begin`,
`part_delta`, `part_end`, `step_end`, and `run_end`. Thread mutations add
`thread_created`, `thread_forked`, and `thread_rewound`. Compact specs identify
model, tool, or flow activity. Completion carries direct typed JSON data,
summary/usage, and display errors. Bindings, State references, full calls, and
provider-native evidence remain in records and explicit inspection.

See [event dataclasses](../src/toolang/execution/events.py) for exact fields and
[event projection](../src/toolang/execution/event_projection.py) for the boundary
from runtime values to public payloads.

## Agent subscriptions and SSE

An accepted response exposes `X-Toolang-Run-ID` for compatibility; CORS exposes
it to allowed origins. New-run POST streams begin with root `run_begin`, which
also supplies the ID. Observation is installed before admission. Disconnecting
never cancels execution; reconnect with GET once the run ID is known, never repeat POST.
An older `after` cursor cannot prepend recovery to a newly admitted root: its
subscription starts at admission. GET recovers the existing scope from `after`.

All execution POST streams and these GET streams accept optional `?after=CURSOR`:

| Endpoint | Scope and lifetime |
| --- | --- |
| `/api/v0/stream` | This agent's events; stays open. |
| `/api/v0/threads/{thread_id}/stream` | Physical events of this thread, including forks from it; stays open. |
| `/api/v0/runs/{run_id}/stream` | Complete root tree, through its last active descendant. A child ID returns `409` with the root ID. |

Cursors are opaque agent-wide positions. Invalid or future cursors return `422`
before POST admission. Filtering leaves legitimate gaps. Use `after` explicitly;
`Last-Event-ID` alone does not select a cursor. The server replays retained events
or reconstructs structure from records at a fixed boundary, then sends its live
suffix. Finalized steps need no `part_*` replay. Joining an unfinished step supplies
its missing ancestors and suppresses partial progress through `step_end`.
Without `after`, a root initializes its recorded tree; thread/agent scopes initialize
active trees. Incompatible epochs replace the whole selected scope, including
retained terminal trees. Historical inspection remains separate; there is no
exact historical `/events` collection.

SSE `event` is the event name and `data` its payload. Source events carry `cursor`
and an SSE ID. Structural context carries `context: true`, an optional original
`cursor`, and **no new SSE ID**. Treat structure as upserts; an already applied End
must survive a repeated Begin of the same incarnation. SSE parsers may retain a
previous ID on context frames; do not acknowledge these as new source positions.

| Control | Payload and action |
| --- | --- |
| `stream_prefill` | `{"cursor": B, "scope": {"kind": "run", "id": ID}, "roots": [ROOT, ...]}` starts replacement of listed trees. `kind` is `run`, `thread`, or `agent`; agent scope omits `id`. `roots: null` replaces the entire scope. Buffer the following structural context. |
| `stream_checkpoint` | `{"cursor": B}`, with SSE ID B. Apply the complete replacement, then commit B; a disconnected prefix commits nothing. Also advances over filtered/suppressed events. |
| `stream_error` | `{"code": "overflow"}` or `{"code": "snapshot_limit"}` ends this subscription. A failed write may close without an error frame. |

Clear unfinished Part rendering on each attachment. `StreamClientState` handles
replacement and checkpoint commits before dispatch to presentation. Keep-alive
comments/checkpoints occur every 15 seconds; each ASGI write, including headers,
has a five-second deadline. Idle event waits do not consume that deadline.

Canonical run progress event names are:

- `run_begin`
- `step_begin`
- `part_begin`
- `part_delta`
- `part_end`
- `step_end`
- `run_end`

Run/Step IDs are scalar strings. `StepBegin` includes owning `run`, parent Step ID
and `index`; its `spec.type` selects compact `model`, `tool` or `flow` information.
Model metadata contains only the model identity. Calls, Flow ASTs, State/input
references and accounting internals stay in records and explicit inspection APIs.
Completion events carry direct `{type, value}` outputs, error text and necessary
completion/usage summaries. `preceded_by` application facts and terminal `control`
identify controls by `{run, index}`; `RunBegin.control` is its own Run binding control index.
External subscribers must upgrade and reconnect for this schema.
Step Begin/End fields use `spec`/`report`. History cursors use agent API v0;
Hub publications use hub API v0 and persisted projections use hub store schema 4.
Upgrade publishers and subscribers together. Model completion
summaries expose four token fields under `report.usage`; USD `report.cost` and
`report.cost_approximate` are siblings. They contain no full cost evidence.
Stored model Report now uses `usage`, `cost`, `invoked`, `cont`, with no
`accounting` envelope; Run records add terminal `report`. Schema 61 requires fresh
storage without compatibility reads. See [terminal records](execution.md#terminal-usage-and-cost-records)
and [Run Events](execution.md#run-events).

Every payload retains its canonical `type` discriminator. A `part_begin`
payload uses `part_type` for the message-part kind so it does not collide with
the event discriminator.

Reasoning uses these same events: `part_begin.part_type` is `"reasoning"`,
`part_delta.delta` is `{"kind":"reasoning","text":"..."}`, and `part_end.data`
contains `{"type":"reasoning","text":"..."}`. The call-local `part`
ordinal also identifies its position in the completed Model Step's output.
Provider signatures, provider metadata, full tool arguments and arbitrary tool
result bodies remain in durable Parts and model history; they are excluded from
Event PartData. Deltas are live only; complete execution Parts remain available
through explicit output/inspection endpoints.

Structural cursors are persisted in the execution store; supported schemas and
write restrictions follow [Durable Truth](execution.md#durable-truth). See the
[model adapter contract](plugins.md#model-adapter) for the required indexed
stream interface. Human output continues to omit reasoning and native fields.

Run control acceptance and status are durable `ControlRecord` truth. A thread
stream may additionally carry `thread_created`, `thread_forked`, and `thread_rewound`, and aggregates live run
events belonging to that thread.

## Hub subscriptions

An enabled agent exports its canonical stream independently of execution and
messaging. The Hub endpoint is `GET /events/stream`, with optional
`agent=agent:alice`, and either `thread=ID` or `run=ID` when an agent is selected.
No filter observes the team. `after` accepts only a Hub cursor
(`h1.<epoch>.<stream-id>`); local agent cursors are separate. Root subscriptions
close after the complete tree and queued events; other scopes stay open.

Execution frames preserve their event name/data and add `agent`, `source_cursor`,
and Hub `cursor`. SSE IDs acknowledge Hub positions. Structural context has
`context: true` and no SSE ID. Ignore inherited SSE IDs on context,
`stream_prefill`, and `stream_status` frames. `stream_prefill` carries `{cursor, scope, replace}`:
`replace: null` resets the selected view; otherwise each `{agent, roots}` replaces
that agent's selected view (`roots: null`) or named trees. Stage the entire prefix
and atomically commit it at `stream_checkpoint`. `stream_status` supplies
`{agent, online, complete, reason}`; incomplete origins must not be treated as
completed executions. A failed or interrupted prefix advances no checkpoint.

Invalid filters/cursors return `400`, unknown or unretained scopes `404`, and
backend/schema failures `503`, with `code`/`detail` JSON. Once streaming starts,
`stream_error` reports `overflow`, `snapshot_limit`, `backend_unavailable`,
`scope_unavailable`, or `protocol_error`, then closes. Backend outages never
cancel local runs.

### Hub retention and recovery

The shared execution feed retains at most 10,000 entries and 64 MiB of accounted
stream data. One delivered event has a 1 MiB budget, including its envelope.
Each agent's structural projection has 10,000-entity and 16 MiB budgets and
retains at most 100 completed root trees. Old completed roots and optional
control history are evicted before active structure; an oversized active tree
fails with a snapshot-limit error rather than appearing complete.

A retained same-epoch cursor permits ordered replay. Trimmed history, an epoch
change, or an origin's recovery boundary requires structural replacement from
its retained projection. This restores structure, not expired Part deltas or
arbitrary historical executions. A root outside retained coverage is unavailable;
use that agent's durable inspection API for historical records.

[events.py](../src/toolang/teaming/events.py) and
[records.py](../src/toolang/teaming/records.py) own budgets and projection trimming;
[subscriptions.py](../src/toolang/teaming/subscriptions.py) owns replay and
replacement, verified by [observation tests](../tests/unit/messaging/test_event_observation.py).


## Implementation evidence

- [Canonical stream](../src/toolang/execution/stream.py), [subscriptions](../src/toolang/execution/subscriptions.py), [normalization](../src/toolang/execution/observation.py)
- [Client reducer](../src/toolang/execution/stream_client.py), [SSE transport](../src/toolang/api/common.py)
- [Event tests](../tests/unit/execution/test_events.py), [stream tests](../tests/unit/execution/test_stream.py), [HTTP transport tests](../tests/unit/api/test_stream_transport.py)
