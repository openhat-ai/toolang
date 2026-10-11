# Serve execution drivers

Status: proposed; implementation requires human approval.

## Goal and boundaries

Let `serve` independently enable interactive chat, task/chore scheduling, and
incoming-message handling. Call these **execution drivers**: each accepts work
from a different source and submits it to the shared executor.

| Driver | Work source | Owner |
| --- | --- | --- |
| `chat` | Interactive CLI/Web/API requests | API admission and Chat client |
| `jobs` | Authored/program tasks and chores | `work/scheduler.py` |
| `messaging` | Messages delivered through Hub, including Talk | `work/messaging.py` |

The management API, core execution services, setup/state watchers, and runtime
health remain available with any combination, including all drivers disabled.
The jobs switch covers both tasks and chores. Hub lifecycle and the `talk`
client remain separate from agent driver selection.

Current `up/server.py` always starts FastAPI and the scheduler, and starts
`TeamingLoop` when `teaming.enabled` is true. `MessagingLoop.handle()` currently
creates a new `script_` thread for each batch. This definition changes those
admission/lifecycle choices, not the language or executor's scheduling rules.

## Startup contract

- Add prominent `--chat/--no-chat`, `--jobs/--no-jobs`, and
  `--messaging/--no-messaging` options to `serve`, with the same options on
  background `start`. Group them as execution drivers in help.
- Omitted options preserve current behavior: chat and jobs enabled; messaging
  enabled when teaming is enabled. Resolve options once at the CLI/hosting
  boundary into a frozen set of three booleans carried by `ServeSpec` and the
  hidden `_serve` entrypoint. Preserve choices through host and guest launches.
- Keep existing `teaming.enabled` as the communication capability prerequisite.
  Explicit `--messaging` with teaming disabled fails before launch with guidance
  to enable teaming. `--no-messaging` disables incoming-message handling only;
  enabled teaming can still provide presence, outbound tools, and event export
  for chat/jobs. An explicit teaming opt-out still makes no Hub connections.
- No new environment variables, persisted driver configuration, or live
  reconfiguration in this scope. Changing drivers requires restarting the agent;
  setup refresh must not overwrite launch choices. Reusing an active runtime
  with conflicting explicit choices fails and explains the required restart.
- Publish the resolved flags as `drivers` in runtime status and
  `/api/v1/profile` runtime metadata, and show them in `too AGENT info` and
  startup output. Preserve HTTP endpoints, port resolution, health checks, and
  sandbox readiness even when chat is disabled.

Proposed usage (`too` is the `toolang` alias):

```sh
too alice serve --chat --no-jobs --no-messaging
too alice serve --no-chat --jobs --messaging
too alice serve --no-chat --no-jobs --no-messaging
```

## Admission and lifecycle

- `serve` owns the resolved drivers and shared runtime. FastAPI lifespan starts
  and stops enabled owners explicitly; no dynamic driver registry or new plugin
  family is needed. Do not instantiate a scheduler or message consumer when its
  driver is disabled.
- Chat disabled rejects new interactive sessions and interactive submissions to
  existing threads before any thread/run/control is persisted. Keep health,
  inspection, subscriptions, capability/job authoring, history management, and
  cancellation available. Standalone script execution remains available.
- Chat and scripts share run endpoints, and Chat can resume non-chat threads.
  Therefore do not infer request source solely from thread prefix. Add
  `client: "chat" | "script"` to execution-admission request bodies (new run,
  retry, rerun, and steer). Default to `chat` on authored/interactive operations
  and `script` on existing non-interactive operations; official callers send
  their source explicitly. Reuse existing thread-creation client values to gate
  interactive creation. Keep this metadata at the HTTP boundary, out of core
  run semantics. It selects behavior; it is not an authentication boundary.
- Disabled admission returns HTTP 409 with
  `detail = {"code": "driver_disabled", "driver": NAME}`. Chat checks advertised
  availability before opening its interactive UI; server checks remain
  authoritative. No operation silently enables a driver or launches a second
  runtime. Read-only history access remains usable.
- Jobs disabled starts no scheduling thread, timer, watcher, or dispatch. Job
  inspection and file authoring remain available without scheduler refresh;
  manual chore runs and task reopen operations return the disabled error before
  mutation. Cancellation remains possible through recorded active run IDs.
  Re-enabling uses existing checkpoint and activation semantics; it adds no new
  missed-occurrence replay policy.
- Separate teaming transport lifecycle from the optional message consumer.
  One lease still owns registration, heartbeat, and event publication. Preserve
  Hub reconnect behavior, batch serialization, cursor advancement, and receipt
  handling from the [transport contract](agent-hub-transport.md).
- On shutdown, stop accepting work, pause jobs, stop message consumption and
  watchers, stop the executor, drain final events, then close transport and
  scheduler resources. Clean up partially started owners on startup failure.

## One messaging thread per agent

