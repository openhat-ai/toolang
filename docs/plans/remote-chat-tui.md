# Remote Chat TUI Integration

Status: Implemented

This definition supersedes the `HttpChatClient` design proposed in #267. The
merged `RunClient` and `RemoteRunClient` now own authored run submission,
canonical SSE decoding, controls, terminal detail, and transport lifecycle, so
the Chat integration must compose those boundaries rather than implement them
again.

## Goal And Success Criteria

Use an already-running resident agent through `RemoteRunClient` from Terminal
Chat while preserving embedded execution for stopped residents, roaming agents,
and visiting agents.

The change succeeds when:

- a healthy running resident is selected automatically and all of its runs use
  `RemoteRunClient`;
- other supported targets retain the current `LocalChatSession` behavior;
- remote model/runnable inspection, thread creation, settings validation, run
  results, controls, and native-event presentation match local Chat behavior;
- the existing banner distinguishes the Chat process version from a compact
  executor value containing its runtime version, port, and optional sandbox
  instance in that order;
- stream loss never retries a submission, falls back to local execution, or
  releases the next queued call while the accepted remote run may still be
  active; and
- public Chat commands, options, scripted mode, and local behavior remain
  compatible.

## Scope

In scope:

- resident runtime detection and remote/local Chat composition;
- a thin `RemoteChatSession` implementing the existing `ChatClient` protocol;
- minimal non-run HTTP operations not owned by `RunClient`;
- remote acceptance and disconnect state in the TUI;
- deterministic unit, API, integration, and PTY coverage; and
- Chat/API documentation updates.

Out of scope:

- manual endpoint flags, authentication, arbitrary remote URLs, and endpoint
  discovery outside the selected resident runtime;
- starting, restarting, stopping, or otherwise managing the runtime process;
- remote execution for roaming or visiting targets;
- SSE replay, event persistence, `Last-Event-ID`, or automatic resubmission;
- WebUI implementation; and
- changes to `RunClient`, `RunRequest`, or canonical `RunEvent` semantics.

## Selection And Configuration

`chat.main::_chat_runtime()` resolves the selected `AgentLayout` first.

1. Roaming and visiting layouts always use `LocalChatSession`.
2. A resident with no active runtime uses `LocalChatSession`.
3. A resident reported as `running` with an endpoint performs a three-second
   `GET /healthz` check, reads runtime identity from `GET /api/v1/profile`, and
   then uses `RemoteChatSession`.
4. `preparing`, `starting`, running without an endpoint, or failed health checks
   fail before the TUI opens. They never fall back to an embedded executor.

After remote selection there is no local fallback. Starting a second executor
against the same resident could create competing owners and conceal an
ambiguous remote submission.

In remote mode the running server owns setup, state, environment, base policy,
working directory, providers, and sandbox. Explicit `--allow`, `--default`, and
`--limit` values become initial session commands and are validated by the
server; local `TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*`, and `TOOLANG_LIMIT_*`
environment overrides are not applied by the TUI. A remote `--allow` can only
restrict the server ceiling. An explicit `--sandbox` must match the running
runtime; omitting it attaches to the runtime's current sandbox. Local option
behavior remains unchanged.

Keep the existing three-row banner and give each value one unambiguous owner:

```text
Toolang   v0.4.0
home      ~/.toolang/agents/eve
executor  embedded
```

`Toolang` is the version of the current Chat TUI process. `home` is always the
host-side agent path resolved by that process. Embedded execution needs no
second version or `host` suffix because it is the same process.

A remote host executor is compactly rendered as `v<version> · :<port>`. A
sandboxed executor appends its driver and short runtime instance ID:

```text
executor  v0.3.9 · :7001
executor  v0.3.9 · :7001 · docker(a1b2c3)
```

The remote version is reported by the server process, not copied from the TUI.
The Docker instance is the first six characters of the container ID; other
non-host drivers use the first six characters of their stable runtime ID. The
`host` sandbox label is omitted. Missing or invalid remote version, port,
driver, or non-host instance metadata fails selection instead of displaying
guessed local values.

