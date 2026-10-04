# Agent HTTP API

This guide owns server lifecycle, request boundaries, publication and streaming
contracts. Exact fields and endpoints come from [schemas](../src/toolang/api/schemas.py),
[routers](../src/toolang/api/routers/) and generated OpenAPI. Language meaning and
durable lifecycle belong to [program](program.md), [call input](call-input.md),
[execution](execution.md) and [records](run-step-records.md).

## Application ownership

Each server assembles one FastAPI app around process-owned `AgentCore`,
`CapsManager` and `JobsManager`. Core owns the executor, history, thread manager,
Setup and State watchers. Typed dependencies read `app.state`; lifespan owns
startup/shutdown and the scheduler. A process-local `LiveEventRelay` distributes
live events. No module global or request `ContextVar` substitutes for app ownership.
See [app assembly](../src/toolang/api/app.py) and [server](../src/toolang/up/server.py).

| Group | Purpose |
| --- | --- |
| `/healthz`, `/api/v1/profile` | Readiness and process/runtime identity |
| `/api/v1/models`, `/tools`, `/workspaces`, `/agics`, `/flows` | Current resource, workspace and runnable inspection, all under `/api/v1` |
| `/api/v1/caps`, `/psyches`, `/skills`, `/services`, `/prompts` | Capability inspection and authored/configured mutations |
| `/api/v1/jobs`, `/tasks`, `/chores` | Job projections and kind-specific mutations |
| `/api/v1/runs`, `/threads` | Acceptance, control, live streams and durable inspection |

Except `/healthz`, group-relative paths above use `/api/v1`. There is no Chat
resource, historical `/events` collection, or registered `/hook/*` route. Channel
plugins exist, but server assembly does not start channel polling.

## Profile and resource inspection

Profile separates server source version from sandbox selector and instance.
Optional sandbox description is presentation metadata; readers tolerate absent
or unknown additive fields. Breaking wire changes require a versioned contract.

Model/tool/cap collection queries use [native TQ semantics](queries.md).
Repeated query branches union and deduplicate. Models preserve first matching
branch order; other resources preserve source order. Model listing includes a
concrete default from the returned set, or null for an empty set, plus reasoning
capabilities and base USD-per-million token prices. It does not apply one
runnable's model directive. Missing prices remain null. API readiness is not a
network reachability or entitlement probe.

Workspace listing returns current revision, named source paths, availability and
canonical workdir. A missing runtime default yields null workdir rather than
preventing the listing. Optional requested workdir is independently validated;
invalid/unavailable locations return 400. Inspection creates no directories.
Guest availability requires a captured mount and an existing guest directory.

## Capability publication

Each concrete kind supports collection/detail/template reads and authored or
configured PUT/DELETE operations. Reads use published State. Lists include form,
scope, origin, source ref and a bounded summary; query identity remains `kind/name`
even when a response's `ref` is a source URI. [Caps](caps.md) owns those distinctions.

Authored PUT receives `scope` (`home` by default or `root`) and raw `content`;
configured PUT receives scope and an external `ref`. DELETE takes scope as a
query parameter. `here` caps are source-owned and not mutation targets.

