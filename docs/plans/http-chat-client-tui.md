# HTTP Chat Client Support In The Chat TUI

## Goal

Allow the terminal Chat TUI to use an already-running resident agent through
HTTP without changing the TUI's execution presentation or creating a second
event vocabulary. Make the selected client visible as text so users can tell
whether execution is process-local or owned by the resident agent runtime.

Implementation starts only after this definition is approved.


## Success Criteria

- A Chat TUI for a healthy running resident agent uses `HttpChatClient`; the
  same command uses `LocalChatSession` when that agent has no active runtime.
- Roaming and visiting chat keep their current process-local behavior.
- HTTP chat supports thread creation and continuation, policy validation, run
  submission, native event streaming, cancellation, steering, model and
  runnable listing, and durable `:show` results through the server API.
- The header and bottom status bar identify every TUI session with a visible
  `[HTTP]` or `[LOCAL]` label that remains meaningful without color.
- HTTP transport loss never causes the client to submit a duplicate run or to
  fall back to a second local executor.
- Execution failures, submission failures, control failures, and transport
  failures remain distinct and produce one concise user-facing diagnostic.
- Existing local chat behavior, public chat command placement, native
  `RunEvent` presentation, and noninteractive output remain compatible as
  defined below.


## Current Behavior

`toolang TARGET chat` resolves resident, roaming, and visiting targets to an
`AgentLayout`. `src/toolang/cli/toolang/commands/chat/main.py` then enters
`_chat_runtime()`, which always creates `LocalChatSession`. That session opens
the selected layout's run store, owns an event-loop thread, maintains setup and
state watchers, starts runs with `RunExecutor`, and passes native `RunEvent`
values to the TUI.

The current construction path has these consequences:

- a resident agent's already-running HTTP process is ignored;
- direct chat rejects every `--sandbox` driver except `none`;
- the TUI and a running resident agent can independently open the same durable
  state while execution remains owned only by the process that called
  `RunExecutor.start()`;
- roaming and visiting chat work because their exact prepared layout is passed
  to `LocalChatSession`.

The shared `RuntimeClient` in `src/toolang/cli/common/client.py` provides JSON
GET/POST and SSE transport plus model and executable list helpers. It does not
implement the current `ChatClient` protocol. The API already provides thread
creation, run detail, run cancellation, run steering, model and executable
inspection, and canonical `RunEvent` SSE streams. Its generic
`POST /api/v1/runs/stream` accepts resolved percept parts, runnable bindings,
named values, and limits; it does not accept the authored chat source or
session policy that `LocalChatSession` currently resolves.

The TUI receives a transport-neutral `ChatClient`. It creates the thread only
on first runnable submission, runs blocking client calls on daemon threads,
queues later input while a run is active, and feeds canonical events through
`ChatRunPresenter`. `:show`, model/runnable listing, policy edits, cancellation,
and steering all call the same client protocol.

The header currently shows Toolang version, model, and agent home. The bottom
bar shows the model, a non-default runnable, and key hints. Neither surface
identifies the client. Connection state also has no presentation contract.

Before the execution rebuild in pull request #231, chat reused a running API
through `RuntimeClient` and a dedicated chat route. That route and the
chat-specific methods were removed with the old execution request/event model.
This feature restores the useful selection behavior through a separate
`HttpChatClient`, using today's policy, record, and canonical event contracts.


## Scope

This feature covers:

- selecting an HTTP or local client for the existing chat entry path;
- adapting the resident agent API to the complete current `ChatClient`
  contract;
- server-side resolution of authored chat source and session policy;
- canonical SSE decoding and safe loss-of-stream recovery;
- explicit client identity and connection feedback in the Chat TUI;
- deterministic unit, integration, and pseudo-terminal coverage;
- updating public chat and API documentation to describe both clients.

This feature does not add:

- a `--client`, `--http`, `--local`, `--endpoint`, or authentication option;
- automatic starting, restarting, or stopping of an agent runtime;
- HTTP chat for roaming or visiting targets;
- event persistence, SSE replay IDs, `Last-Event-ID`, or an alternate chat
  event protocol;
