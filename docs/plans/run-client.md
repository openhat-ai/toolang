# Define the Run Client Boundary

Status: Approved on 2026-08-25.

## Work Type

Feature definition for a behavior-preserving execution boundary. This is a
prerequisite for remote terminal Chat execution and does not implement an HTTP
client or change the agent API.

## Verified Current Behavior

- `RunExecutor` combines run acceptance, local task ownership, durable store
  mutation, control observation, recursive execution, and shutdown.
- `RunSpec` contains process-local `AgentSetup` and `AgentState` snapshots.
- `LocalRunHandle` contains a local `asyncio.Task` and returns a local
  `RunRecord`.
- `LocalChatSession` constructs `RunStore`, `IdIssuer`, `RunExecutor`, setup and
  state watchers, and its own event-loop thread. It calls the executor directly
  for start, stop, steer, and shutdown.
- Other local owners, including Script, Scheduler, Inbox, and `AgentCore`, also
  use `RunExecutor` directly and have independent lifecycle requirements.

## Problem

Terminal Chat cannot substitute a remote run transport while it depends
directly on `RunExecutor` and its local-only types. Copying the complete
`RunExecutor` interface would expose storage, snapshot, task, and ownership
semantics that do not belong to a client boundary.

## Goal

Define a small transport-neutral `RunClient` contract, implement it over the
existing local executor, and migrate only Terminal Chat's embedded execution
path without changing behavior. This is a Chat interaction boundary, not a
replacement abstraction for every `RunExecutor` caller.

## Success Criteria

- Terminal Chat depends on `RunClient`, not `RunExecutor`, for run start,
  stop, steer, and client-owned lifecycle cleanup.
- `LocalRunClient` preserves current authored-input resolution, native event
  order, controls, errors, results, and shutdown behavior.
- The client contract contains no `RunStore`, `IdIssuer`, `AgentSetup`,
  `AgentState`, `asyncio.Task`, `RunRecord`, or HTTP types.
- Existing Script, Scheduler, Inbox, API, and `AgentCore` execution behavior
  remains unchanged; their executor handle imports use the explicit local name.
- No HTTP endpoint, schema, runtime-selection rule, or Chat presentation changes.

## Scope

In scope:

- caller-facing request, handle, and client protocol types;
- renaming the executor-owned `RunHandle` to `LocalRunHandle` so the
  transport-neutral boundary owns the unqualified name;
- a local adapter over `RunExecutor`;
- migration of `LocalChatSession` to that adapter;
- focused unit, integration, and existing Chat regression coverage;
- concise execution-architecture documentation.

Out of scope:

- `RemoteRunClient`, `HttpChatClient`, runtime detection, or endpoint discovery;
- WebUI implementation or changes to any external WebUI repository;
- adding or changing API endpoints and request schemas;
- retry, rerun, validation, pending-control cancellation, inspection, thread
  management, model/runnable listing, or result lookup in `RunClient`;
- migrating Script, Scheduler, Inbox, API routers, or `AgentCore`;
- changing `RunExecutor`, `RunSpec`, or `LocalRunHandle` behavior beyond the
  approved type rename;
- changing terminal Chat commands, policy, rendering, or error text.

## Boundary Design

`RunExecutor` remains the process-local execution engine. `RunClient` is the
smaller caller boundary above it:

```text
Terminal Chat
    -> RunClient
        -> LocalRunClient
            -> resolve current request to RunSpec
            -> RunExecutor
```

The client is capability-driven rather than a mirror of `RunExecutor`. Add only
the operations currently needed by Terminal Chat. Later clients may motivate
separate additions, but they must not be added speculatively.

Terminal Chat is the only consumer in this implementation. A future WebUI run
client is expected to follow the same request, native-event, control, and
terminal-detail shape, but that similarity does not expand this change into a
shared UI framework or a migration of non-Chat execution owners.

### `RunRequest`

Add an immutable caller request containing:

- `thread`: existing thread ID;
- `commands`: per-run `RunOverride` values;
- `input`: parsed `RunnableInputRaw`;
- `session_commands`: captured session `RunOverride` values;
- `runnable_fallbacks`: ordered, explicit call-site candidates;
- `request_id`: caller-issued idempotency identifier.

The request stays above execution resolution. It contains authored input and
policy, but no setup/state snapshots, include callback, resolved resources, or
transport representation. Terminal Chat passes `("chat", "default")`; the
client selects the first candidate present in the same state snapshot used for
resolution. This expresses call-site policy without reading state twice or
asking the execution layer to invent a default.

Place this caller-facing value with the execution protocol schemas. It may
depend on language and execution value types, but not on stores, watchers,
runtime services, API schemas, or event emitters.

### `RunHandle`

Define a handle protocol with:

- immutable `run_id`;
- `wait()`, which completes when the accepted root run becomes terminal and
  returns caller-facing `RunDetail`.

Do not expose `LocalRunHandle` through the client contract. Stop and steer
remain client methods so the protocol handle has no back-reference to a
concrete implementation.

### `RunClient`

The initial protocol contains:

```python
class RunClient(Protocol):
    async def start(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    async def stop(
        self,
        run_id: str,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlInfo: ...

    async def steer(
        self,
        run_id: str,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> ControlInfo: ...

    async def close(self) -> None: ...
```

All operations are async so callers do not depend on whether work is performed
in-process or across a future transport. `close()` releases resources owned by
the client. It does not define a universal "cancel every referenced run"
operation; the local implementation cancels its runs only because it owns its
executor, matching current embedded Chat behavior.

Do not include:

- `store`, `ids`, task access, or direct record access;
- `validate`, because Chat session validation remains its existing concern;
- `retry`, `rerun`, or `cancel_control`, because embedded Chat does not use them;
- thread, history, result, model, or runnable operations, which belong to other
  concepts and remain on `LocalChatSession` for now.

## `LocalRunClient`

`LocalRunClient` adapts an owned `RunExecutor` and receives narrow dependencies
for current setup, current state, and include resolution. It must not own or
construct watchers.

On `start()` it:

1. reads setup and state exactly once;
2. selects the first explicit runnable fallback present in that state;
3. calls the existing `resolve_spec()` with the request's run commands, input,
   session commands, selected fallback, and local include resolver;
4. calls `RunExecutor.start()` with the request ID and tracer;
5. returns a client handle wrapping only the accepted run ID and an internal
   await operation;
6. converts the terminal durable result to `RunDetail` through `RunHistory`.

On `stop()` and `steer()` it delegates to the executor and converts the accepted
control to caller-facing `ControlInfo`. It preserves timing, request ID, reason,
message validation, and existing exceptions.

`close()` is idempotent and calls `RunExecutor.shutdown()` once. The surrounding
`LocalChatSession` continues to close its store after the client has closed.

The adapter does not duplicate preparation, resource resolution, persistence,
control observation, or execution logic.

## Local Chat Migration

`LocalChatSession` continues to own:

- store, IDs, thread manager, setup/state watchers, and event-loop thread;
- model/runnable listing, thread creation, settings validation, and result lookup;
- Chat-specific conversion from callback functions to `RunTracer`.

It constructs one `LocalRunClient` around its executor and passes snapshot and
include dependencies. Its run path parses the authored call into `RunRequest`,
then awaits `client.start()` and the returned handle. Stop and steer delegate to
the client. Shutdown closes the client before stopping watchers and closing the
event loop.

The `ChatClient` presentation protocol is unchanged in this prerequisite. Its
synchronous callback surface continues to bridge to the owned event-loop thread.

## Design Touchpoints

- `src/toolang/execution/schemas.py`: immutable `RunRequest`.
- `src/toolang/execution/client.py` (new): `RunClient`, `RunHandle`, local
  adapter, and private local handle implementation.
- `src/toolang/cli/toolang/commands/chat/local.py`: construct and use the client
  while retaining Chat-owned inspection and watcher behavior.
- `tests/unit/execution/test_run_client.py` (new): protocol and local adapter.
- existing Chat command, TUI, local execution, and system PTY tests.
- `docs/execution.md`: distinguish caller client from process-local executor.

Keep the execution package facade narrow; callers may import from
`toolang.execution.client` instead of expanding `toolang.execution.__init__`.

## Acceptance Tests

1. `RunRequest` rejects invalid field shapes and contains no local runtime or
   transport values.
2. Local start captures one setup/state pair, preserves include resolution and
   session/run policy precedence, returns the accepted ID, forwards every native
   event once, and resolves `wait()` to matching `RunDetail`.
3. Preparation rejection creates no run and surfaces the existing error.
4. Stop and steer preserve timing, request IDs, reason/message payloads, and
   caller-facing control results.
5. Closing is idempotent, cancels and awaits client-owned active runs, and leaves
   them durably terminal before the store closes.
6. Local Chat model/runnable lists, settings, thread creation, result lookup,
   plain input, named input, includes, prompts, queueing, cancellation, steering,
   scripted mode, and presentation remain unchanged.
7. Script, Scheduler, Inbox, API, `AgentCore`, and direct `RunExecutor` tests have
   no production changes and remain green.
8. The default offline verification suite passes.

## Risks

- A protocol that mirrors `RunExecutor` would preserve local-only coupling;
  the closed initial method set prevents that expansion.
- Capturing setup and state at different times could produce an inconsistent
  request; the local adapter captures both together before resolution, matching
  the current Chat execution boundary.
- Returning local records would make a future remote implementation impossible;
  the handle returns caller-facing `RunDetail` instead.
- Moving watcher or thread ownership into the adapter would mix lifecycle
  concerns; `LocalChatSession` retains them.

## Follow-up

After this foundation is implemented and verified, define `RemoteRunClient`
separately. That definition must map this actual protocol to existing API
capabilities and add only endpoints required by uncovered operations. HTTP Chat
selection, stream recovery, and UI identity belong to that follow-up. A future
WebUI client may use the same server contract, but its implementation remains a
separate surface concern.

The RunClient prerequisite must have its own implementation issue and pull
request. It does not close the existing HTTP Chat definition issue.

## Open Questions

None. Scope is limited to the initial protocol, local adapter, and embedded Chat
migration.
