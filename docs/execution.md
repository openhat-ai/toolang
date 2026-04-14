# Toolang Execution Model

This document defines the current runtime model.

Layout lives in [layout.md](./layout.md).
Caps live in [caps.md](./caps.md).
API surfaces live in [api.md](./api.md).


## 1. State Forms

Toolang uses three forms of state:

- `durable`
  - persisted source of truth
- `prepared`
  - immutable compiled snapshot
- `live`
  - in-memory state used by the next run

Rules:

- authored definitions are durable
- operational facts are also durable, but separate from authored definitions
- `prepared` is generated from authored definitions only
- `live` is loaded from one `prepared` snapshot and patched by operational facts
- one run binds one snapshot and must not switch snapshots mid-run


## 2. Durable State

Durable state has two parts:

- `definitions`
  - `.too`
  - local caps
  - task, chore, and will documents
- `operational facts`
  - run truth
  - task status
  - scheduler cursors
  - prepare status

Rules:

- definition changes may require a new `prepared` snapshot
- operational changes must not trigger `prepare` by default
- `execution.db` is the durable store for runtime truth and agent-local operational facts


## 3. Execution Concepts

Toolang execution should use these core concepts:

- `run request`
  - one queued request admitted by one loop or API surface
  - it describes what should run, not how the caller will receive the result
- `run binding`
  - one admitted run fixed to one snapshot, thread, and origin
  - queue-owned attachments such as response sinks and completion futures stay
    outside the bound request
- `run input`
  - one read-only assembled semantic input for one run
- `run context`
  - one mutable execution context used by one run strategy
- `instructions`
  - one provider-neutral high-priority instruction block assembled by Toolang
- `messages`
  - the ordered conversation message list carried into one model turn
- `model binding`
  - one resolved model target plus one loaded model plugin
- `model turn`
  - one request-response exchange against one resolved model target
- `model plugin`
  - one plugin that performs one model turn only
- `run strategy`
  - one agent-loop implementation such as `basic` or `react`
  - it decides how to use model turns and tools until the run ends
- `run outcome`
  - one in-memory runtime outcome returned by the runner
  - it is not a durable record and not one API response model
- `step`
  - one execution-unit trace inside one run
  - current kinds include `model_call`, `tool_call`, and `runtime`
- `durable record`
  - one persisted execution truth row
  - current record families are `RunRecord`, `StepRecord`, and `UpdateRecord`
- `trace event`
  - one internal execution fact emitted by the runtime
  - persist and response sinks consume trace events
- `response`
  - one optional response path back to the original caller
  - it may be buffered, streamed over SSE, or delivered through one channel
- `response sink`
  - one caller-facing projection of run output
  - examples include buffered web replies, SSE streams, and channel replies
- `response event`
  - one caller-facing transport projection derived from trace events
  - current streaming chat responses follow one AI SDK data stream protocol subset

Rules:

- `run input` is a Toolang core concept, not an OpenAI-specific payload
- `instructions` is the core concept; one transport may encode it as a
  developer-role message, system message, or similar provider-specific field
- strategy-facing contracts such as `StrategyPlugin`, `RunContext`,
  `ModelBinding`, `RunResult`, and the canonical message/part model live in
  the formal shared boundary currently rooted at `experiments.base`
  so plugin authors do not depend on `execution` internals
- durable execution records, runtime stream events, and API/detail projections
  live in `execution`
- `RunRecord` owns one run-level canonical input message
- `StepRecord` owns only real execution steps
- initial user input must not be duplicated as synthetic `step 0`
- `StepRecord.input` may mix `RunInputRef`, `StepOutputRef`, and inline
  `Message` items so user steer and prior step outputs can coexist
- `model_call` step payloads include one lightweight `instructions_hash`
  reference rather than one duplicated prompt body
- `execution.db` stores deduplicated instruction bodies in `instruction_blobs`
  and `model_call` payloads point at them by hash
- `assemble` produces `run input`
- runtime resolves `run input.model` into one `model binding`
- runtime builds one `run context` from `run input` plus `model binding`
- `run strategy` consumes `run context`
- model plugins do not own tool loops, retries, or run termination policy
- `step` records execution truth, not transport truth
- response transport details such as outbound channel delivery are not `step`
  kinds and should stay outside the execution trace
- execution publishes one trace-event stream, but it should not know how one
  specific transport performs delivery
- buffered callers typically receive the final assistant message for one run
- streaming callers receive one response-event stream derived from trace events
- full response messages should use the same part model as durable step output
- tool interactions in full message payloads should project as `tool_call` and
  `tool_result` parts rather than one merged `tool` part
