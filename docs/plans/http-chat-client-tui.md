# HTTP Chat Client Support In The Chat TUI

## Goal

Use an already-running resident agent through HTTP from the terminal Chat TUI,
while retaining process-local chat for stopped and nonresident agents. Identify
the selected client textually and preserve canonical `RunEvent` presentation.

Implementation starts only after this definition is approved.


## Success Criteria

- A healthy running resident uses `HttpChatClient`; a resident without an
  active runtime, a roaming agent, and a visiting agent use `LocalChatSession`.
- HTTP chat supports the current `ChatClient` operations through the resident
  API without introducing a second event vocabulary.
- The header and bottom bar always show `[HTTP]` or `[LOCAL]` as text.
- Stream loss never resubmits a run or falls back to a local executor.
- Local chat, public commands, native event rendering, and scripted successful
  output remain compatible.


## Current Behavior

`src/toolang/cli/toolang/commands/chat/main.py::_chat_runtime()` always creates
`LocalChatSession`, even when the selected resident already has a running API.
The local session opens the selected run store, owns an event-loop thread and
watchers, starts `RunExecutor` directly, and sends native events to the TUI.
Direct chat rejects sandbox drivers other than `none`.

`RuntimeClient` already provides JSON and SSE transport. The API already owns
thread creation, run inspection/control, model and executable inspection, and
canonical run streams, but its generic run request accepts resolved inputs
rather than the authored source and session policy used by local chat.
`RuntimeClient` therefore does not satisfy the current `ChatClient` protocol.

The TUI is transport-neutral and creates a thread lazily. It captures settings
with queued calls, renders `RunEvent` values through `ChatRunPresenter`, and
uses the client for settings, lists, controls, and `:show`. Its header and
status bar do not identify the client or define connection-loss behavior.


## Scope

In scope:

- HTTP/local selection in the existing chat composition path;
- an `HttpChatClient` adapter and the minimal API needed for authored chat;
- canonical streaming, controls, durable result reads, and safe stream-loss
  recovery;
- accessible client identity and bounded connection feedback;
- deterministic unit, API integration, and PTY acceptance coverage;
- chat, API, and execution-presentation documentation updates.

Out of scope:

- manual client or endpoint flags, authentication, and remote URLs;
- automatically starting, restarting, or stopping an agent runtime;
- HTTP chat for roaming or visiting targets;
- event persistence, replay IDs, `Last-Event-ID`, or alternate events;
- conversation-history restoration and the summary redesign owned by #261;
- database changes or changes to generic `/api/v1/runs/stream` submission.


## Client Selection And Lifetime

The composition root resolves the exact `AgentLayout`, then:

1. Uses local chat immediately for `roaming` and `visiting` placement.
2. Reads `AgentProcess(layout).status()` for a resident.
3. Uses local chat when no active status exists.
4. For `running` with an endpoint, requires `GET /healthz` to return
   `{"ok": true}` within three seconds, then constructs
   `HttpChatClient(RuntimeClient(endpoint))`.
5. Fails before opening the TUI for `preparing`, `starting`, running without an
   endpoint, or an advertised endpoint that fails its health check.

There is no fallback after HTTP is selected: it could create a second executor
against the same thread and hide execution ownership. To request local chat,
the user stops the resident runtime first.

Closing a local client retains current executor shutdown. Closing an HTTP
client closes response bodies and recovery workers but never controls the
resident process. Exiting an HTTP TUI detaches from an active run; cancellation
remains an explicit Ctrl-C, double-Escape, or run-control action.

The TUI and scripted loop receive `ChatClient`, not `RuntimeClient`, and do not
inspect concrete client classes.


## Identity And Status

`ChatClient` exposes immutable identity metadata:

| Kind | Label |
| --- | --- |
| `local` | `LOCAL` |
| `http` | `HTTP` |

The startup header adds `client: LOCAL` or `client: HTTP`. The bottom bar begins
with `[LOCAL]` or `[HTTP]`, followed by model, runnable, and hints. The bracketed
token is never replaced by color or removed at narrow widths; lower-priority
content truncates first. The header keeps the selected home and omits the
endpoint.

Scripted successful output gains no banner. HTTP-origin failures still use an
`HTTP` diagnostic prefix where the source would otherwise be ambiguous.


## Configuration And Policy

The running server owns setup, prepared state, environment, ceiling, bindings,
limits, working directory, providers, and sandbox in HTTP mode.

| Chat option | Local | HTTP |
| --- | --- | --- |
| `--default` | existing setup binding override | initial validated session default |
| `--limit` | existing setup limit override | initial validated session limit |
| `--allow` | existing setup ceiling override | rejected before TUI startup |
| no `--sandbox` | direct `none` execution | attach to existing sandbox |
| `--sandbox VALUE` | existing local validation | require the running sandbox to match |

HTTP rejects CLI `--allow` because the local option can replace a setup
ceiling, while a request to a running server may only narrow its ceiling.
Users configure and restart the runtime for a base ceiling, or use session
`:allow` for an additional restriction.