- automatic restoration of earlier conversation scrollback when the TUI
  opens;
- changes to the status/summary content owned by #261 except for the required
  client identity and connection diagnostic;
- a database schema change or new execution record type;
- changes to generic script submission through `/api/v1/runs/stream`.


## Client Identity Contract

`ChatClient` gains immutable caller-facing identity metadata rather than
requiring the TUI to inspect concrete client classes:

```text
kind    label
local   LOCAL
http    HTTP
```

The protocol may expose this as a small frozen value object or equivalent
read-only properties. The values are closed vocabulary owned by terminal chat;
they are not execution metadata and are not persisted on runs or threads.

The TUI presents the identity in two places:

- the startup header adds `client: HTTP` or `client: LOCAL`;
- the left side of the bottom bar begins with `[HTTP]` or `[LOCAL]` before the
  model and runnable.

Square brackets and the words themselves are required. Color may reinforce the
identity but must not replace, abbreviate, or hide it. In a narrow terminal,
the client token has higher priority than model, runnable, and shortcut hints;
`[HTTP]` or `[LOCAL]` remains intact while lower-priority content is truncated
or omitted. This feature does not assign HTTP or local meanings to a color.

The header continues to show the selected agent home. It does not print the
endpoint: discovered endpoints may be long, unstable, or contain deployment
details, and the concise client label is sufficient to explain ownership.

Noninteractive scripted chat does not add an identity banner. Its existing
stdout/stderr contract remains unchanged; transport failures still identify
HTTP in their diagnostic text.


## Client Selection And Lifetime

The chat composition root selects one client before opening either the TUI or
the scripted input loop.

1. Resolve the exact `AgentLayout` as today.
2. If its placement is `roaming` or `visiting`, construct
   `LocalChatSession` without consulting process status.
3. For a resident layout, read `AgentProcess(layout).status()` using the
   configured UI base URL.
4. If no active status exists, construct `LocalChatSession` as today.
5. If status is `running` and publishes an endpoint, perform a bounded health
   check and construct `HttpChatClient(RuntimeClient(endpoint))`.
6. If status is `preparing`, `starting`, or `running` without a usable
   endpoint, fail before the TUI opens with the existing active-agent wording.
7. If the status advertises a running endpoint but the health check fails,
   report that endpoint as unavailable. Do not silently open a local executor.

The health check uses `GET /healthz` with a three-second timeout and requires
`{"ok": true}`. It is a readiness check, not a retry loop. A later request can
still fail normally.

No public transport selector is added. Process ownership is already the
unambiguous signal: a running resident runtime owns execution, while a
resident without one and every nonresident layout use local execution. A
future manual remote endpoint or authenticated deployment needs a separate
feature definition.

The construction context owns the adapter, not the resident runtime:

- closing `LocalChatSession` shuts down its executor and watchers as today;
- closing `HttpChatClient` closes active response bodies, stops recovery
  workers, and releases transport resources, but never stops or restarts the
  agent process;
- force-exiting an HTTP TUI detaches from an active run. It does not implicitly
  cancel server-owned execution. Users cancel explicitly with Ctrl-C, double
  Escape, or the existing control command before exiting.

The TUI and scripted loop continue to receive the protocol, not a
`RuntimeClient`, and must not branch with `isinstance()` checks.


## Configuration And Policy

The running server's current `AgentSetup`, prepared state, environment, model
providers, ceiling, bindings, limits, working directory, and sandbox are
authoritative in HTTP mode. The chat process does not rebuild setup from its
own dotenv files and does not mutate server configuration.

Explicit chat options behave as follows:

| Option | Local client | HTTP client |
| --- | --- | --- |
| `--default` | current setup binding override | initial chat-session default command validated by the server |
| `--limit` | current setup limit override | initial chat-session limit command validated by the server |
| `--allow` | current setup ceiling override | unsupported; fail before opening the TUI |
| `--sandbox` omitted | direct `none` execution | attach to the runtime's existing sandbox |
| `--sandbox VALUE` | retain current direct-chat validation | require VALUE to match the running runtime's canonical sandbox selector |

