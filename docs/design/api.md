# Agent HTTP API

This guide describes the running agent's HTTP boundary. It is for client and
server implementers. [CLI](cli.md) owns process commands, [Drivers](drivers.md)
owns admission by driver, and [Events](events.md) owns live observation.
Agent routes use `AGENT_API_VERSION = 0`; statistics have an independent
[API version](execution-statistics.md). Inspect the server's `/openapi.json`
for the complete current request and response schemas.

## Application ownership

Each running agent exposes one local FastAPI server.

The process keeps one `AgentCore`, `CapsManager`, and `ChoresManager` for the
application lifetime. `AgentCore` owns the process-local executor, history,
thread manager, setup watcher, and state watcher. These owners are stored on
`app.state` and exposed through small typed request dependencies. The API also
uses the execution-owned `Subscriptions` service over the executor's canonical
stream and records.
Application-wide FastAPI dependencies are reserved for side-effect-only
concerns such as authentication or common validation. FastAPI lifespan owns
required startup and shutdown; module globals and `ContextVar` do not carry
application state.

`RunExecutor.run()` returns a `LocalRunHandle`; the application retains handles
only when its own protocol needs additional lifecycle bookkeeping.

Core endpoints are grouped as:

- `agent`
- `caps`
- `chores`
- `runs`
- `threads`

Non-interactive clients use `/api/v0/runs`; interactive Chat clients use the
chat-gated `/api/v0/chat/runs` routes. Direct and authored requests are described
below. Both produce canonical execution events; CLI clients share these
protocols through RemoteRunClient.


## Agent Endpoints

- `GET /healthz`
- `GET /api/v0/stream`
- `GET /api/v0/profile`
- `GET /api/v0/models`
- `GET /api/v0/tools`
- `GET /api/v0/workspaces`
- `GET /api/v0/agics`
- `GET /api/v0/flows`
- `PUT /api/v0/runtime/leases/{client_id}`
- `DELETE /api/v0/runtime/leases/{client_id}`
- `POST /api/v0/runtime/promote`

`GET /api/v0/workspaces` reads the server's current Setup and State. It returns
`revision`, ordered `items` (`name`, source `path`, and runtime `available`), and
`workdir`. A listing remains available when the runtime default cannot be resolved;
in that case `workdir` is `null`. Inspection does not create workspace directories.
Optional `workdir=NAME://path` validates that location independently of the runtime
default; invalid or unavailable requested locations return HTTP 400. In a guest sandbox,
availability requires a matching captured mount and an existing guest directory.

`too info AGENT` uses this endpoint while the agent is running and preserves its
runtime workspace defaults. `too AGENT workspace list` reads authored workspace
configuration and checks local directory availability without querying the server
or preparing State. It excludes implicit `lab` and temporary grants. See
[Scripts](scripts.md) for configuration locations and CLI options.
Execution CLI commands parse local directory grants but leave named workspace URI
validation to the embedded executor or remote server; client configuration and
filesystem paths do not determine remote availability.

`/api/v0/profile` returns:

- profile metadata
- runtime identity:
  - the server process's Toolang source `version`
  - enabled `drivers`
  - `launch_id`, `lifetime` (`persistent`, `foreground`, or `automatic`), and
    `api_version: 0`
  - `sandbox.driver`
  - the complete `sandbox.selector`
  - `sandbox.instance`: Docker's 12-character lowercase hexadecimal guest
    identity, otherwise `null`; the full container ID stays in host lifecycle state
  - the host-plugin-supplied `sandbox.description` for non-Docker runtimes,
    otherwise `null`

- environment summary

The profile endpoint validates sandbox metadata: Docker requires its 12-character
instance and a null description; non-Docker runtimes require a valid description
and a null instance. Invalid/missing required runtime metadata returns HTTP 500.
Chat's reader tolerates an absent/null description in a supplied profile by using
the local host-plugin fallback; that tolerance does not change server validation.
Runtime profile readers ignore unknown additive fields. Lifecycle and pagination
payloads share the agent API version; incompatible versions are rejected.