All policy-only `:allow`, `:default`, and `:limit` commands remain supported.
`HttpChatClient.apply_settings()` merges the complete candidate with shared
policy helpers, validates it on the server, and commits presentation selectors
only on success. Per-run policy stays in the authored source, preserving setup
then session then run precedence.

Policy JSON preserves unrestricted allow as `null`, denied allow as `[]`,
selector tuples as arrays, and decimal cost as text reconstructed to `Decimal`.
Schemas reject unknown fields and extra keys.


## HTTP API Contract

Add `/api/v1/chat` as a source-and-policy adapter over the existing executor.
It does not define new execution events.

### `POST /api/v1/chat/validate`

Accept an ordered array of canonical policy commands. Reconstruct
`PolicyCommand` and call `validate_commands()` against current setup/state,
using agic `chat` when present and otherwise `default`. Return `204` on success
or `422 detail` on invalid policy. Do not create durable state.

### `POST /api/v1/chat/stream`

The strict request contains:

- `thread`: an existing thread ID;
- `request_id`: a unique start-control request ID;
- `source`: normalized authored chat source, including per-run policy and named
  input syntax;
- `session_commands`: the complete canonical session policy.

Use the same `parse_call()`, `resolve_spec()`, policy layering, default
runnable, snapshots, and include resolver as `LocalChatSession`. Relative
includes use the server setup working directory, falling back to agent home.
Start through the process-owned executor and existing live relay.

Return the canonical root-run SSE protocol and terminate after root `RunEnd`.
The response includes `X-Toolang-Run-ID` after durable acceptance and before
event consumption. Missing thread returns `404`; pre-acceptance source, policy,
input, runnable, or resource rejection returns `422`; post-acceptance execution
failure remains a canonical `RunEnd`.

### `GET /api/v1/chat/result`

Require exactly one of `run_id` or `thread_id`. The run form returns that run's
resolved nonempty output. The thread form returns the newest visible finished
root with nonempty output, without a fixed history window. Return canonical
message parts and `run_id`, preserving local missing/no-result distinctions.

### `GET /api/v1/runnables`

Return the effective default plus combined `{kind, name}` agic and flow items
from one setup/state snapshot. Do not synthesize this list client-side from
separate generations.

Existing endpoints remain responsible for thread creation (`client: "tui"`),
models, agics, flows, run detail, cancel, and steer. Generic run submission is
unchanged.


## `HttpChatClient` Operations

`HttpChatClient` belongs to the chat package; `RuntimeClient` remains a generic
transport.

| `ChatClient` operation | HTTP behavior |
| --- | --- |
| identity | immutable `http` / `HTTP` |
| list | existing model/agic/flow endpoints and atomic runnable endpoint |
| create thread | `POST /threads` with `client: tui` |
| apply settings | merge locally, validate complete candidate remotely |
| start | `POST /chat/stream`, decode native events |
| stop | immediate `POST /runs/{id}/cancel` |
| steer | next-step `POST /runs/{id}/steer` |
| result | `GET /chat/result` |
| close | stop client readers/recovery only |

Each start, cancel, and steer uses a fresh `term_<uuid>` request ID. A queued
call sends the exact session policy captured with it.

Decode SSE objects with `run_event_from_data()`. Treat malformed JSON, invalid
events, a first event other than root `RunBegin`, header/event root mismatch,
root identity changes, and EOF before root `RunEnd` as transport/protocol loss.
Pass valid events unchanged; never construct presentation events.

Controls run concurrently with the stream reader and do not synthesize run
events. If cancel races with terminal state, confirm terminal detail and avoid
a false failure. Steering a terminal or unknown run remains an error.


## Stream Loss And Recovery

Because events have no replay cursor, the client must not reattach to SSE and
present an incomplete stream, or retry the submission POST. Request IDs are
uniqueness guards, not replay keys.

After `X-Toolang-Run-ID` is known, stream loss keeps the TUI run active and
shows `HTTP disconnected; waiting for RUN_ID.` Poll run detail after 500 ms,
1 s, 2 s, then every 5 s. The TUI remains responsive and controls continue to
address the known run.

At terminal detail, send one transport-recovery update containing ID, status,
and error. The TUI removes remaining live state without fabricating events or
repeating partial assistant output. A finished run prints
`Result: :show RUN_ID`; failed/canceled runs print their terminal status and
selected error. The queue resumes only after terminal detail.

Polling continues at the capped interval while the TUI is open, without
repeating status messages. Closing the TUI stops polling and leaves server work
running.

If failure occurs before response headers reveal the run ID, acceptance is
unknown. Do not resubmit or guess from thread state. Show
`HTTP submission status unknown (REQUEST_ID); queued submissions paused.`,
reject new runs, and require chat restart. Use the same paused state if a known
run later returns `404` or mismatched identity. Read-only quick commands may
still be attempted.

The TUI/client boundary adds one transport-recovery update type distinct from
`RunEvent` and ordinary `on_error`; local chat never emits it.