- future run-input history should be rebuilt from durable run input plus the
  durable step trace
- replay history must not be inferred from UI-only payload projections
- current trace-event families are:
  - `run`
  - `step`
  - `part`
- current trace-event types are:
  - `RunStart`
  - `StepStart`
  - `PartStart`
  - `PartDelta`
  - `PartEnd`
  - `StepEnd`
  - `RunEnd`


## 4. Agent-Local Updates

`execution.db` should store one append-only stream of agent-local updates.

These updates are already scoped to one agent, so their names should not repeat
that scope with prefixes such as `agent_` or `runtime_`.

Typical update kinds include:

- `created`
- `started`
- `stopped`
- `removed`
- `program_changed`
- `config_changed`
- `psyche_changed`
- `prompt_changed`
- `service_changed`
- `skill_changed`
- `task_changed`
- `chore_changed`

Rules:

- agent-local update names describe durable state changes, not runtime internals
- prepare and live refresh mechanics are implementation details, not update kinds
- do not persist implementation-detail events such as `live_reloaded`
- `stopped` covers both graceful stop and failed stop; use payload fields to
  distinguish outcome when needed
- `*_changed` absorbs add, edit, archive, remove, pause, status, and schedule
  changes for that durable primitive
- shared bus events may use different names because they are cross-agent
  projections, not agent-local execution truth


## 5. Runtime Process

One runtime process owns:

- `live caps`
- `live jobs`
- run queue
- active runs

Built-in loops:

- `chat`
- `pulse`
- `poll`
- `hook`
- `control`
- `inspect`
- `watcher`

Rules:

- `chat`, `pulse`, `poll`, and `hook` produce run requests
- `control` writes durable state
- `inspect` returns merged durable, prepared, and live views
- `watcher` observes definition changes, prepares a new snapshot, and swaps `live`
- loops do not execute model work directly; the runner does


## 6. Term Rules

Toolang uses these terms:

- `job`
  - the umbrella term for task, chore, and will definitions
- `task`
  - one collaboration-oriented job definition
- `chore`
  - one recurring job definition
- `will`
  - one long-horizon recurring job definition
- `run`
  - one execution attempt

Rules:

- use `job` for the shared definition layer
- use `run` for runtime execution
- do not use `running task` or `running job` for an executing run
- one runtime process owns active runs, not active tasks


## 7. Prepare And Live Refresh

Rules:

- `prepare` builds immutable snapshots such as `prepared.caps` and `prepared.jobs`
- dirty checks use definition fingerprints, not operational facts
- unchanged definitions reuse the existing prepared snapshot
- successful prepare updates `prepared` first, then swaps `live` atomically
- a Web UI write may appear in `inspect` before it becomes `live`

Useful inspect states:

- `prepare_pending`
- `prepare_complete`
- `prepare_error`
- `live`


## 8. Jobs And Task State

Tasks use two separate axes:

- operational status
  - `todo`
  - `doing`
  - `done`
  - `cancelled`
- placement
  - `active`
  - `archived`

Rules:

- task status is an operational fact
- active task definitions enter `prepared.jobs`
- archived tasks do not enter `prepared.jobs`
- `todo`, `doing`, `done`, and `cancelled` must not trigger `prepare`
- archiving removes a task from the active prepare set
- pulse admission uses active definitions overlaid with operational facts


## 9. Run Pipeline

Each admitted run uses five stages:

1. `bind`
2. `assemble`
3. `execute`
4. `persist`
5. `respond`

`bind` fixes the run-local inputs:

- `run_id`
- `thread_id`
- `origin`
- `thunk`
- `snapshot_id`

`assemble` is a separate runner stage.

It reads:

- the bound prepared snapshot
- operational facts
- message history
- runtime metadata

It returns one assembled run input:

- model
- instructions
- messages
- snapshot
- tools
- debug payload

Rules:

- run-input assembly is read-only
- run-input assembly must not fetch, sync, prepare, or mutate durable state
- run-input assembly must not bind a model plugin or choose a continuation policy
- all runs persist execution truth
- only runs with one caller-facing response path need one response sink
- background runs such as task or chore execution should use `response = None`
- run-input assembly must not encode provider-specific transport roles as core
  concepts
- runtime resolves the assembled model selector against installed model plugins
- runtime builds one `run context` that owns model calls, tool calls, and
  trace events
- one run strategy drives the high-level loop over that `run context`
- `persist` writes durable records and operational updates
- `respond` projects the same run into one optional caller-facing response sink
- response sinks consume emitted trace events and may be buffered or streaming
