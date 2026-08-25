# Define Remote Run Client And Required Endpoints

Status: Draft for human approval.

## Work Type

Feature definition for an HTTP implementation of the approved `RunClient`
boundary. This plan supersedes the transport decisions in #262 and #267 but
does not replace their remaining Terminal Chat selection and presentation
scope.

## Verified Current Behavior

- `RunClient` exposes async `start`, `stop`, `steer`, and `close`; an accepted
  `RunHandle` exposes `run_id` and async `wait()` returning `RunDetail`.
- `RunRequest` carries unresolved run/session overrides, authored primary and
  named input, ordered runnable fallbacks, an existing thread, and a required
  globally unique request ID.
- `LocalRunClient` reads setup and state once, selects a runnable fallback,
  resolves the request with server-local includes, starts the executor, sends
  canonical recursive `RunEvent` values to an optional tracer, and converts the
  terminal durable run to `RunDetail`.
- `POST /api/v1/runs/stream` accepts a different, already selected runnable and
  resolved HTTP input shape. It cannot preserve `RunRequest` policy, fallback,
  prompt, named-source, or include semantics without client-side access to the
  server's setup and prepared state.
- `GET /api/v1/runs/{run_id}`, `POST /api/v1/runs/{run_id}/cancel`, and
  `POST /api/v1/runs/{run_id}/steer` already expose durable detail and control
  acceptance.
- Live run events are transient and have no replay IDs. A separate start request
  followed by `GET /api/v1/runs/{run_id}/stream` can miss events between run
  acceptance and subscription.
- Run acceptance is durable while status is `pending`. `RunExecutor.stop()` and
  `steer()` accept pending or running runs, but the HTTP control routes currently
  reject pending runs before delegating.

## Problem

A remote adapter cannot implement the approved `RunClient` contract by merely
wrapping the current endpoints. It must send unresolved `RunRequest` values to
the process that owns setup and state, learn the accepted run ID, and subscribe
before the executor can publish the first native event. Adding a broad chat API
or duplicating local resolution in the client would couple the transport to one
UI and allow local and remote behavior to drift.

## Goal

Implement a reusable HTTP `RemoteRunClient` with the same caller request,
native-event, control, and terminal-detail boundary as `LocalRunClient`, and add
only the server behavior that this mapping lacks.

## Success Criteria

- A caller can substitute `RemoteRunClient` for `LocalRunClient` without
  changing `RunRequest`, `RunHandle`, `RunTracer`, `ControlInfo`, or `RunDetail`.
- The server, not the remote process, resolves current setup, state, runnable
  fallback, policy, prompt, input, and file includes.
- The accepted root ID is available in response headers and the live
  subscription exists before execution can publish its first event.
- Every received canonical event is decoded to `RunEvent` and delivered once in
  stream order; no transport-specific event vocabulary is added.
- Existing detail, cancel, and steer endpoints are reused, with only the active
  run compatibility change required by the client contract.
- The client never retries a start, reconnects an incomplete event stream,
  fabricates missing events, cancels server work on close, or manages the remote
  process lifecycle.

## Scope

In scope:

- strict wire schemas and conversion for `RunRequest`;
- one authored-run start-and-stream endpoint;
- shared request resolution used by local and HTTP entry points;
- async HTTP/SSE `RemoteRunClient` and its private remote handle;
- mapping existing detail, cancel, and steer endpoints;
- pending-run control parity and empty user-message parity with `RunExecutor`;
- deterministic unit and API integration tests;
- API and execution documentation.

Out of scope:

- selecting local versus remote execution in Terminal Chat;
- changing `ChatClient`, TUI identity, status, queue, recovery, or error
  presentation;
- WebUI implementation, authentication, endpoint discovery, or remote agent
  lookup;
- SSE replay, persistence, IDs, `Last-Event-ID`, or resubmission;
- retry, rerun, validation, thread creation, history listing, result lookup,
  models, runnables, settings, or runtime lifecycle in `RunClient`;
- changing the existing non-interactive `POST /api/v1/runs/stream` contract.

## Boundary Design