`--allow` is rejected in HTTP mode because a process-local setup override can
replace a configured ceiling, while a request sent to an already-running
server may only restrict the server's ceiling. Quietly changing the option
from an override to a restriction would make the same command mean different
things. The diagnostic directs users to configure and restart the resident
runtime, or to use a session `:allow` command for an additional restriction.

Policy-only input inside the TUI remains supported for all `allow`, `default`,
and `limit` fields. `HttpChatClient.apply_settings()`:

1. merges the proposed commands with the current session commands using the
   shared policy functions;
2. sends the complete candidate command sequence to the server validation
   route;
3. returns the updated presentation selectors only after validation succeeds;
4. leaves the current session unchanged on any validation or transport error.

Per-run policy prefixes stay in the authored source and are resolved by the
server after the session layer. This preserves the existing setup -> session
-> run precedence and ensures that resource restrictions cannot expand the
server ceiling.

Decimal cost limits are serialized as JSON strings and reconstructed as
`Decimal`; allow-list values preserve the distinction among unrestricted
`null`, denied `[]`, and an explicit selector list. Unknown fields and extra
request keys are rejected by strict API schemas.


## HTTP Chat API Contract

Add a chat router under `/api/v1/chat`. It is a source-and-policy adapter over
the existing execution core, not a second execution model.

### Validate Session Policy

```text
POST /api/v1/chat/validate
```

The request contains an ordered `commands` array of canonical policy command
objects. The server reconstructs `PolicyCommand` values and calls
`validate_commands()` against its current setup and state with the same
default-runnable rule as local chat: use `chat` when that agic exists,
otherwise `default`. Success returns `204 No Content`. Invalid policy returns
`422` with one `detail` string. Validation does not create a thread, run, or
control.

### Submit And Stream A Chat Run

```text
POST /api/v1/chat/stream
```

The strict request contains:

- `thread`: an existing thread ID;
- `request_id`: one globally unique client-generated start-control request ID;
- `source`: the normalized authored chat source, including any per-run policy
  prefix and named input syntax;
- `session_commands`: the complete canonical session policy sequence.

The server uses the same `parse_call()`, `resolve_spec()`, policy precedence,
default runnable, setup/state snapshots, and include resolver as
`LocalChatSession`. Relative includes resolve against the server setup's
working directory, falling back to the server agent home. It then starts the
run through its process-owned `RunExecutor` and publishes through the existing
`LiveEventRelay`.

The response is canonical SSE:

- each SSE `event` is the canonical `RunEvent.type`;
- each `data` payload is the canonical serialized event and retains its `type`;
- the complete recursive run tree is ordered exactly as for
  `/api/v1/runs/stream`;
- the stream ends after the root `RunEnd`;
- keep-alive comments remain transport-only.

The response includes `X-Toolang-Run-ID` after the start control has been
accepted and before event bytes are consumed. This allows the client to track
an accepted run even if the connection fails before it reads `RunBegin`. The
header does not make `request_id` a replay key; duplicate non-null request IDs
remain rejected.

Submission errors before acceptance return `404` for a missing thread and
`422` for source, policy, input, runnable, or resource validation. They do not
open an SSE stream or create a failed run. Failures after acceptance are
represented by the canonical root `RunEnd`, not by a second HTTP error body.

### Read A Chat Result

```text
GET /api/v1/chat/result?run_id=RUN_ID
GET /api/v1/chat/result?thread_id=THREAD_ID
```

Exactly one query parameter is required. The run form returns that visible
run's resolved output. The thread form returns the newest visible finished
root run with nonempty resolved output, matching `LocalChatSession.get_result`
without an arbitrary history window. The response contains `run_id` and the
ordered canonical message parts. Missing runs, absent results, and empty
results retain the local client's current user-facing distinctions through
`404` or `409` detail messages.

### Runnable Inspection

Add `GET /api/v1/runnables` beside the existing agic and flow list routes. It
returns the server's effective default plus combined `{kind, name}` items from
one setup/state snapshot. `HttpChatClient.list_executables("runnable")` must
not synthesize a result from two requests that could observe different
generations.

