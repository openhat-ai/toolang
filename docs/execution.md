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

`execution.db` is the durable store for runtime truth.

It stores:

- runs
- steps
- agent-local updates
- deduplicated instruction bodies


## Durable Records

### RunRecord

`RunRecord` stores run-level truth:

- `run_id`
- `thread_id`
- `origin`
- `input`
- `status`
- `error`
- `created_at`
- `started_at`
- `finished_at`

`input` is the canonical initial `Message` for the run.

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

- `RunInputRef`
- `StepOutputRef`
- inline `Message`

This allows one step to depend on run input, prior step output, and newly added
input in one ordered list.

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
- `instructions_hash`

Instruction bodies are stored separately in `instruction_blobs` and referenced
by hash.


## Trace Events

Trace events are the internal execution fact stream.

Current trace-event types are:

- `RunStart`
- `StepStart`
- `PartStart`
- `PartDelta`
- `PartEnd`
- `StepEnd`
- `RunEnd`

Trace events drive both:

- durable persistence
- caller-facing response projection

Trace events do not duplicate the initial user input as a synthetic step.


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
