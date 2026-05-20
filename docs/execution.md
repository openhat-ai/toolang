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

`runs.db` is the durable store for runtime truth.

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

Toolang-owned run ids use `run_<id>`, where `<id>` is encoded with the `run`
id family. Thread ids use `<kind>_<id>`, such as `task_3nprht`,
`chore_xy1234`, or `web_1776857671893`.

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

Run input is stored as an ordered `inputs` stream. Each input has:

- `index`
- `action`: `start`, `steer`, or `stop`
- optional `mode`: `immediate`, `next_step`, or `next_call`
- optional `request_id`
- optional `message`

`index = 0` is the `start` input that created the run. Later `steer`
inputs belong to the same running run and can be referenced by later steps.

Step input is an ordered mix of:

- input refs: `{ "kind": "input", "index": 0 }`
- step refs: `{ "kind": "step", "index": 1, "part": 0 }`
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
Resource-scoped event streams also publish `run_input` events for client-side
run inputs. A `run_input` event is emitted before any `step_start` that
references the same input.


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
