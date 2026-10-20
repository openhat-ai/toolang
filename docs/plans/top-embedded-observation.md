# Embedded script observation in Top

Status: approved by the human and implemented on October 20, 2026.

## Goal and evidence

Show accurate active counts and live rows for host scripts executed in-process
by `too FILE.too`. Preserve the [Top contract](../design/top.md) for failed
observations, completed roots, usage, and instance departure.

On October 20, 2026, `too top --json` reproduced two running instances: resident
`gus` supplied a fresh empty live snapshot; roaming `aid` supplied readable
usage and completions but `live=null`, `live_stale=true`, and no observation
error. Consequently, the aggregate was `active=null` and its active table was
empty. A deterministic local fixture reproduces the same result.

PR #872 proves zero only when an endpoint-free instance has no unfinished
physical roots. Its tests deliberately retain unknown coverage for unfinished
work. Supporting that work requires a live observation transport; this is a
feature definition, not a regression in the idle proof.

Success means a healthy embedded script contributes its actual root count and
the same LiveTree rows as a server-backed run, including with `--quiet` or
`--out`. An idle embedded executor contributes zero. Loading or an actual
observation failure can still leave the count unknown.

## Scope and decisions

- Keep host script execution in its current process, with its current options,
  workspace, cancellation, result handling, and empty driver set. Resident and
  guest execution retain their existing server paths.
- Start a process-owned, read-only HTTP listener around the existing executor.
  Bind `127.0.0.1` on an OS-assigned port; publish only after readiness. Serve
  `GET /api/v0/top/snapshot` with the existing query validation and
  `ActivitySnapshot` response. Disable API docs and expose no execution,
  management, or driver routes.
- Reuse FastAPI/Uvicorn and the Top snapshot handler through an API factory.
  Narrow the snapshot dependency to the existing executor and explicit store
  path; do not construct a second AgentCore, RunExecutor, or store writer.
  Pin history and capture LiveTree under the executor publication gate, then
  scan completions outside it, exactly as the current snapshot path does.
- Preserve registration before preparation. Pass the registration explicitly
  into the embedded execution call. Publish its observation endpoint after
  executor startup and before admitting the script root. Update the existing
  registration only when its launch token still matches.
- Add optional `control.observation_endpoint` and `control.observation_error`
  fields to that runtime entry. Leave its management endpoint and sandbox
  reference unchanged. Discovery uses the validated observation endpoint only
  for a running embedded host instance without a server endpoint; it must have
  scheme `http`, host `127.0.0.1`, a valid port, and no credentials, path, query,
  or fragment. Missing fields preserve compatibility with older processes.
- Run the listener as an owned asynchronous task alongside execution. It must
  not install process signal handlers, print access/startup output, or extend
  the script lifetime. On normal completion, retain observation until executor
  shutdown drains owned work, then stop the listener before closing storage.
  Every exceptional exit and cancellation closes the task and socket.
- Listener startup or serving failure must not fail or cancel the script.
  Publish an observation error, clear the endpoint with launch-token fencing,
  and retain durable reads. Map that error to the existing `live_error` notice
  and JSON diagnostic. Do not use the idle proof to hide an observation failure.
- Keep the existing observer refresh, epoch rejection, stale-row retention,
  paging, and departure rules. A published listener failure cannot become zero.
  Older endpoint-free instances retain the existing idle proof and otherwise
  unknown live coverage. No database scan may label unfinished records live.

An in-process listener preserves the exact executor's LiveTree and uses Top's
existing transport. Moving scripts into a full AgentServer would change their
execution lifecycle; reconstructing activity from SQLite would misidentify
abandoned work and miss resident invocation facts. Neither is in scope.

## Design touchpoints and likely files

- `src/toolang/up/activity.py`: accept explicit executor/store dependencies.
- `src/toolang/api/routers/top.py` and `src/toolang/api/app.py`: reuse Top route
  validation and provide the restricted observation-app factory.
- `src/toolang/up/observation.py` and a focused listener module under
  `src/toolang/up/`: own launch-fenced metadata, endpoint validation, listener
  readiness, failure, and shutdown. Core execution remains transport-free.
- `src/toolang/cli/toolang/commands/script.py`: wire the listener into the
  existing embedded registration and executor lifetime.
- `src/toolang/up/top.py`: consume published observation failures independently
  from durable completion/usage reads.
- `tests/unit/api/test_top_api.py`, `tests/unit/up/test_top_observer.py`, focused
  listener/registration tests, and `tests/unit/cli/test_script_command.py`.
- Update `docs/design/top.md`; generate the behavior changelog through
  `too aid.too update_changelog` and obtain maintainer verification before merge.

## Acceptance tests

All tests run offline, synchronize on readiness/events, and use ephemeral ports.

1. Block a real embedded executor on a fixture tool. Top reports one active
   physical root with its thread ID, run ID, current call, and settled usage;
   combine it with an idle resident and verify the aggregate stays numeric.
2. Exercise pending admission, parallel branches, and a child run. Count only
   physical roots and preserve the existing tree structure and root totals.
3. Release the fixture root. The next snapshot has zero active roots and one
   completed root without duplication or a missing transition.
4. Run with `--quiet` and `--out`. Observation remains available, output content
   and exit behavior match existing expectations, and listener logs stay absent.
5. Fail preparation, validation, execution, cancellation, listener startup, and
   listener serving separately. Execution outcomes remain correct; all owned
   listener tasks/sockets close, with observation failures shown as unknown.
6. Replace the registration during a running request. Old publication/cleanup
   cannot modify the new launch; obsolete snapshots cannot restore live rows.
   Departure retains eligible history without polling the old listener.
7. Verify the restricted app rejects mutations and non-Top routes. Reject
   malformed or non-loopback observation endpoints without requesting them.
8. Preserve old endpoint-free idle/unfinished behavior and existing server-backed
   Top tests. Run the repository's default verification before each code commit.

## Risks and open questions

The listener adds one local port and asynchronous task per embedded script;
startup and cleanup tests must guard against leaks and shutdown hangs. It also
exposes the existing bounded Top snapshot to other local processes, consistent
with current local AgentServer access. Launch-token fencing and strict endpoint
validation prevent discovery from adopting stale or arbitrary remote targets.

Open questions: none for implementation.