`ChatClient.executor_label` remains the only presentation property; do not add
separate version, endpoint, home, or sandbox rows to the protocol. The remote
session formats the value from strict profile metadata and the normalized
endpoint. `RemoteRunClient` exposes its normalized endpoint as a read-only
property so URL validation is not duplicated. Existing responsive folding,
status bar, and scripted successful output remain unchanged.

## Composition

```text
Terminal Chat -> ChatClient
    embedded -> LocalChatSession -> LocalRunClient
    resident -> RemoteChatSession
                    -> RemoteRunClient
                    -> non-run HTTP reads and validation
```

`RemoteChatSession` belongs to the Chat CLI package. It owns one background
asyncio loop and one `httpx.AsyncClient`, injects that HTTP client into
`RemoteRunClient`, and bridges the synchronous callback-based `ChatClient`
surface in the same way as `LocalChatSession`. It closes the run client before
the shared HTTP client. Closing detaches readers and recovery work but never
cancels a run or controls the resident process.

Do not extend the older synchronous `RuntimeClient` with a second SSE path.
`RemoteRunClient` remains the only authored-run transport and native-event
decoder.

### Remote Chat Operations

| `ChatClient` operation | Remote behavior |
| --- | --- |
| executor identity | normalized endpoint plus runtime profile metadata |
| models | existing `GET /api/v1/models` |
| agics / flows | existing `GET /api/v1/agics` and `/api/v1/flows` |
| runnables | combine the two server responses and their exclusive defaults |
| create thread | existing `POST /api/v1/threads` with `client: "tui"` |
| apply settings | merge locally, then validate the complete session remotely |
| explicit result | existing `GET /api/v1/runs/{run_id}` |
| latest result | new latest-result read owned by the thread resource |
| start / wait / stop / steer | `RemoteRunClient` only |
| close | detach and close client-owned HTTP resources |

Responses are decoded into existing execution schemas before use. Run outputs
are converted from `Local` to canonical message parts with the execution value
helper used by local history. HTTP errors retain useful status/detail text but
never expose response bodies, request source, policy values, or tracebacks.

## Minimal API Additions

Add only the non-run behavior that existing endpoints cannot express.

### `GET /api/v1/profile` Runtime Identity

Extend the existing profile response additively with strict runtime metadata:
the server process's Toolang package version and a sandbox projection containing
the driver plus an optional stable instance ID. Host has no instance ID. Docker
uses the first six characters of its container ID; other drivers expose the
first six characters of a stable runtime ID. Keep the existing environment
fields unchanged. This is runtime truth returned by the executor process, not
data copied from the host CLI status file.

### `POST /api/v1/runs/authored/validate`

Accept a strict body containing the complete ordered `session_commands` and
the ordered `runnable_fallbacks`. Reconstruct `RunOverride` values and validate
them against one current setup/state pair using a shared execution helper that
selects the first available fallback before calling `validate_commands()`.
Return `204` on success and `422 detail` on invalid policy or fallback. Do not
create a thread, run, control, or other durable state.

Local Chat uses the same shared helper with `("agic:chat", "default")` so
local and remote settings retain one validation rule.

### `GET /api/v1/threads/{thread_id}/result`

Return the newest succeeded root `RunDetail` with a nonempty output across the
complete thread history. Return `404` with distinct details when the thread is
unknown or has no result. Explicit run lookup continues to use the existing run
detail endpoint. The history package owns the reusable latest-result lookup;
the API router does not inspect records directly.

No chat-specific start, stream, cancel, steer, model, or runnable event endpoint
is added.

## Acceptance, Disconnects, And Queue Safety

The Chat run callback boundary gains a transport-neutral state update in
addition to canonical run events and ordinary definitive errors:

- `accepted(run_id)`: records the addressable root before the first event;
- `disconnected(run_id, message)`: keeps the run active and the queue paused;
- `recovered(detail)`: completes presentation from durable terminal truth
  without fabricating missing `RunEvent` values; and
- `blocked(run_id?, message)`: prevents further submissions when acceptance or
  durable identity cannot be established safely.

Both local and remote sessions emit `accepted` immediately after `start()`
returns. Local Chat emits no other transport state. A matching `RunBegin` still
owns normal presentation, and all received canonical events remain unchanged.

If a remote stream fails after acceptance, the session emits `disconnected`
once and polls the existing run-detail endpoint after 500 ms, 1 s, 2 s, and
then every 5 s. Stop and steer continue to address the accepted ID. At matching
terminal detail, `recovered` finalizes the remaining live presentation with the
actual status, reports that partial output may be inspected with `:show
RUN_ID`, and releases the queue. It does not synthesize `RunEnd` or missing
step events.

A pre-acceptance HTTP rejection is definitive and uses the existing submission
error path. A transport/protocol failure before a valid accepted ID has unknown
acceptance and emits `blocked`; new and queued submissions remain paused until
Chat is restarted. A `404`, mismatched run identity, or invalid detail during
accepted-run recovery also becomes blocked. Read-only slash commands and exit
remain available. Closing stops polling and leaves remote work running.

## Design Touchpoints

- `execution/calls.py` and `history.py`: shared settings validation and latest
  thread-result lookup.
- `execution/remote.py`: normalized endpoint property only; no new run behavior.
- `api/schemas.py`, `conversion.py`, `routers/agent.py`, and the run/thread
  routers: strict runtime identity, validation request, and latest-result
  endpoint.
- `chat/remote.py` (new): remote session, HTTP projections, run composition,
  and recovery polling.
- `chat/base.py`, `events.py`, `main.py`, `tui.py`, and `presenter.py`: compact
  executor identity, run-state updates, selection, blocked queue state, and
  recovered presentation.
- focused execution/API/Chat unit tests, resident CLI integration tests, and
  local/remote PTY tests.
- `docs/api.md`, `docs/chat.md`, and `docs/execution.md`.

Keep HTTP schemas out of execution and Chat packages, native event decoding in
`RemoteRunClient`, history queries in `RunHistory`, and runtime detection at the
CLI composition root.

## Acceptance Tests

1. Select remote only for a healthy running resident; cover every placement and
   active/unready state, endpoint health failure, sandbox match, and no fallback.
2. Preserve local policy behavior; in remote mode cover initial CLI commands,
   server-owned environment, settings validation, decimal/null/list values, and
   rejected widening or invalid selectors.
3. Cover remote lists, combined runnable defaults, lazy thread creation,
   explicit/latest results, local/remote version differences, host and Docker
   executor labels in version/port/sandbox order, six-character instance IDs,
   and unchanged wide/narrow banner layout.
4. Run a remote plain/named/include request and assert accepted ID, native event
   order, terminal detail, controls, queued setting capture, and scripted output.
5. Drop the stream after acceptance and assert one submission, addressable
   controls, bounded detail polling, no replay/fabricated events, actual terminal
   recovery, and queue release only after terminal truth.
6. Drop before acceptance and return missing/mismatched recovery data; assert
   blocked submissions, retained queue, no retry/local fallback, usable read-only
   commands, and detach-on-close.
7. Keep all existing embedded Chat, API, Script, Scheduler, and executor tests
   green, then pass the default repository verification.

## Risks

- Competing execution owners are prevented by fail-closed resident selection.
- Policy drift is prevented by server validation against one snapshot pair.
- Duplicate work after ambiguous submission is prevented by blocking rather
  than retrying or advancing the queue.
- Incomplete streams are represented as transport recovery, never as invented
  native events.
- Endpoint and request data are kept out of user-facing failure details.

## Open Questions

None. Selection, configuration authority, API additions, run composition,
identity, recovery, queue safety, lifecycle, compatibility, and test scope are
decided.
