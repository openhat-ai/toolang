# Execution Model

This document defines the runtime execution model.


## State Forms

Toolang uses three forms of state:

| State Form | Meaning |
| --- | --- |
| `durable` | Authored files and persisted execution truth |
| `prepared` | Immutable runtime-ready snapshots |
| `live` | In-memory state used by the active runtime |

One run is bound to one prepared snapshot for its full lifetime.


## Durable Store

`runs.db` is the durable store for thread and run truth.

It stores:

- threads
- runs
- steps
- agent-local updates
- deduplicated prompt bodies

`.runtime/jobs.db` stores the scheduler projection for ready task and chore
documents. It is used for atomic job claims and completion bookkeeping; it is
not the transcript or run-history store.

`.runtime/files.db` stores file request claims for inbox directories. It records
the watched root, relative path, absolute path, size, mtime, content
fingerprint, terminal status, run id, timestamps, and terminal error. It is used
for deduplicating file fingerprints and completion bookkeeping; it is not the
transcript or run-history store.


## Durable Records

### RunRecord

`RunRecord` stores run-level truth:

- `run_id`
- `thread_id`
- `origin`
- `status`
- `error`
- `created_at`
- `started_at`
- `finished_at`

Toolang-owned run ids use `run_<id>`, where `<id>` is encoded with the `run`
id family. Thread ids use `<kind>_<id>`, such as `tsk_3nprht9x`,
`chr_xy1234ab`, `file_def456gh`, or `web_def456gh`.

File request runs use origin `file` and explicitly invoke the thunk named
`file`. The start command stores the rendered file message, and run metadata
stores the `invoke_parts` file attachment data plus the file request id and
fingerprint.

### CommandRecord

`CommandRecord` stores one accepted client command for a run:

- `run_id`
- `index`
- `kind`: `start`, `steer`, or `stop`
- optional `mode`: `immediate`, `next_step`, or `next_call`
- optional `request_id`
- optional `message`
- `created_at`

`index = 0` is the `start` command that created the run. Later `steer`
commands belong to the same running run and can be referenced by later steps.
Cancel operations append a `stop` command and then finish the run as canceled.

### StepRecord

`StepRecord` stores one real execution step:

- `run_id`
- `step_index`
- `kind`
- `status`
- `input`
- `output`
- `payload`
- `error`
- `started_at`
- `finished_at`

Current step kinds are:

- `model_call`
- `tool_call`
- `runtime`

Current step statuses are:

- `finished`
- `failed`
- `canceled`

Step input is an ordered mix of:

- command refs: `{ "kind": "command", "index": 0 }`
- step refs: `{ "kind": "step", "index": 1, "part": 0 }`
- inline `Message`

This allows one step to depend on accepted run commands, prior step output, and
new inline messages in one ordered list.

### UpdateRecord

`UpdateRecord` stores agent-local operational updates such as:

- `started`
- `stopped`
- `program_changed`
- `task_changed`
- `chore_changed`


## Model-Call Payload

`model_call` steps use a dedicated payload with:

- `model_ref`
- `input_tokens`
- `output_tokens`
- `instruct`
- `context`

Instruction and context bodies are stored separately in `prompts` and referenced
by content hash.


## Trace Events

Trace events are the internal execution fact stream.

Current trace-event types are:

- `RunStarting`
- `RunSteering`
- `RunStopping`
- `RunBegin`
- `StepBegin`
- `PartBegin`
- `PartDelta`
- `PartEnd`
- `StepEnd`
- `RunEnd`

Trace events drive both:

- durable persistence
- caller-facing response projection

Trace events do not duplicate run commands as synthetic steps. A run emits
`RunStarting` when the runtime accepts the start command. It emits `RunBegin`
when execution actually begins. If a started run waits behind another run, the
resource-scoped event stream may publish `run_waiting` between those events.

The command trace events map to public event names as:

- `RunStarting` -> `run_starting`
- `RunSteering` -> `run_steering`
- `RunStopping` -> `run_stopping`

The execution trace events map to public event names as:

- `RunBegin` -> `run_begin`
- `StepBegin` -> `step_begin`
- `PartBegin` -> `part_begin`
- `PartDelta` -> `part_delta`
- `PartEnd` -> `part_end`
- `StepEnd` -> `step_end`
- `RunEnd` -> `run_end`

The runtime must publish trace events in causal order. A command event must be
published before any step that can reference that command.


## Canonical Content Model

Toolang uses one shared content model from `toolang.base`:

- `Message`
- `Part`
- `Delta`

Current part kinds are:

- `text`
- `tool_call`
- `tool_result`

Current delta kinds are:

- `text`
- `tool_call`


## Response Events

Caller-facing streaming responses are derived from trace events.

`POST /api/v1/chat/stream` uses an AI SDK UI message stream subset with:

- `start`
- `message-metadata`
- `text-start`
- `text-delta`
- `text-end`
- `tool-input-start`
- `tool-input-delta`
- `tool-input-available`
- `tool-output-available`
- `start-step`
- `finish-step`
- `finish`
- `error`
- `[DONE]`

This layer is transport output. It is not the durable execution truth.


## Live UI Projection

Real-time UIs consume the public event stream as mutable blocks. A mutable block
has four operations:

- `create`
- `update`
- `delta`
- `finalize`

Each active run owns a sequence of step blocks driven by SSE events:

- `run_starting` creates the start-command block
- `run_begin` updates that block with the runtime run id and finalizes it
- `step_begin` creates a step block
- `part_delta` updates the current step block
- `step_end` finalizes the current step block with the durable result
- `run_stopping` marks the active run as canceling
- `run_end` finalizes the run status

A UI keeps at most one top-level mutable block at a time. Finalized blocks leave
mutable state and may move to scrollback immediately. Parallel work is rendered
inside the current mutable block rather than by opening additional top-level
mutable blocks.


## Inspection Views

Run detail is exposed as:

- `info`
- `input`
- `output`

Thread detail is exposed as:

- `info`
- `runs`

The detail API projects messages from durable run input and durable step
output. It does not rely on a separate durable chat store.