Lease upsert/release and promotion accept `{"launch_id": "<generation>"}`.
Upsert and promotion return `launch_id`, `lifetime`, and `api_version`;
release returns `{"ok": true}` and ignores a different generation. Promotion
keeps the same endpoint/drivers, disables automatic shutdown, and rejects a
foreground server. A stale generation or closing server rejects upsert/promotion
with HTTP 409. Upgrade the CLI and agent servers together.

Mutating requests may carry `X-Toolang-Launch-ID` to reject a replaced execution
owner before writes. Resident/visiting CLI clients send it automatically. Lease
renewal is independent of SSE; readers do not retain automatic servers. See
[Hosting](hosting.md#server-lifetime-and-client-leases) for timing,
shutdown eligibility, and failure handling.

`GET /api/v0/models` returns the server's current effective
`AgentSetup.models_effective()` view. Runnable `models` directives are applied when a
run starts, not by this inspection endpoint. The response includes:

- `default`
- `items`
  - `ref`
  - `name`
  - `provider`
  - `parameters.reasoning.effort`
  - `parameters.reasoning.applicable`
  - `price.input`
  - `price.output`

`ref` is the exact catalog route identity, including nested model ids, and does
not include query predicates. Reasoning efforts are distinct recognized catalog
values in catalog order; an unsupported model returns an empty list.
`default` is always one of the returned item refs when `items` is non-empty: it
is the configured default when that ref is included, otherwise the first item.
It is `null` only for an empty result.
`parameters.reasoning.applicable` is true when the model advertises either
effort-level or token-budget control; a toggle by itself is not applicable.
`price.input` and `price.output` are base token prices converted to USD per one
million tokens and are `null` when the corresponding catalog price is absent.

`GET /api/v0/models` accepts repeatable `query` parameters evaluated by the
runtime model collection. `GET /api/v0/tools` uses the same convention and
returns effective tool `ref`, `toolset`, `plugin`, and `description`
fields. Omitting
`query` lists the complete effective collection. Repeated query values and
comma-separated top-level matches form a union. Models use first matching
branch order, then source order; tools retain source order. Overlaps are
deduplicated. Empty matches return an empty `items` list; the model response
also has a `null` default. Matching uses the public CLI JSON projection, while
these HTTP response fields remain unchanged.

`GET /api/v0/agics` and `GET /api/v0/flows` list the agent's runnable
definitions.


## Cap Endpoints

Summary:

- `GET /api/v0/caps`

Collections:

- `GET /api/v0/psyches`
- `GET /api/v0/skills`
- `GET /api/v0/services`
- `GET /api/v0/prompts`

The cap summary and collection endpoints accept repeatable `query` parameters.
They match the public cap records documented in [Resource Queries](queries.md),
using `kind/name` identities and `tags`, then retain source order. The summary
retains its grouped response and counts. Both response items and query records
use `cap` for `kind/name` and `source` for the resolved Source Ref. CLI records
also expose the human-facing content address as `location`.

Cap list items include `form` and the additive `summary` display field.
`summary` is at most 256 Unicode code points and selects the first nonblank
title metadata, description metadata, or body paragraph. It does not replace
`description` for collection query matching. Clients can render cap discovery
without issuing one detail request per item.

Detail:

- `GET /api/v0/psyches/{name}`
- `GET /api/v0/skills/{name}`
- `GET /api/v0/services/{name}`
- `GET /api/v0/prompts/{name}`

Templates (one bundled `default` per kind; list responses contain one entry):

- `GET /api/v0/psyches/templates`
- `GET /api/v0/skills/templates`
- `GET /api/v0/services/templates`
- `GET /api/v0/prompts/templates`
- `GET /api/v0/psyches/templates/default`
- `GET /api/v0/skills/templates/default`
- `GET /api/v0/services/templates/default`
- `GET /api/v0/prompts/templates/default`

Write:

- `PUT /api/v0/psyches/{name}/authored`
- `PUT /api/v0/skills/{name}/authored`
- `PUT /api/v0/services/{name}/authored`
- `PUT /api/v0/prompts/{name}/authored`
- `DELETE /api/v0/psyches/{name}/authored`
- `DELETE /api/v0/skills/{name}/authored`
- `DELETE /api/v0/services/{name}/authored`
- `DELETE /api/v0/prompts/{name}/authored`
- `PUT /api/v0/psyches/{name}/configured`
- `PUT /api/v0/skills/{name}/configured`
- `PUT /api/v0/services/{name}/configured`
- `PUT /api/v0/prompts/{name}/configured`
- `DELETE /api/v0/psyches/{name}/configured`
- `DELETE /api/v0/skills/{name}/configured`
- `DELETE /api/v0/services/{name}/configured`
- `DELETE /api/v0/prompts/{name}/configured`

Authored write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `content`: raw cap content

Configured write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `ref`: supplied Source Ref (kind is determined by the endpoint)

Capability mutations write through the authored/configured catalogs, then await
State publication. PUT responses read the exact published root/home layer,
including a cap shadowed by a higher scope or excluded by runtime allow policy.
DELETE responses wait for publication too. If the source change is saved but
State rejects the candidate, HTTP 409 reports that distinction; the last valid
State remains active. Publication does not rebind an existing run.

Delete routes accept `scope=home|root` as a query parameter. Cap read
items include:

- `name`
- `description`
- `scope`
- `origin`
- `form`
- `cap`: effective `kind/name` Cap Ref
- `source`: resolved Source Ref
- `definition_file`
- `line` when known
- `editable`

Read and write payloads use the same `root`, `home`, and `here` scope
vocabulary. Read payloads retain `form`, `scope`, and `origin`; CLI query
records expose all three through `tags`. `location` addresses actual content
as an absolute path or remote URL. Only inline caps append a declaration line
as `file:line`; CLI records have no separate `definition` or `line` field.


## Chat Client Orchestration

The HTTP API has no endpoint that accepts terminal `ChatInput` text. A chat
client creates a thread when needed, converts the interaction to the shared
`RunRequest` boundary, and starts each turn through the authored run stream:

1. `POST /api/v0/threads` with `client_id` and `request_id`.
2. `POST /api/v0/chat/runs/authored/stream` with the returned thread ID, client ID, request ID,
   authored input, materialized model/policy, `driver: "chat"`, and a nullable
   `runnable.ref`. A null ref leaves handler selection to execution admission;
   a session choice or input override supplies an explicit ref.

An existing chat thread can be passed directly to the authored endpoint; the
client does not create another thread for every turn. The server resolves the
request against its current setup and Agent State, including fallback
selection, materialized policy, prompts, named input, and client-supplied file
Parts. CLI clients discover includes with `/api/v0/runs/input-references`, read
those files against procdir, then submit `attachments` and `source_revision`.
The authored HTTP route treats an omitted attachment map as empty; a missing
referenced key returns 422 rather than reading the server filesystem. See
[client file inputs](#client-file-inputs).
This keeps TUI and WebUI clients on the same run protocol
without adding a chat-specific server vocabulary.

The separate non-interactive `POST /api/v0/runs/stream` endpoint continues to
accept a selected runnable and canonical input Parts such as:

- `text`
- `image`
- `audio`
- `document`

Actual part support still depends on the selected model route. The built-in
OpenAI Chat Completions and Responses adapters map text, image, audio, and
document inputs. Chat Completions rejects a `DocumentPart` that has only a
document URL; the caller must first provide document data or a provider file
id.

For multipart payload details:

- an image part's `image_url` may be a remote URL or a local `data:` URL
- an audio part's `data` should be base64 payload; `data_url` is also accepted
  as an alias and is normalized to base64
- a document part's `url` is for remote documents
- a document part's `data` carries inline provider-facing document data and may
  be a full `data:...;base64,...` URL
- `file_id` references a document already uploaded to the selected provider

Both run-streaming endpoints return the execution SSE protocol described below. A WebUI
that needs another presentation shape adapts these events client-side; the API
does not maintain a second chat event vocabulary.

## Chore Endpoints

| Request | Behavior |
| --- | --- |
| `GET /api/v0/chores` | List effective definitions and execution state |
| `GET /api/v0/chores/{name-or-id}` | Read effective definition and state |
| `POST /api/v0/chores` | Create a home definition with required `name` |
| `PATCH /api/v0/chores/{name-or-id}` | Update home `title`, `body`, or `schedule` |
| `DELETE /api/v0/chores/{name-or-id}` | Remove home definition, retain checkpoint/history |
| `POST /api/v0/chores/{name-or-id}/run` | Request a serialized manual occurrence |
| `POST /api/v0/chores/{name-or-id}/cancel` | Cancel active or pending work |

Creation accepts optional `title`, `body`, and `schedule`. Missing schedule means
one-off. Use a new name for a new identity. Inline definitions appear in
inspection but must be edited in their `.too` source. Root chore files do not participate.

Responses include `name`, `ref`, derived `id`, title, source `path` and optional
`line`, schedule, and `runtime`. Runtime contains `thread_id`, `status`,
`active_run_id`, `pending_at`, `next_scheduled_at`, `last_run`, `scheduler_error`,
and checkpoint `updated_at`. `synced` indicates whether the checkpoint describes
the current body and schedule; false means its status still belongs to an older
or unobserved definition. Definitions have no execution timestamps or guessed
remote issue fields. Status is null before the driver creates a checkpoint,
then pending/running/done/failed/canceled. A recurring chore remains pending
while future work exists; an already exhausted schedule starts as done.

CLI and API inspection read the same current source definitions without resolving
handlers. The driver uses accepted State for inline definitions and handlers.
PATCH/DELETE reject inline definitions with 409 and identify the source
to edit. Creating a home file hidden by an inline declaration also returns 409.

Authoring and inspection work with the chore driver disabled. Manual dispatch
returns 409 with `{ "code": "driver_disabled", "driver": "chore" }`.
Unknown definitions return 404. Refresh the collection after a mutation and
poll it for background progress. Execution history is available through the run
and thread APIs. Chore threads support fork/rewind under the same state checks
as other threads; these operations do not change chore checkpoints.


## Run And Thread Endpoints

Interactive clients submit, rerun, and steer under `/api/v0/chat/runs`;
these write routes require the chat driver. Noninteractive scripts use the
`/api/v0/runs` routes below and create `exec_` threads with derived kind `exec` at
`POST /api/v0/runs/threads`.
Interactive thread creation at `POST /api/v0/threads` creates `chat_` contexts.
Thread responses omit classification fields and `channel`; the ID prefix
identifies the kind.
Thread listing filters with `kind=chat|talk|chore|work|exec`; there is no Thread
`origin` or `channel` filter. Status `running` includes pending and running Runs;
`active_run.status` retains the actual Run state. CLI steer/cancel use the same
active-state definition as HTTP controls.
Run `updated_at` uses completion, start, or admission time, in that order.
Thread `updated_at` is the latest visible Run timestamp or Thread mutation time;
listing sorts by that value, including newly admitted pending work.
All callers use `client_id` and `request_id` rather than a client-type enum.
Management, cancellation, inspection, and script execution remain available
when chat is disabled. See [driver selection](drivers.md).

- `POST /api/v0/runs/stream`
- `POST /api/v0/runs/authored/stream`
- `GET /api/v0/runs/defaults`
- `POST /api/v0/runs/workdir/resolve`
- `GET /api/v0/runs`
- `GET /api/v0/runs/{run_id}`
- `GET /api/v0/runs/{run_id}/stream`
- `POST /api/v0/runs/{run_id}/rerun/stream`
- `POST /api/v0/runs/{run_id}/rerun`
- `POST /api/v0/runs/{run_id}/steer`
- `POST /api/v0/runs/{run_id}/cancel`
- `POST /api/v0/threads`
- `GET /api/v0/threads`
- `GET /api/v0/threads/{thread_id}`
- `GET /api/v0/threads/{thread_id}/result`
- `POST /api/v0/threads/{thread_id}/rewind`
- `POST /api/v0/threads/{thread_id}/fork`
- `GET /api/v0/threads/{thread_id}/stream`

`/api/v0/runs/{run_id}` is the main trace-detail endpoint.

Run collections return `RunInfo` arrays directly. `RunInfo` combines run
identity, status, input text, output summary, failure, and timestamps; there is
no separate `RunSummary` response type.

`RunDetail.output` is a resolved Output envelope (`type`, `value`, `binding`),
or `null` when no output edge exists. The complete value may itself be empty
or JSON null; it is not implicitly converted to a Parts array.

`steer` and `cancel` operate on active (`pending` or `running`) runs. Steer
accepts a user message whose parts may be empty. Both accept optional
`expected_control` (`<run_id>@<control_index>`). The server checks live ownership
and this invocation before writing; mismatch returns HTTP 409. Resident/visiting
CLI controls always pin the invocation, preventing a delayed command from
reaching another invocation. Existing callers may omit the field.
`rerun` accepts a terminal root run and starts a new root from its source
invocation, replacing the source in the visible thread projection. The
non-streaming endpoint accepts optional `request_id`, `client_id`, partial
`limits`, and an exact `model` replacement; it executes on the owner's event loop.

The `/rerun/stream` variant accepts an authored restart request: required
`request_id`, optional `client_id`, and a `commands` array of policy overrides
(default `[]`). It accepts either `model` or a sparse `model_override` with
`identity`, `effort`, and `max_output`. These fields are mutually exclusive;
a sparse override applies to the source's complete model request. This variant
returns HTTP 200 SSE, rather than a control receipt.

Thread `rewind` and `fork` request bodies take an optional `run_id` anchor and
`request_id` and `client_id`. An omitted run ID selects the last visible run.
Both operations accept every Thread kind and require a visible terminal root
anchor; rewind also requires an idle Thread.

Non-streaming `steer`, `cancel`, and `rerun` return
`{run: RunDetail, command: ControlInfo}`. Steer/rerun use HTTP 202; cancel
uses HTTP 200. The receipt confirms acceptance; inspect `command.status` for
application. Independent root start/rerun requires an idle target thread;
a pending/running root causes HTTP 409 without admitting replacement work.

An accepted manual chore request returns 202 without a run result; inspect the
chore for progress. Thread create and fork return the created thread; rewind
returns the updated existing thread representation. None of these thread
operations starts a follow-up run.

`POST /api/v0/runs/stream` accepts:

- `thread_id`: required existing thread id
- `request_id`: required globally unique caller-supplied control identifier
- `runnable.ref`: required concrete agic or flow ref
- `runnable.input`: flat input-value object; `_` holds primary input and other
  keys hold arguments, for example `{"_": "Summarize", "count": 2}`
- `model`: a concrete `ModelRequest`, or `null` for a model-free runnable
- `policy`: materialized `allow` ceilings and complete `limits`

HTTP limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`,
and `time`. The caller materializes omitted session values before submission;
an explicit JSON `null` disables that field for the run.

`POST /api/v0/runs/authored/stream` accepts the authored `RunRequest` wire
shape:

Authored requests may supply `driver` (`chat`, `talk`, or `chore`) with
`runnable.ref: null` for automatic handler selection. Explicit refs keep their
normal validation and take precedence. A displayed default does not pin an
automatic request to that handler; input source-revision checks still apply.

The following request assumes an existing thread and an `agic:chat` declaration
with a named `audience` parameter, plus an available route accepting high effort:

```json
{
  "thread_id": "chat_example",
  "request_id": "example_request",
  "runnable": {
    "ref": "agic:chat",
    "input": {
      "_": "Summarize these notes.",
      "audience": "maintainers"
    }
  },
  "model": {
    "ref": "openai/gpt-5",
    "reasoning": {"effort": "high"}
  },
  "policy": {
    "allow": [],
    "limits": {
      "agic_model_calls": 200,
      "agic_tool_calls": null,
      "tokens": 4000,
      "cost": null,
      "time": null
    }
  }
}
```

Both endpoints use the same object shape. Authored values must be strings
evaluated as Content; direct values are decoded against the runnable signature
without Content evaluation. A direct parts array belongs under `_`, for example
`{"_": [{"type": "text", "text": "Summarize"}]}`. Omitted `input` means `{}`;
`input: null` and primary null are invalid. Missing `_` and explicit empty input
remain distinct. Duplicate input keys are rejected before accepting a run.

This is a breaking format change: sibling `args`, source `named` lists, and
primary parts arrays as the complete container are no longer accepted. A valid
declared argument may still be named `primary`, `named`, or `args`. Input-bearing
control responses likewise use flat maps with self-describing value encodings.
Run/Step outputs use `{"type": "Text", "value": "result", "binding": "_"}`.
The binding is a name or null. The type describes the complete value; there is
no Local wrapper or dimension flag.
See [Records](records.md) for output reference paths.

The server reads setup and state once and validates the concrete runnable,
model parameters, policy, input, prompts, named sources, and supplied file Parts
before accepting the run. Reasoning effort is validated against the selected
catalog route and replaces that target's complete reasoning choice. Named input
names must be unique. Unknown fields and invalid combinations return `422`; a
missing thread returns `404`.

`GET /api/v0/runs/defaults` returns one concrete `model` request, including its
typed `parameters`, plus `runnable`, materialized `policy`, and the effective
canonical `workdir` for clients to adopt as session-owned defaults. The optional
`thread_id` selects the workdir inherited from that thread. The resolver endpoint
`POST /api/v0/runs/workdir/resolve` accepts optional `thread_id`, `workdir`, and
`workdir_base` fields and returns one canonical `workdir`. It applies the same
workspace authorization and path rules as run submission, but does not accept a
run or mutate client session settings. Run submissions still perform full
validation of the complete request.

`GET /api/v0/threads/{thread_id}/result` returns the newest succeeded root
`RunDetail` with a nonempty resolved output. An unknown thread and a known
thread without a result return distinct `404` details.

### Client file inputs

`POST /api/v0/runs/input-references` accepts `runnable`, authored flat `input`,
and optional `driver`. It expands prompts and returns `{state, references}`
without opening files. Clients read those references against their invocation
directory, then submit `attachments: {reference: Part}` and the returned State
revision as `source_revision` on the authored Run request.

The server requires every encountered include key in that map, including keys
introduced by prompt expansion. Missing keys or changed source revisions reject
admission with 422 and create no Run. With no includes the map may be empty;
HTTP omission also becomes an empty map. Client-read bytes do not grant workspace
access. Direct-value requests put already materialized Parts under runnable input
instead of evaluating `@file` text. [Scripts](scripts.md#file-inputs) owns path origins.

### Live execution snapshots

`GET /api/v0/runs/live` returns `LiveSnapshot(epoch, observed_at, trees)` from
resident executor memory. Optional `root_run_id` selects one root; unknown,
completed and child IDs return an empty `trees` collection. Invalid IDs return
`422`; a stopped executor returns `503`. Local and remote RunClients expose
`await live_trees(root_run_id=None)` after connection.

Each tree has `root_run_id`, `thread_id` and parent-first `nodes`. Run nodes hold
identity, current runnable/status/timestamps/occurrence and subtree
`calls`, `usage`, `cost` (USD), `cost_approximate`. Step nodes hold their actual
parent, kind/operation, compact name/plugin/target/summary, status/timestamps,
occurrence, asynchronous launch and direct invocation/usage/cost facts. They exclude call contexts,
arguments, output bodies, provider metadata and references. Completed leaves are
pruned; async launch ancestors remain until their child ends. No history or Store
read is performed. Treat each response as a replacement sample; epoch changes
identify a different executor stream lifetime. Polls may miss short-lived Steps;
use the existing event stream when every transition is needed.

## Execution streams

POST submission and GET observation share the canonical SSE protocol in
[Events](events.md). Observe an accepted Run through GET after a disconnect;
never repeat a submission to reconnect. Accepted responses expose
`X-Toolang-Run-ID`, including through CORS for allowed origins.
