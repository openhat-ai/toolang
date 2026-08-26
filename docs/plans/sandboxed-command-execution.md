# Define Sandboxed Command Execution

Status: Approved on 2026-08-26.

## Work Type

Feature definition for selecting, attaching to, and owning the execution runtime
used by interactive and one-shot CLI commands.

## Verified Current Behavior

- `too run` and `too start` resolve `--sandbox`, then the merged root/agent
  `[sandbox]` binding, then `host`; they start an AgentServer through the sandbox
  lifecycle.
- Terminal Chat accepts `--sandbox`. It attaches to a running resident
  AgentServer, rejects an explicit mismatch, and otherwise creates an embedded
  host `LocalChatSession`. It cannot start a command-owned guest runtime and
  ignores a configured non-host sandbox when no server is running.
- Direct `.too` script commands have no `--sandbox` option and construct a local
  `RunExecutor` in the CLI process.
- `retry` and `rerun` construct a local `RunExecutor` even when an AgentServer
  already owns execution for the same agent. Their existing HTTP endpoints start
  execution before a separate event subscription can be established.
- `RunClient` deliberately contains only start, stop, steer, and close. Its local
  and HTTP implementations provide equivalent authored-run behavior, and the
  HTTP start-and-stream operation subscribes before execution can emit events.
- Root preparation controls persist resources, limits, state, runnable, model,
  and locals, but not the sandbox in which the run was accepted.
- Sandbox lifecycle code already resolves canonical selectors, waits for API
  readiness, reports staged startup progress, stores host-only recovery state,
  and stops and releases command-owned workloads after foreground interruption.

## Problem

Commands independently decide whether to create a local executor, attach to an
AgentServer, or reject a sandbox request. Extending each command separately
would duplicate runtime-status, selector-matching, startup, interruption, and
ownership rules. It would also leave retry able to resume durable work in a
different environment from the source run.

## Goal

Make sandbox an execution-runtime startup property, give CLI commands one shared
runtime-selection and ownership boundary, and preserve the source environment
when retrying durable work.

## Success Criteria

- Chat, Script, Retry, and Rerun use the same active-runtime and sandbox
  selection rules.
- A healthy running AgentServer is reused; an explicit incompatible sandbox is
  rejected before any run mutation.
- Without an active server, `host` remains embedded and a non-host sandbox uses
  a command-owned temporary AgentServer.
- Commands stop only the temporary AgentServer they created, including after
  interruption or failure.
- Every newly accepted root preparation control records the canonical actual
  sandbox.
- Retry has no `--sandbox` option and cannot execute without proving that the
  current runtime matches the source run's sandbox.
- Rerun is a new run and may select a new sandbox.
- Local and HTTP restart paths resolve the same request, subscribe before the
  first event, and return the same terminal detail.
- Existing `run` and `start` behavior remains unchanged.

## Scope

In scope:

- one CLI-owned execution-runtime selection and lifecycle boundary;
- temporary AgentServer startup for non-host Chat, Script, Retry, and Rerun;
- `--sandbox` for direct Script and Rerun commands;
- canonical sandbox provenance on root start, rerun, and retry controls;
- a separate retry/rerun client boundary with local and HTTP implementations;
- atomic retry/rerun start-and-stream HTTP operations;
- consistent progress, diagnostics, interruption, and cleanup behavior;
- deterministic unit, CLI, API, and sandbox-lifecycle tests;
- Linux, macOS, and WSL2 behavior supported by the existing sandbox plugins.

Out of scope:

- changing the public behavior of `run`, `start`, `stop`, or sandbox plugins;
- adding sandbox selection to Retry;
- Inbox execution, which requires a separate ownership refactor;
- Rewind and Fork, which mutate thread history and do not start an executor;
- a general replacement for `RunExecutor`, `RunClient`, or `ChatClient`;
- persistent runtime pooling, authentication, remote agent discovery, or WebUI;
- native Windows support beyond the existing WSL2 path;
- custom model catalog work or plugin configuration redesign.

## Execution Runtime Boundary

Add a CLI orchestration value named `ExecutionRuntime` and an owning context
operation such as `open_execution_runtime()`. A runtime reports:

- the canonical actual sandbox selector;
- `embedded` or `remote` execution mode;
- the AgentServer endpoint when remote;
- whether the current command owns the remote workload.

The runtime does not execute runs or construct a universal client. Chat creates
its `ChatClient` from the result, while Script and restart commands create their
run client. This avoids coupling Chat-only thread and settings operations to the
smaller execution client contract.

The boundary belongs to CLI orchestration. Sandbox parsing and lifecycle remain
in `toolang.up.sandbox`; runtime status remains in `toolang.up.process`; local
execution remains in `toolang.execution`. Core execution receives concrete
resolved inputs and does not read CLI configuration.