```text
Terminal Chat or another caller
    -> RunClient
        -> RemoteRunClient
            -> POST /api/v1/runs/authored/stream
                -> resolve current RunRequest on the server
                -> RunExecutor + LiveEventRelay
            -> existing run detail/cancel/steer endpoints
```

`RemoteRunClient` belongs to the execution client boundary, not the Chat
package. It may use HTTP libraries but must not import `toolang.api` or
`toolang.cli`; API wire models remain server-owned and the client encodes and
decodes their documented JSON representation.

Move the complete `RunRequest` resolution operation, including ordered
runnable-fallback selection, into an execution-owned helper next to
`resolve_spec()`. `LocalRunClient` and the new API route call the same helper.
The helper receives concrete setup, state, thread, request, and include
resolver values and owns no watcher, store, relay, or transport behavior.

## Authored Run Wire Contract

Add strict API payloads that represent `RunRequest` without exposing local
runtime objects. `POST /api/v1/runs/authored/stream` accepts:

```json
{
  "thread": "term_example",
  "request_id": "term_0123456789abcdef",
  "commands": [
    {"group": "limit", "field": "tokens", "value": 4000}
  ],
  "input": {
    "primary": "Summarize\n@notes.md",
    "named": [
      {"name": "audience", "source": "maintainers"}
    ]
  },
  "session_commands": [
    {"group": "default", "field": "model", "value": "openai/gpt-5"}
  ],
  "runnable_fallbacks": ["agic:chat", "default"]
}
```

The wire rules are:

- `thread` and `request_id` are required canonical nonempty strings;
- `commands` and `session_commands` are ordered arrays and default to empty;
- each command uses the existing `RunOverride` groups and fields;
- allow values are selector arrays or `null`, default values are strings or
  `null`, integer limits are integers or `null`, and cost is canonical decimal
  text or `null` so JSON conversion does not lose precision;
- `input.primary` is text or `null`; `input.named` is an ordered array of unique
  `{name, source}` entries and defaults to empty;
- `runnable_fallbacks` is a required nonempty ordered array of unique canonical
  strings;
- unknown fields and invalid group/field/value combinations return `422`.

The API reconstructs the existing immutable `RunOverride`, `RunnableInputRaw`,
and `RunRequest` values before resolution. No second request vocabulary enters
the executor.

## Start And Event Streaming

The authored route performs the following synchronously in one owner event-loop
turn before returning its streaming response:

1. verify that the thread exists;
2. read current setup and state exactly once each;
3. reconstruct and resolve `RunRequest` with the shared execution helper;
4. resolve file includes relative to the setup working directory, falling back
   to the agent home;
5. call `RunExecutor.start()` with the supplied request ID and the live relay
   tracer;
6. subscribe the relay to the accepted root run before yielding control.

The response is canonical root-run SSE and includes
`X-Toolang-Run-ID: <accepted-root-id>`. CORS exposes this header for clients
using an allowed browser origin. The stream ends after the root `RunEnd` and
closing the response only removes the subscription; it never stops the run.

Missing threads return `404`. Schema, policy, runnable, input, include, resource,
and other pre-acceptance rejections return `422` and create no run. Failures
after acceptance remain canonical terminal `RunEnd` events. The existing
non-interactive stream endpoint retains its request and response behavior.

The header is required instead of waiting for `RunBegin`: it preserves the
protocol meaning that `start()` returns a durably accepted handle and keeps the
run addressable even if the stream fails before its first event. Atomic
subscription avoids the event gap of a separate POST followed by GET.

## `RemoteRunClient`

Add `RemoteRunClient` in a dedicated execution transport module. It owns an
`httpx.AsyncClient` by default and permits injection of a configured async
client for tests and future authentication. Add `httpx-sse` as a direct
dependency rather than maintaining a second partial SSE parser.

Its constructor is:

```python
RemoteRunClient(
    endpoint: str,
    *,
    client: httpx.AsyncClient | None = None,
)
```