Thread creation continues to use `POST /api/v1/threads` with `client: "tui"`.
Model, agic, and flow listing, run detail, cancellation, and steering continue
to use their existing endpoints. The generic run-stream request and response
schemas do not change.


## `HttpChatClient` Behavior

`HttpChatClient` lives in the chat package, while `RuntimeClient` remains a
package-neutral HTTP/SSE transport. The adapter implements every
`ChatClient` operation:

| Operation | HTTP behavior |
| --- | --- |
| identity | immutable `http` / `HTTP` metadata |
| list models | `GET /api/v1/models` |
| list agics, flows, runnables | matching agent inspection endpoint |
| create thread | `POST /api/v1/threads` with client `tui` |
| apply settings | merge locally, validate complete candidate remotely |
| start run | `POST /api/v1/chat/stream`, decode native events |
| stop run | immediate `POST /api/v1/runs/{id}/cancel` |
| steer run | next-step `POST /api/v1/runs/{id}/steer` |
| get result | `GET /api/v1/chat/result` |
| close | stop local readers/recovery only; never control the runtime process |

Every start, cancel, and steer operation gets a fresh `term_<uuid>` request
ID. The adapter sends the exact full session policy captured in the queued
call, so changing settings while another submission is queued does not mutate
that queued submission.

SSE data is parsed with `run_event_from_data()`. A payload that is not an
object, has an unknown discriminator, fails canonical validation, begins with
an event other than the root `RunBegin`, changes root identity, or reaches EOF
without root `RunEnd` is a protocol/transport failure. Valid events are passed
to the existing callback unchanged. The adapter never constructs a
presentation-specific event.

Cancellation and steering remain concurrent with the blocking stream reader.
An accepted control does not synthesize a run event; the stream remains the
source of execution progress. A cancel that races with an already-terminal
run treats the server's terminal detail as success rather than displaying a
spurious `cancel failed`. A steer against a terminal or unknown run remains an
error. Repeating Ctrl-C after one accepted cancel remains suppressed by the
TUI's existing `cancel_sent_run_id` behavior.


## Streaming Loss And Reconnect Behavior

Toolang does not persist an exact event log, issue SSE event IDs, or replay
missed deltas. `HttpChatClient` therefore must not reconnect to the live stream
and pretend that the resulting event sequence is complete. It also must not
retry the POST: request IDs are uniqueness guards, not idempotent replay keys.

The defined recovery behavior is:

1. Record the accepted root ID from `X-Toolang-Run-ID`, then confirm that the
   first canonical `RunBegin.run` matches it.
2. If the stream ends normally at the matching root `RunEnd`, perform no
   recovery.
3. If transport or protocol failure occurs after the root ID is known, keep
   the TUI run active, replace transient status with
   `HTTP disconnected; waiting for RUN_ID.`, and poll
   `GET /api/v1/runs/{RUN_ID}`.
4. Poll after 500 ms, 1 s, 2 s, then every 5 s. The TUI remains responsive;
   cancel and steer continue to address the known server run.
5. When durable detail becomes terminal, emit one transport-recovery update
   to the TUI containing run ID, status, and error. Do not fabricate missed
   `RunEvent` values and do not repeat partially rendered assistant output.
6. Finalize the remaining live area with a textual notice that live progress
   was interrupted and, for a finished run, print `Result: :show RUN_ID`.
   Failed and canceled runs include their canonical terminal status and the
   selected error when present.
7. Only after the remote run is terminal may the existing queue continue.

Recovery polling has no elapsed-time cutoff while the TUI stays open. Network
failures use the capped interval and do not produce a new status line on every
attempt. Closing the TUI stops polling and detaches without canceling the run.

If the request fails before response headers expose a run ID, acceptance is
unknown. The adapter does not resubmit or guess from the thread. It reports
`HTTP submission status unknown (REQUEST_ID); queued submissions paused.` The
session enters a failed-connection state: read-only quick commands may still
be attempted, but it rejects new runs and does not drain the queue. Restarting
chat is the explicit recovery path. This rare ambiguity can only be removed by
a future idempotent submission contract.