## Runtime Selection

Selection is performed once per command after its `AgentLayout` is materialized:

| Observed runtime | Request | Result |
| --- | --- | --- |
| Healthy running AgentServer | omitted or compatible | Attach; the command does not own it |
| Healthy running AgentServer | explicitly incompatible | Reject before run mutation |
| Preparing or starting | any | Reject and ask the user to wait |
| Running with incomplete status, failed health, or inconsistent API identity | any | Reject; never fall back to embedded execution |
| Failed runtime requiring recovery | any | Reject with the existing recovery diagnostic |
| Stopped or absent | effective sandbox is `host` | Create an embedded host executor |
| Stopped or absent | effective sandbox is non-host | Launch and attach to a temporary AgentServer |

An active runtime wins over current configuration when no explicit selector is
given. For an inactive runtime, the effective selector is resolved in this
order:

1. the command's explicit `--sandbox`, when the command supports it;
2. the merged root/agent `[sandbox]` binding;
3. `host`.

Selector comparison preserves the current useful shorthand: a driver-only
request such as `docker` matches a canonical running selector using that driver,
while a request containing a spec must equal the canonical selector. Comparison
is trimmed and case-sensitive.

The API runtime identity is authoritative after attachment. A status-file/API
sandbox mismatch is an error and cannot silently choose either value.

## Ownership And Lifecycle

An attached runtime is never stopped, restarted, or reconfigured by a command.
Closing its client releases only client-owned transport resources.

A temporary runtime:

1. uses the existing sandbox selection, environment, mount, port, readiness,
   startup-progress, and recovery paths;
2. captures AgentServer output in the agent log so it cannot corrupt Chat or
   Script presentation;
3. yields only after the API is ready and its identity matches the selected
   sandbox;
4. on normal completion, stops and releases the workload;
5. on startup failure, run failure, transport failure, or Ctrl-C, first requests
   cancellation of an accepted active run when possible, then stops and releases
   the workload;
6. preserves existing recoverable sandbox state and reports the log path if
   cleanup itself fails.

Only the command that successfully launched the temporary runtime owns this
cleanup. A launch race that discovers another active workload must be resolved
as an active-runtime conflict, not by stopping that workload.

This feature does not add a cross-process lease for concurrent embedded host
commands. The existing limitation that multiple local CLI processes can open
the same agent remains separate work.

## Command Behavior

| Command | Sandbox input | No active runtime | Active runtime |
| --- | --- | --- | --- |
| Chat | Existing `--sandbox` | Embedded host or temporary guest | Attach; reject explicit mismatch |
| Direct Script | Add runnable-level `--sandbox` | Embedded host or temporary guest | Attach; reject explicit mismatch |
| Retry | No option | Use source provenance; embedded host or temporary guest | Require source provenance to match the active sandbox |
| Rerun | Add `--sandbox` | Embedded host or temporary guest | Attach; reject explicit mismatch |

The direct Script syntax is `too FILE.too RUNNABLE --sandbox SELECTOR`. Script
keeps its current dynamic runnable parsing, explicit-runnable validation,
stdout result contract, stderr progress, `--quiet`, `--save`, log reporting, and
exit codes.

Chat keeps its current local and remote presentation. A configured guest
sandbox now starts a temporary remote session when no AgentServer is active.

Retry reopens the same root run and therefore must preserve its environment.
It ignores current `[sandbox]` configuration and derives the required selector
only from the latest preparation control of the source run. Rerun creates a new
root run, so it follows normal runtime selection and records the new environment.

## Sandbox Provenance

Add nullable `sandbox` provenance to the durable start, rerun, and retry
preparation payloads. New root controls must write the nonempty canonical value
captured by `AgentSetup.environment.sandbox`; nested controls may omit it.
Serialization continues to read older payloads that lack the field as unknown
provenance.

The executor enforces retry consistency before accepting a retry control:

- read the source run's latest preparation control;
- reject if its sandbox provenance is missing;
- compare it with the current setup's canonical actual sandbox;
- reject a mismatch without changing the run;
- write the same canonical sandbox to the accepted retry control.

This invariant belongs in execution, not only the CLI or HTTP adapter, so direct
API and local callers cannot bypass it. The diagnostic for an older run without
provenance tells the user to use Rerun, which can safely choose a new sandbox.
No value is inferred from current configuration, process location, or historical
runtime status.

## Restart Client And HTTP Contract

Do not expand the Chat-focused `RunClient`. Add a small `RunRestartClient`
boundary with `retry`, `rerun`, and `close`, reusing the existing `RunHandle`,
`RunDetail`, native `RunEvent`, `RunTracer`, and `RunOverride` vocabulary.