`endpoint` is the absolute HTTP or HTTPS agent-runtime origin reported by the
runtime, with no user information, query, fragment, or path other than `/`;
trailing slashes are ignored. Invalid endpoints fail during construction.
Every operation builds a full absolute URL by appending its `/api/v1/...` path
to this normalized endpoint, so an injected client's `base_url` is ignored.
The injected client still supplies transport, authentication, connection
pooling, and default timeout configuration. The remote client closes only a
client it constructed itself.

### `start()`

`start()`:

1. requires an open client and serializes `RunRequest` to the authored wire
   shape;
2. opens `POST /api/v1/runs/authored/stream` without a finite response-body
   read timeout;
3. validates a successful SSE response and canonical
   `X-Toolang-Run-ID` header;
4. returns a private handle as soon as headers establish durable acceptance;
5. keeps one background reader for that handle, even when no tracer is supplied,
   so protocol completion and terminal state can be observed;
6. holds event delivery behind a private gate until the handle is constructed;
   it opens the gate immediately before returning with no intervening await, so
   the background task cannot call the tracer before the caller receives the
   handle;
7. decodes each data event with `run_event_from_data()`, requires the first
   event to be the matching root `RunBegin`, rejects any later second root
   `RunBegin`, and sends valid recursive events to the optional tracer
   sequentially;
8. treats the matching root `RunEnd` as successful stream completion.

SSE comments are ignored. Malformed JSON, invalid native events, a wrong first
event, a header/event root mismatch, a second root begin, or EOF before the root
`RunEnd` raises `RemoteRunClientError`. As in local execution, tracer exceptions
are logged and isolated from run completion. A matching root `RunEnd` wins over
a subsequent connection close. Start is never automatically submitted again.

### `wait()`

The private handle has only its immutable root ID and client-owned completion
state. `wait()` may be called more than once. It waits for the background reader
to receive the matching root `RunEnd`, then calls the existing
`GET /api/v1/runs/{run_id}` and decodes `RunDetail`. A stream or protocol failure
raises instead of returning an incomplete durable projection.

This change deliberately does not poll after premature stream loss. Without
event replay, polling can discover terminal state but cannot satisfy the tracer
contract or repair presentation state. Terminal Chat recovery and queue behavior
remain a later surface-level decision under #262.

### Controls

`stop()` posts the existing cancel body and returns the response's decoded
`command` as `ControlInfo`. `steer()` requires the same user `Message` accepted
by `RunExecutor`, serializes `Message.to_data()`, posts the existing steer body,
and returns `command` as `ControlInfo`. Timing, optional request ID, stop reason,
message parts, and server error details are preserved.

Change the existing cancel and steer precondition from exactly `running` to
active (`pending` or `running`) so a handle is controllable immediately after
acceptance, matching the executor. Allow an empty user parts array in the HTTP
steer schema because it is valid at the `RunClient` boundary. Terminal runs
remain `409` and unknown runs remain `404`.

### Errors And Close

`RemoteRunClientError` is the one transport/protocol exception exposed by the
implementation. It retains a useful HTTP status and FastAPI string `detail`
when available, but does not expose response bodies, endpoints, request input,
policy values, or tracebacks. Connection failures, invalid response JSON, and
schema decode failures use concise operation-specific messages.

`close()` is idempotent. It prevents new operations, cancels and awaits active
reader tasks, closes only an internally owned HTTP client, and releases an
injected client without closing it. It does not call cancel, wait for server
runs, or manage the server process. Handles whose readers are interrupted by
close fail with the client-closed error rather than leaking `CancelledError`.

## Existing Endpoint Mapping

| `RunClient` operation | HTTP mapping | Change |
| --- | --- | --- |
| `start` + native events | `POST /api/v1/runs/authored/stream` | new atomic endpoint and run-ID header |
| `RunHandle.wait` | stream root `RunEnd`, then `GET /api/v1/runs/{run_id}` | reuse detail endpoint |
| `stop` | `POST /api/v1/runs/{run_id}/cancel` | accept pending runs |
| `steer` | `POST /api/v1/runs/{run_id}/steer` | accept pending runs and empty user parts |
| `close` | client-local resource cleanup | no server endpoint |

No endpoint is added for client construction, validation, retry, reconnect,
history, lists, results, or shutdown.

## Design Touchpoints