## Error And Capability Contract

| Condition | Behavior |
| --- | --- |
| selection/readiness failure | exit before the header; no local fallback |
| settings rejection | bottom-bar error; retain prior settings |
| pre-acceptance submission rejection | one submitted-block diagnostic |
| execution failure | normal failed `RunEnd` presentation |
| control rejection | one `cancel failed` or `steer failed` status |
| transport/protocol loss | durable recovery or paused-queue diagnostic |

Extract FastAPI `detail`, retain useful status codes, and omit tracebacks, raw
bodies, endpoints, source, and policy values. A received root `RunEnd` wins over
subsequent EOF; an accepted cancel is not failed because terminal state won a
race.

HTTP-specific limitations are:

- resident placement and discovered local runtime only;
- server environment and ceiling are authoritative;
- CLI `--allow` is unsupported;
- includes resolve in the server filesystem, which may differ in a sandbox;
- exit detaches rather than cancels;
- missed event replay is unsupported;
- the client never manages runtime lifecycle or uploads client-only files.


## Compatibility Constraints

- Keep `toolang TARGET chat [THREAD]`, target routing, arguments, and option
  names.
- Preserve lazy thread creation, target normalization, input-history location,
  queued-setting capture, slash commands, keys, scrollback, and native event
  presentation.
- Preserve local setup overrides and noninteractive successful output.
- Keep `RuntimeClient` independent of chat presentation and keep canonical
  events plus `/api/v1/runs/stream` unchanged.
- Make API additions strict and additive; do not persist client kind merely for
  display or add replay claims.
- Treat `[HTTP]`/`[LOCAL]` as the required client field while #261 owns other
  status and summary decisions.


## Design Touchpoints

- `chat/base.py`: client identity and recovery types; protocol additions.
- `chat/http.py` (new): adapter, policy serialization, events, controls,
  results, close, and polling.
- `chat/main.py`: status-aware factory, health, initial policy, sandbox match,
  and ownership cleanup.
- `chat/tui.py`, `blocks.py`, `widgets.py`: identity, recovery finalization,
  queue gating, and narrow layout.
- `cli/common/client.py`: response headers and closable SSE response without a
  chat dependency.
- `api/routers/chat.py` (new), `routers/agent.py`, `router.py`, `schemas.py`:
  strict endpoints reusing run-start/live-relay helpers.
- `tests/unit/cli/test_chat_http.py`, existing chat command/TUI tests,
  `tests/integration/api/test_chat.py`, and system CLI PTY tests.
- `docs/api.md`, `docs/chat.md`, and `docs/execution-presentation.md`.

Keep source and policy parsing in owning execution/language modules. The API
composition layer supplies current snapshots and server path resolution; it
must not duplicate executor lifecycle logic.


## Acceptance Tests

1. Select HTTP only for a healthy running resident; cover every local placement,
   active/unready state, health failure, and no-fallback rule.
2. Cover HTTP defaults/limits, rejected CLI allow, session/run policy parity,
   decimal/null/list serialization, sandbox match, and unchanged local policy.
3. Snapshot `[HTTP]`/`[LOCAL]` in header/status with color disabled and narrow
   widths; retain scripted output.
4. Assert lazy thread creation, authored text/named input/include resolution,
   header/root ID match, canonical recursive events, and all terminal states.
5. Cover concurrent cancel/steer, cancel-terminal race, lists, explicit and
   latest `:show`, including history beyond the normal detail window.
6. Cover HTTP details, malformed protocol, wrong first/root event, EOF, and
   single execution/transport diagnostics without tracebacks.
7. Drop SSE before `RunBegin`, during work, and before terminal state; assert
   one POST, polling schedule, addressable controls, no replay, and queue resume
   only at terminal.
8. Drop before headers; assert unknown acceptance, paused queue, rejected new
   runs, and no retry/fallback. Close during recovery and retain remote work.
9. Run resident-HTTP and local PTY exchanges, cancellation, visible identity,
   and HTTP detach without stopping the server.
10. Run `uv run ruff check .`, `uv run ruff format --check .`,
    `uv run ty check`, and `uv run pytest`.


## Risks And Mitigations

- **Unhealthy runtime hidden by fallback:** health-check and fail visibly.
- **Duplicate work after ambiguity:** never retry; pause mutating submissions.
- **Incomplete replay:** poll durable terminal truth and direct users to
  `:show` instead of reattaching.
- **Policy drift:** resolve authored source and policy on the server; reject the
  one CLI override that cannot preserve meaning.
- **Unexpected detach:** keep `[HTTP]` visible and cancellation explicit.
- **Sandbox file mismatch:** use server path semantics and do not invent upload.
- **Status redesign conflict:** reserve only the bracketed identity token.
- **Long recovery:** cap polling at five seconds and stop it on TUI close.


## Open Questions

None. Client selection, configuration authority, API adaptation, identity,
streaming, controls, recovery, unsupported operations, compatibility, and
acceptance behavior are decided.