A restart request contains the source run, caller-issued request ID, ordered
policy overrides, and an optional retry anchor. Retry and rerun are separate
methods so invalid combinations are unrepresentable. A shared execution-owned
resolver applies the overrides to one current setup/state snapshot; local and
HTTP implementations call that resolver rather than duplicating CLI policy
logic.

Add authored retry-and-stream and rerun-and-stream endpoints. Each endpoint:

1. validates and resolves the complete request on the AgentServer;
2. accepts the retry or rerun durably;
3. subscribes to the native event relay in the same owner event-loop turn;
4. returns the accepted root run ID in the existing header;
5. streams through the matching root `RunEnd`;
6. leaves an accepted run executing if the subscriber disconnects.

The remote client follows the existing `RemoteRunClient` protocol rules: no
automatic submission retry, no incomplete-stream recovery without replay, no
fabricated events, and `wait()` fetches terminal `RunDetail` after a complete
stream. Existing non-streaming retry/rerun endpoints remain available and gain
the same executor-level sandbox validation.

## Presentation And Errors

- Runtime startup progress is written to stderr and reuses the existing staged
  sandbox presentation.
- Native run progress and run/step footers are reused for Script, Retry, and
  Rerun in both local and remote modes.
- Script result output remains on stdout; AgentServer bootstrap and logs never
  enter stdout.
- Sandbox mismatch errors name both requested/required and active canonical
  selectors and make no state change.
- Ctrl-C returns exit code `130` after best-effort run cancellation and owned
  runtime cleanup. A cleanup failure is also reported without hiding the
  interruption.

## Implementation Sequence And Touchpoints

After this definition is approved, use four implementation PRs:

1. **ExecutionRuntime and Chat:** add the shared runtime boundary under
   `src/toolang/cli/common`, reuse `toolang.up` lifecycle/status operations, and
   migrate Chat as the first consumer.
2. **Script:** add its runnable-level option, adapt local/remote run startup to
   `ExecutionRuntime`, and preserve output and interruption behavior.
3. **Sandbox provenance:** update execution records, serialization, store
   acceptance, schemas, history projection, and executor retry validation.
4. **Retry and Rerun:** add the restart request/client, local and HTTP adapters,
   atomic stream routes, CLI runtime selection, and final presentation.

Likely files include:

- `src/toolang/cli/common/` for execution-runtime orchestration;
- `src/toolang/cli/toolang/commands/chat/`, `script.py`, and `thread.py`;
- `src/toolang/up/sandbox.py` and `process.py` for narrow reusable lifecycle and
  status operations;
- `src/toolang/execution/records.py`, `store.py`, `schemas.py`, `executor/`, and
  new restart client modules;
- `src/toolang/api/schemas.py` and `routers/runs.py`;
- corresponding unit and integration test modules and CLI/API documentation.

## Acceptance Tests

- Runtime-selection table coverage for absent, stopped, preparing, starting,
  healthy, incomplete, inconsistent, and failed runtime states.
- Exact and driver-only selector matching, including Docker selectors with
  image tags containing colons.
- Chat and Script use embedded host execution, attach to an existing host or
  Docker AgentServer, and create and clean up a temporary Docker AgentServer.
- Script preserves stdout/stderr separation, quiet/save behavior, terminal
  status, Ctrl-C exit code, and run/step footers in local and remote modes.
- Attached AgentServers survive command completion and Ctrl-C; owned temporary
  AgentServers do not.
- New root start/rerun/retry controls round-trip canonical sandbox provenance;
  older payloads round-trip with missing provenance.
- Retry succeeds only on the source sandbox and rejects missing or mismatched
  provenance before adding a control.
- Rerun can select a different sandbox and records it on the new root run.
- Local and remote restart clients produce equivalent native event order and
  terminal detail; atomic HTTP tests prove no first-event subscription gap.
- Default offline tests do not require Docker; existing opt-in sandbox lifecycle
  coverage exercises supported host platforms.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest` pass for every implementation PR.

## Risks

- Temporary AgentServer cleanup spans transport and sandbox lifecycles. Ownership
  must be explicit so an attached user-managed workload is never stopped.
- Provenance changes durable payloads. Readers must distinguish older missing
  data from a newly written invalid value without guessing.
- Script currently owns local state preparation and result storage. Its remote
  migration must keep server-owned resolution while preserving the shared
  mounted store and output contract.
- Retry/rerun HTTP streaming can duplicate subtle `RemoteRunClient` protocol
  code. Shared private transport helpers are appropriate, but the public clients
  remain capability-specific.

## Open Questions

None.