- `src/toolang/execution/calls.py`: shared complete `RunRequest` resolution.
- `src/toolang/execution/client.py`: use the shared helper without changing the
  protocol or local lifecycle.
- `src/toolang/execution/remote.py` (new): remote implementation, handle, wire
  encoding/decoding, errors, and background readers.
- `src/toolang/api/schemas.py` and `src/toolang/api/conversion.py`: strict
  authored-run wire schemas and core-value reconstruction.
- `src/toolang/api/routers/runs.py`: atomic authored stream and active control
  parity.
- `src/toolang/api/app.py`: expose the accepted run-ID header through CORS.
- `pyproject.toml` and `uv.lock`: direct async SSE client dependency.
- `tests/unit/execution/test_remote_run_client.py` (new): transport mapping and
  failure/lifecycle behavior.
- `tests/unit/execution/test_run_client.py`: shared-resolution local parity.
- `tests/integration/api/test_streaming.py`: authored endpoint, headers,
  resolution, events, rejection, and pending controls.
- `docs/api.md` and `docs/execution.md`: endpoint and remote boundary.

Keep `toolang.execution.__init__` narrow; concrete clients remain importable
from their owning modules.

## Acceptance Tests

1. Round-trip every `RunRequest` field, all policy value kinds including
   `Decimal`, primary/named input, and ordered fallbacks through the strict wire
   conversion; reject extra, malformed, duplicate, and lossy values.
2. Prove local and API paths call the shared resolver with one setup/state pair
   and preserve fallback, session/run policy precedence, prompts, named input,
   and server-relative includes.
3. Start an authored HTTP run and assert the accepted header matches the first
   root `RunBegin`, every recursive canonical event is delivered once in order,
   the stream ends at root `RunEnd`, and durable detail matches the handle ID.
4. Assert missing thread is `404`; invalid policy, fallback, input, include, and
   duplicate request ID are pre-header `422` responses that do not create an
   unintended run.
5. Exercise endpoint normalization, injected-client base URL and ownership,
   `RemoteRunClient.start()`, and repeatable `wait()` over deterministic HTTP/SSE
   fixtures, with and without a tracer. Assert no tracer callback occurs before
   `start()` returns, decode the returned `RunDetail`, and isolate tracer failures
   as they are locally.
6. Cover malformed JSON/event data, wrong first/root IDs, premature EOF,
   pre-header transport failure, HTTP detail errors, invalid detail/control
   responses, and the no-retry rule.
7. Stop and steer a pending and a running run; preserve timing, request ID,
   reason, empty and nonempty user messages, `ControlInfo`, and existing
   terminal/unknown rejection.
8. Close during active streams and after completion; assert idempotence, no
   leaked tasks, no server-run cancellation, injected-client ownership, and
   closed-operation errors.
9. Keep the existing non-interactive stream request, event order, controls,
   local client, Terminal Chat, Script, Scheduler, Inbox, and `AgentCore`
   behavior green.
10. Run `uv run ruff check .`, `uv run ruff format --check .`,
    `uv run ty check`, and `uv run pytest`.

## Risks And Mitigations

- **Events lost between start and subscribe:** accept and subscribe in one owner
  event-loop turn before response headers.
- **Resolution drift:** one execution-owned helper serves local and HTTP paths.
- **Duplicate work after ambiguous transport failure:** never retry start;
  request IDs remain uniqueness guards, not idempotency keys.
- **Incomplete presentation after stream loss:** fail explicitly without
  fabricating or replaying events; leave TUI recovery to its owning scope.
- **Closing the UI stops server work unexpectedly:** remote close detaches only.
- **API/client model coupling:** document JSON, keep strict API schemas on the
  server, and keep the execution client independent of `toolang.api`.

## Follow-up

After this definition is approved and implemented, #262 may define the smaller
remaining Terminal Chat composition change: runtime selection, remote/local
identity, non-run operations, and user-facing recovery. A future WebUI can
implement the same documented authored-run HTTP contract without sharing TUI
presentation code.

## Open Questions

None. Request ownership, endpoint shape, event atomicity, controls, terminal
detail, failures, lifecycle, compatibility, and follow-up scope are decided.