If a known run later returns `404`, or the endpoint comes back with a different
run identity, recovery fails with the same paused-queue state. No HTTP failure
causes automatic local fallback, because that could create concurrent runs
against the same thread and hide which process owns execution.

The TUI/client boundary gains a transport-recovery update distinct from
`RunEvent` and from `on_error`. `LocalChatSession` never emits it. The TUI owns
its wording and block finalization; execution and API packages remain unaware
of terminal presentation.


## Error Presentation

Errors are classified before presentation:

| Class | Example | TUI ownership |
| --- | --- | --- |
| client selection | active runtime has no endpoint | command exits before header |
| settings validation | invalid model or ceiling restriction | bottom-bar error; previous settings retained |
| submission rejection | missing thread, invalid source | submitted block becomes one rejection diagnostic |
| execution failure | root `RunEnd(status="failed")` | normal run presentation |
| control rejection | terminal steer, inaccessible cancel | bottom-bar `steer failed` or `cancel failed` |
| transport/protocol loss | EOF, unreachable endpoint, invalid SSE | recovery state or paused-queue diagnostic |

HTTP diagnostics begin with `HTTP` when their origin is not otherwise obvious.
The shared client error parser extracts FastAPI `detail` text, removes nested
JSON envelopes, and never exposes a Python traceback. It retains status codes
when they distinguish authentication, missing resources, conflicts, and
validation. Endpoint URLs, response bodies, source text, and policy values are
not copied into generic connection errors.

A canonical failed `RunEnd` is never followed by an HTTP failure for the same
run. If the stream ends immediately after that event, the root event wins.
Similarly, an accepted cancel is not reported as failed merely because the
terminal event arrived first.


## Capability Differences And Unsupported Operations

| Capability | Local | HTTP |
| --- | --- | --- |
| execution owner | TUI process | running resident agent process |
| supported placements | resident, roaming, visiting | resident only |
| setup and environment | rebuilt for the selected layout | server's live setup and environment |
| CLI `--allow` override | supported | rejected before TUI startup |
| session/run `:allow` restriction | supported | supported within server ceiling |
| relative file includes | TUI execution working directory | server execution working directory |
| hosted sandbox | unsupported direct execution | supported only by attaching to an already-running matching runtime |
| exit during a run | local session shutdown cancels owned work | detach; server work continues |
| exact stream replay | not applicable in-process | unsupported; durable terminal polling only |
| runtime lifecycle control | owns only its local session | never starts, stops, or restarts the runtime |

The server may be inside a sandbox whose filesystem differs from the terminal
process. A source include that exists only on the client side fails through
normal source resolution. The client does not upload arbitrary local files or
rewrite paths in this feature.

HTTP chat is not an offline fallback. If the selected resident is running,
users see `[HTTP]` and server errors. To use process-local execution they must
stop the resident runtime first; this feature does not add a bypass flag.


## Compatibility Constraints

- Keep `toolang TARGET chat [THREAD]`, all current target placements, command
  routing, arguments, and option names public.
- Preserve delayed thread creation, thread/run target normalization, input
  history location, queued-setting capture, slash commands, key bindings,
  scrollback behavior, and native event presentation.
- Preserve all current local setup override semantics and local tests. HTTP
  selection is additive for a state that current chat ignores.
- Keep `RuntimeClient` usable by other CLI surfaces as a generic transport;
  do not make it depend on terminal chat types or presentation.
- Keep the existing canonical `RunEvent` types and `/api/v1/runs/stream`
  contract unchanged.
- Make API additions field-strict and additive. Do not remove the historical
  guarantee that run and thread streams have no replay cursor.
- Do not persist client kind in run context solely for display. Thread creation
  remains `client: tui` and produces the existing terminal thread identity.
- Keep noninteractive successful output byte-compatible. New HTTP failure text
  may differ where no current HTTP path exists.
- Coordinate bottom-bar layout changes with #261: `[HTTP]`/`[LOCAL]` is a
  required identity token, while summary fields and run-ID policy remain owned
  by that issue.