- Introduce `ThreadPrefix.MSG = "msg"`, with `origin="msg"`. Reserve it for the
  message consumer; it must not fall through to the current `chat` origin or
  terminal-channel classification. Preserve existing origin-based fork/rewind
  restrictions and ordinary script IDs.
- Each local agent execution store owns one `msg_<issued-id>` thread. All of
  that agent's conversations and batches append runs to it; other agent homes
  and execution stores remain independent. Create it lazily for the first
  executable batch, then reuse it across consumer/process restarts and Hub
  reconnects. Backend changes retain this agent-owned thread; conversation
  checkpoints remain scoped to backend identity.
- Use the durable thread record as the identity source. Serialize lookup/create
  with the existing execution-store thread lock, select the unique `msg` origin
  with the matching prefix, and create through `ThreadManager` if absent. Reuse
  a committed empty thread after interruption. Conflicting or duplicate records
  are errors, never an arbitrary selection. Do not add a second identity file.
- Proposed context behavior: retain normal thread history and compaction across
  batches, including batches from different conversations. Preserve the current
  per-conversation `previous` envelope for custom `agic:msg` compatibility and
  initial continuity after upgrade. Include backend identity in each new batch
  envelope so equal conversation IDs from different datasets remain distinct.
  Preserve the current per-batch initial workdir instead of inheriting an earlier
  message handler's workdir accidentally.
- Leave all historical `script_` threads, runs, message origins, and checkpoints
  unchanged. Start the new thread on the next eligible batch; do not migrate,
  merge, or replay old runs. Replies report the stable thread ID and distinct
  per-batch run IDs; spawned workers keep their own thread identities.

## Implementation touchpoints

| Area | Likely files |
| --- | --- |
| Launch options and resolved flags | `cli/toolang/commands/runtime.py`, `cli/toolang/main.py`, `up/{config,types,server,sandbox,process}.py` |
| Driver visibility and admission | `api/{app,schemas}.py`, `api/routers/{agent,threads,runs,jobs}.py`, `cli/toolang/commands/{agent,script,thread}.py`, `cli/toolang/commands/chat/{main,remote}.py`, `execution/client.py` |
| Optional scheduling and consumption | `work/{scheduler,messaging,teaming}.py` |
| Messaging identity and origin | `execution/{types,threads,schemas}.py` |
| Tests | CLI runtime/help/client tests, `tests/unit/up/`, `tests/unit/api/`, `tests/unit/messaging/test_loop.py`, execution thread lifecycle tests, existing sandbox/messaging integration tests |
| User documentation | `docs/messaging.md`, relevant CLI/runtime documentation, generated `CHANGELOG.md` Unreleased entry |

Paths above are relative to `src/toolang/` unless already under `tests/` or `docs/`.
Keep plugin factories and command registration patterns intact. Split delivery
into independently verifiable changes: serve selection/admission, optional
background drivers, and the durable messaging thread.

## Acceptance

1. All eight driver combinations retain a healthy management API, report exact
   flags, and instantiate only selected work producers. Verify foreground and
   background argument forwarding for host and guest launches offline.
2. With chat disabled, CLI/API attempts to create or resume an interactive
   session, run, retry, rerun, or steer fail without writes; scripts, management,
   cancellation, and enabled background drivers still work.
3. With jobs disabled, due tasks/chores never dispatch; authoring and inspection
   work; manual execution requests fail without mutations. Restarting with jobs
   enabled resumes existing checkpoint semantics.
4. With messaging disabled, no batch is read or handled and no checkpoint/thread
   is created. With teaming enabled, chat/jobs retain outbound tools, presence,
   and event export; with teaming disabled, no Hub calls occur.
5. Two batches from different conversations share one `msg_` thread and distinct
   runs. A new consumer and reopened store reuse it; another agent does not.
   Interrupted initialization cannot create duplicate threads.
6. A later model request sees earlier recorded exchanges through ordinary thread
   history. Conversation checkpoints remain separate; source backend identity
   is present. Historical script records and spawned-worker origins are intact.
7. Disabled, partially started, failed, and normally stopped owners clean up
   without leaked tasks or scheduler threads. Existing reconnect, uncertain-send,
   cancellation-receipt, and no-self-reply tests remain green.
8. Implementations pass the repository's Ruff, formatting, type, and full offline
   pytest checks; this definition requires link/example review and diff checks.

## Risks and approval

Persistent messaging history shares context across an agent's conversations and
backends and grows over time; retain existing compaction behavior. Keeping the
old batch envelope duplicates some context but avoids changing custom handler
inputs in this delivery. Driver flags are operational controls, not security
permissions. Existing external script callers of authored/interactive API
operations need `client="script"` when chat admission is disabled.

Confirmed: management HTTP stays enabled when chat is off; jobs includes task
and chore. Pending human approval: driver names/flags, incoming-only messaging
control, and the proposed shared messaging context. No implementation is
authorized by this document alone.