Mutations write the owning catalog, then await State publication. PUT returns
the exact published root/home layer, including shadowed or allow-excluded caps.
If the source was saved but publication rejects the candidate, 409 distinguishes
that outcome; last valid State remains active. DELETE also waits for publication
and succeeds with 204. Publication never rebinds accepted code; model-call resources follow the
[State visibility contract](program.md#directives).

## Run requests

Create a thread explicitly with `POST /api/v1/threads` before its first Run.
Both start endpoints require `thread_id`, globally unique `request_id`, a concrete
`runnable.ref`, flat `runnable.input`, explicit `model` (nullable) and materialized
`policy` with allow ceilings and complete limits:

| Endpoint | Input boundary |
| --- | --- |
| `POST /api/v1/runs/stream` | Typed input values; strings are values, not Content source. |
| `POST /api/v1/runs/authored/stream` | `CallInput[str]` Content sources; additionally accepts workdir/base, attachments and source revision. |

The server validates against current Setup/State, resolves authored Content and
coercion, then accepts the Run. Clients resolve mutable session defaults and
runnable fallbacks beforehand. Unknown fields/combinations and invalid inputs
return 422; an unknown thread returns 404. Duplicate flat input keys are rejected.
There is no sibling `args` input envelope or implicit server-side model default
for these materialized requests. Agics require a concrete model.

`GET /api/v1/runs/defaults` returns concrete model/runnable/policy/workdir for
clients to adopt. Optional `thread_id` includes that thread's workdir.
`POST /api/v1/runs/workdir/resolve` validates a proposed workdir/base without
creating a Run or mutating client settings.
`POST /api/v1/runs/input-references` discovers client file references against a
State revision. Authored clients supply resolved attachment Parts keyed by the
references; attachments supply content without granting workspace access.

Media shape and provider support are different boundaries. Input supports text,
image, audio and document Parts; actual adapters enforce their supported forms.
For example, Chat Completions does not accept a URL-only document. Native strings
and structured values follow [typed input](call-input.md), not ad hoc JSON parsing.

## Controls and inspection

Run collections return `RunInfo` arrays; detail returns `RunDetail`. A detail's
output is resolved canonical Parts, null before an output edge exists, and may
be an empty array for an empty result. Record references and persistence types
remain owned by [records](run-step-records.md).

Steer/cancel require an active pending/running Run and return accepted control
projections. Steer defaults to `next_step`; cancel defaults to `immediate`.
They may also request other supported [control timings](execution.md#run-controls).
Acceptance is not evidence that a control has been applied.

Retry/rerun accept a terminal root and return 202. Retry retains identity/model
and accepts an optional Step anchor and partial limits. Rerun starts another root;
it does not replace the source's durable history membership. The non-streaming
rerun body accepts a concrete `model`; the authored streaming rerun additionally
supports sparse `model_override` and retained command inputs. Consult their
separate request schemas rather than assuming identical payloads. Retry rejects
unsafe history/sandbox/workspace reuse as described in [execution](execution.md#retry-and-rerun).

Thread creation accepts `web`, `term`, `tui`, `chat` or `script` placement.
Fork/rewind take optional root Run anchors; they do not start another Run.
Job-derived threads are not branchable. The thread result endpoint returns the
latest succeeded root with nonempty output; unknown thread and no result have
distinct 404 details. Thread detail is a projection over records, not a separate
message store.

## Live streaming

Authored start/retry/rerun streams expose `X-Toolang-Run-ID`; the typed start
stream identifies acceptance through its first root `run_begin`, without that
header. Start subscriptions are established before execution emits its first
event. CORS exposes the acceptance header to configured browser origins.

A start stream includes the recursive tree through root `run_end`. Observing an
already active Run receives only events emitted after subscription, so it need
not begin with `run_begin`. Disconnecting removes the subscriber without canceling
the Run. A child stream request returns 409 and identifies its root.

SSE `event` is the canonical event name and `data` is its payload, retaining the
`type` discriminator. Run events are `run_begin`, `step_begin`, `part_begin`,
`part_delta`, `part_end`, `step_end`, `run_end`; thread streams can also include
`thread_created`, `thread_forked`, `thread_rewound`. No second transport event
wrapper or synthetic control-acceptance event is added.

Part events retain the call-local ordinal. `part_begin.part_type` identifies the
Part kind without colliding with the event discriminator. Reasoning uses the
same events and completed `ReasoningPart`; provider signatures stay on completed
Parts. Deltas are live only. Human presentation omits reasoning/native metadata;
canonical records retain it according to the [adapter contract](plugins.md#model-adapter).

There are no SSE IDs or replay cursor; `Last-Event-ID` is ignored. Do not retry an
ambiguous start or reconnect expecting missed deltas. An inspection client can
subscribe and buffer new events, read durable detail as its baseline, then apply
buffered/new events idempotently. Chat's accepted-stream recovery is described
in [Chat](chat.md#recovery), and never synthesizes missed events.

## Jobs and consumer projections

Unified `/jobs` reads combine Markdown job kinds; `kind=task|chore` filters them.
These catalog routes do not enumerate or edit `agent.too` declarations; see
[caller projection](tasks.md#caller-projection). Mutations use `/tasks` or
`/chores`. Default lists/details select ready jobs; `/archived` selects
archived jobs. Draft files exist but are not exposed by a dedicated draft-list
route. Lists return arrays without body; detail and mutation return the resource
with body. [Tasks](tasks.md) owns authored stage/identity and [work](work.md) owns
scheduler status and recovery.

Task creation/patch accepts title/body; chore creation also accepts an optional
schedule (the [authored default](tasks.md#identity-and-fields) applies) and patch
can change it. Stage actions are `draft`, `ready`, `archive`.
Task `reopen` makes a terminal task pending. Chore `run` creates one manual
occurrence without changing schedule. Deletion is available only through archived
routes and returns 204. IDs are server-generated.

Runtime projections provide `thread_id`, `last_run`, `next_run_at` and scheduler
`error`; a last Run's error belongs to execution. Pending/running accepted Runs
and scheduler claims must not be confused with authored stage. A UI may derive
board phases from stage, scheduler status and last-run status, but `phase` is not
a stored field or API mutation. Prioritize archived/draft placement, then active
execution, then the kind's terminal/pending state; chore scheduling is a separate
fact. Refresh the collection after writes and fetch detail only when body is
needed. There is no job-event hub; consumers needing background refresh poll.

Catalog not-found/conflict errors map to 404/409; invalid authored data maps to
400, schema validation to 422, and invalid scheduler actions to 409. Link to
thread/run detail using returned IDs, without offering fork/rewind on job threads.

## Verification anchors

[API unit tests](../tests/unit/api/),
[streaming](../tests/integration/api/test_streaming.py),
[remote runs](../tests/integration/api/test_remote_runs.py) and
[remote Chat support](../tests/integration/api/test_remote_chat_support.py)
cover schemas, acceptance and SSE. [Cap publication](../tests/unit/api/test_cap_publication.py)
covers saved-but-unpublished mutations. OpenAPI from the current application is
the route inventory; do not copy an independently maintained exhaustive table.