## Design Touchpoints And Likely Files

- `src/toolang/cli/toolang/commands/chat/base.py`
  - add transport-neutral client identity and recovery update types;
  - extend `ChatClient` without importing HTTP implementation details.
- `src/toolang/cli/toolang/commands/chat/http.py` (new)
  - implement `HttpChatClient`, policy serialization, strict native event
    decoding, controls, results, close behavior, and durable recovery polling.
- `src/toolang/cli/toolang/commands/chat/main.py`
  - turn `_chat_runtime()` into the resident-status-aware factory;
  - resolve initial HTTP session policy, sandbox matching, health validation,
    and ownership-specific cleanup;
  - keep selected layouts exact for local roaming and visiting chat.
- `src/toolang/cli/toolang/commands/chat/tui.py`
  - pass client identity to header/status widgets;
  - handle recovery and failed-connection updates without treating them as
    execution events;
  - prevent queue drain while remote ownership is unknown.
- `src/toolang/cli/toolang/commands/chat/blocks.py` and `widgets.py`
  - render the textual header/status identity and one bounded recovery notice;
  - enforce narrow-terminal identity priority.
- `src/toolang/cli/common/client.py`
  - expose response headers and an explicitly closable SSE response to the
    adapter while retaining existing JSON/SSE helpers for other callers.
- `src/toolang/api/routers/chat.py` (new), `routers/agent.py`, and `router.py`
  - add validation, authored-source streaming, exact result retrieval, and the
    atomic runnable list endpoint;
  - reuse the existing live relay and canonical SSE helper.
- `src/toolang/api/schemas.py`
  - define strict policy, validation, chat-run, and chat-result schemas.
- `tests/unit/cli/test_chat_http.py` (new)
  - cover adapter payloads, event validation, controls, results, close, errors,
    polling, and no-resubmit guarantees.
- `tests/unit/cli/test_chat_command.py` and `test_chat_tui.py`
  - cover selection, option handling, identity, narrow layout, recovery state,
    and queue gating.
- `tests/integration/api/test_chat.py` (new)
  - cover server-side source/policy parity, SSE headers/events, exact results,
    strict failures, and runnable inspection.
- `tests/system/cli/`
  - add a deterministic PTY scenario backed by an in-process or subprocess
    resident API, including visible `[HTTP]` identity and cancel behavior.
- `docs/api.md`, `docs/chat.md`, and `docs/execution-presentation.md`
  - document selection, new endpoints, ownership differences, recovery, and
    the client identity token.

The chat router may share private run-start/SSE assembly helpers with
`routers/runs.py`; it must not duplicate executor lifecycle logic. Source and
policy parsing stay in their owning execution/language modules, and only the
API composition layer resolves server paths and current snapshots.


## Acceptance Tests

1. **Factory selection**
   - Assert a healthy running resident selects `HttpChatClient` and displays
     no local session construction.
   - Assert a stopped/absent resident, roaming layout, and visiting layout each
     select `LocalChatSession` with the exact layout.
   - Assert starting/preparing, missing endpoint, failed health, and malformed
     health states fail without local fallback.

2. **Configuration and lifetime**
   - Assert HTTP `--default` and `--limit` become validated initial session
     policy, `--allow` is rejected, and local option behavior is unchanged.
   - Assert omitted sandbox attaches to any running sandbox, matching explicit
     selectors attach, and mismatches fail before opening the TUI.
   - Assert HTTP close releases readers without stopping or canceling the
     resident runtime.

3. **Identity and accessibility**
   - Snapshot local and HTTP headers and status bars with exact `[LOCAL]` and
     `[HTTP]` text.
   - Render with color disabled and assert identity remains complete.
   - Cover narrow widths where model, runnable, or shortcuts are removed while
     the client token remains intact.
   - Assert scripted successful output gains no banner.

4. **Policy parity**
   - Validate every allow/default/limit field, reset-to-default/null behavior,
     Decimal cost, empty allow lists, selector lists, and invalid combinations.
   - Assert a failed HTTP policy edit leaves session selectors unchanged.
   - Run the same setup/session/run policy layers through local and HTTP chat
     and assert equivalent resolved runnable, model, ceilings, and limits.

5. **Submission and native streaming**
   - Create a thread lazily, submit authored text with named input and per-run
     policy, and assert the header run ID matches the first `RunBegin`.
   - Cover model deltas, tool steps, child runs, flow statements, successful,
     failed, and canceled roots.
   - Assert every HTTP event round-trips through `run_event_from_data()` and
     matches the local event sequence for the same deterministic execution.
   - Assert relative includes resolve against the server working directory.

6. **Controls and results**
   - Cancel and steer an active HTTP run while its stream reader is blocked;
     assert request IDs, control modes, and one resulting event presentation.
   - Cover the cancel/terminal race without a false error and preserve terminal
     steer errors.
   - Assert `:show RUN_ID` and `:show` match local behavior, including a thread
     with more runs than the ordinary thread-detail window.
   - Assert model, agic, flow, and atomic runnable list responses match local
     shapes and defaults.

7. **Transport and protocol errors**
   - Cover HTTP status detail extraction, invalid JSON, invalid event shape,
     wrong first event, root-ID mismatch, and EOF before root `RunEnd`.
   - Assert a canonical failed `RunEnd` is shown only as execution failure.
   - Assert no exception traceback or raw response body reaches the TUI.

8. **Reconnect recovery**
   - Drop SSE after the response header but before `RunBegin`, during a model
     step, and immediately before terminal state.
   - Assert the POST occurs exactly once, durable polling follows the defined
     schedule, live output is not replayed, controls remain addressable, and
     the queue resumes only after terminal detail.
   - Drop the request before response headers and assert unknown acceptance,
     paused queue, rejected new runs, and no retry or local fallback.
   - Close the TUI during recovery and assert polling stops while the remote
     run remains active.

9. **End-to-end TUI behavior**
   - Start a resident agent API, open its TUI in a pseudo-terminal, assert the
     `[HTTP]` identity, complete one exchange, cancel one exchange, and exit
     without stopping the server.
   - Retain all local PTY scenarios with `[LOCAL]` added to their UI snapshots.

10. **Repository verification**
    - `uv run ruff check .`
    - `uv run ruff format --check .`
    - `uv run ty check`
    - `uv run pytest`


## Risks And Mitigations

- **Automatic selection can hide an unhealthy resident runtime.** Require a
  bounded health check and fail visibly; never conceal the condition through
  local fallback.
- **A second submission after ambiguous transport failure can duplicate
  work.** Never retry the POST, never treat request IDs as replay keys, and
  pause queued/new submissions when acceptance is unknown.
- **A reattached SSE stream would omit missed events.** Do not reattach. Poll
  durable run detail to terminal and direct users to `:show` for the canonical
  result.
- **Closing the TUI can be mistaken for canceling server work.** Keep `[HTTP]`
  visible, document detach semantics, and preserve explicit cancel controls.
- **Client and server policy resolution can drift.** Send authored source and
  canonical session commands to server-owned shared resolvers; keep the
  adapter limited to merge, serialization, and presentation state.
- **CLI `--allow` cannot preserve local override semantics remotely.** Reject
  it explicitly instead of weakening or changing its meaning.
- **Sandboxed servers may not see client-local files.** Resolve includes only
  in the server environment and report the missing path normally; file upload
  is outside scope.
- **Identity additions can conflict with the pending status redesign.** Treat
  the bracketed client token as an orthogonal required field and leave summary
  content/order to #261.
- **Polling can run indefinitely for a long execution.** Use a capped interval,
  one status message, daemon/background ownership, and deterministic shutdown
  when the TUI closes.
- **Dedicated chat endpoints can duplicate generic run orchestration.** Share
  private start and SSE helpers and keep the endpoint limited to source/policy
  adaptation.


## Open Questions

None. This definition selects the client automatically, fixes configuration
authority, defines API adaptation and native streaming, specifies safe
transport recovery, identifies unsupported operations, fixes accessible UI
wording, and names the required compatibility and acceptance boundaries.
