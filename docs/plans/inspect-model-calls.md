# Inspect Model Calls

## Goal

Add model-call inspection as a first-class `inspect` target. A user can inspect
either:

- the normalized `ModelCall` persisted for a historical model step; or
- the normalized first `ModelCall` that an `agic` would produce from the
  current program and supplied inputs.

The same target can use `--request` with an exact model ID supplied through
`--default model=...` to display the actual provider request that the model's
adapter would build. This feature is read-only; sending the projected request
is deferred.

## Success Criteria

- `too alice inspect model_call@run_id.0.1` displays the persisted model call
  for that historical model step.
- `too alice inspect model_call --default runnable=agic:review` prepares and
  displays the structured first model call without executing the `agic`.
- With no view option, both target forms display a complete structured
  `ModelCall`, not a summary.
- `--request` writes the sanitized provider-native JSON request body produced
  for the exact `--default model=PROVIDER/MODEL_ID` without sending it.
- `--request` without an exact default model binding is rejected.
- `--send` is not added in this scope; all supported forms remain read-only.
- Inspection never exposes API keys, authorization values, or other adapter
  secrets.
- Thread IDs, run IDs, step paths, and model-call targets are parsed into one
  typed inspect-target union and handled through one dispatcher.
- Existing inspect targets and normal execution behavior remain compatible.

## Current Behavior

`agic` execution already prepares a normalized `ModelCall` from rendered
instructions, rendered context, messages, tools, services, model selection, and
runtime facts. The model step persists that call before invoking the adapter.
Historical step JSON therefore contains the normalized call under the step's
given value.

The current generic step inspector can expose that nested value, but model calls
are not addressable directly and the human presentation is not model-call
specific. There is also no supported way to prepare a future first call or to
inspect the provider payload constructed by an adapter.

The CLI currently parses thread IDs, run IDs, and step paths in
`parse_inspect_target()`, then `_inspect()` branches on the parsed thread/run
shape. A step is represented as a run target with a nonempty index tuple rather
than as its own target kind. Adding model calls as a separate command path would
duplicate parsing, resource selection, errors, and presentation dispatch.

## Scope

This feature includes:

- direct historical and prospective model-call selectors;
- integration of model calls into the shared inspect parser and dispatcher;
- structured human and JSON presentations of normalized model calls;
- explicit hypothetical inputs for prospective preparation;
- optional inclusion of existing thread history;
- exact model-ID resolution and provider-request projection through that
  model's adapter;
- conditional inspect routing for each read-only form.

This feature does not include:

- inspecting an arbitrary future iteration of an agent tool loop;
- executing tools returned by a sent request;
- sending a projected provider request;
- replaying or resending a historical request;
- editing a model call or provider request;
- exposing raw credentials or unredacted transport data;
- changing the HTTP inspection API; or
- changing normal `agic` execution semantics.

## Selector Grammar

The canonical inspect grammar gains one target kind while preserving existing
thread, run, and step forms:

```text
INSPECT_TARGET     := THREAD_ID | RUN_ID | STEP_PATH | MODEL_CALL_TARGET | ...
MODEL_CALL_TARGET := "model_call" | "model_call@" STEP_PATH
```

The ellipsis represents the control and value targets defined by inspect
navigation; they remain members of the same parser and dispatcher rather than
being nested under model-call parsing.

Examples:

```sh
too alice inspect model_call@run_ab12.0
too alice inspect model_call@run_ab12.0.1
too alice inspect model_call --default runnable=agic:review
too alice inspect model_call --default runnable=agic:default
```

`STEP_PATH` uses the existing run-step path syntax. The selected historical
step must be a model step. The bare `model_call` target is prospective and
resolves its agic from the normal `--default runnable=...` binding.

The `model_call@` prefix is intentional: the inspected concept is a model call,
while the suffix identifies the historical step that owns it.
Because `@` is also used by control paths, the parser recognizes reserved typed
prefixes such as `model_call@` before applying the generic owner-control grammar.
`:` remains invalid in inspect target paths; runnable syntax belongs to the
default binding parser.

## Unified Parsing And Dispatch

There is one public `parse_inspect_target(raw)` entry point. It is a pure syntax
operation and returns a discriminated union rather than a run target with
optional path state:

```text
InspectTarget :=
    ThreadInspectTarget(thread_id)
  | RunInspectTarget(run_id)
  | StepInspectTarget(step_path)
  | ModelCallInspectTarget(owner)
  | existing control and value target variants

ModelCallOwner := HistoricalModelCallOwner(step_path)
                | ProspectiveModelCallOwner()
```

The parser validates the complete outer grammar before any store or authored
state access. It does not check that a record, model step, or `agic` exists.
Those semantic checks belong to the selected handler.

All target kinds enter one `InspectDispatcher`. The command does not branch on
thread, run, step, or model-call spellings and does not open resources before
dispatch requirements are known. Dispatch has two explicit phases:

1. select the handler, validate target-specific options, and return its
   `InspectRequirements`; and
2. open only those concrete resources, run the handler, and return a shared
   `InspectDocument` for human or JSON presentation.

`InspectRequirements` describes, at minimum, whether the handler needs the
execution store, prepared authored state, model resolution, write access, and
provider network access. This keeps routing declarative and prevents the CLI
from accumulating target-specific conditionals.

The initial dispatcher may use an exhaustive match over the closed target
union; a dynamic plugin registry is not required. Adding a target requires its
parser variant, handler, requirements, document kind, renderer, and tests to be
registered together. An unhandled parsed target is an internal error, never a
fallback to thread inspection.

Existing semantic error ordering is preserved: invalid syntax fails during
parsing; a missing owner fails before a missing child; and a historical
model-call target resolves its run, then step, then verifies that the step owns
a `ModelCall`.

## CLI Contract

Historical form:

```text
too TARGET inspect model_call@STEP_PATH
    [--full | --json]
    [--request --default model=MODEL_ID]

MODEL_ID := PROVIDER_ID "/" PROVIDER_MODEL_ID
```

Prospective form:

```text
too TARGET inspect model_call
    --default runnable=AGIC_REF
    [--default model=MODEL_ID]
    [--full | --json | --request]
    [--input CONTENT]
    [--arg NAME=CONTENT]...
    [--thread]
    [--allow CAPABILITY]...
```

The view and action matrix is:

| Options | Historical step | Prospective `agic` |
| --- | --- | --- |
| none | structured persisted `ModelCall` | structured prepared `ModelCall` |
| `--json` | persisted `ModelCall` JSON | prepared `ModelCall` JSON |
| `--request --default model=MODEL_ID` | project the persisted call for that model as a provider-native JSON body | project the prepared call for that model as a provider-native JSON body; no send |
| `--request` without an exact default model | error | error |

`--full`, `--json`, and `--request` are mutually exclusive view selectors.
`--request` already implies JSON because it emits the concrete JSON request
body; combining it with `--json` is rejected as redundant.

`--request` is a parameterless view selector. `MODEL_ID` is supplied through
the normal `--default model=MODEL_ID` binding and is one exact model ID accepted
by the runtime model catalog. It is
the canonical `provider/model_id` identity, where the first slash separates the
provider and the remaining model ID may contain more slashes. It is not an
alias, wildcard, selector expression, or optional override. Resolving the model
ID yields the concrete model target and adapter used for request projection.
Unknown or unavailable model IDs fail before payload construction and before
any durable or network action.

Examples:

```sh
too alice inspect model_call@run_ab12.0 --default model=openai/gpt-5 --request
too alice inspect model_call --default runnable=agic:review \
  --default model=openai/gpt-5 --request
```

Historical request inspection is a projection through the model ID supplied
now. The model ID can differ from the model originally recorded on the step.
The command answers "what provider request would this persisted `ModelCall`
produce for this model now?" and does not claim to reconstruct the bytes sent
by the historical run.

`--json` encodes the normalized `ModelCall` as lossless machine-readable JSON.
`--full` disables normal human-display truncation for the default structured
view. Neither option changes whether a request is sent.

## Prospective Inputs

Prospective inspection uses normal authored input resolution and content
coercion:

- `--input CONTENT` supplies the primary input;
- repeatable `--arg NAME=CONTENT` supplies named inputs;
- `--input -` explicitly reads the primary input from standard input;
- standard input is never consumed implicitly;
- missing required inputs and unknown named inputs are errors; and
- normal defaults are applied.

Examples:

```sh
too alice inspect model_call --default runnable=agic:review --input draft.md
too alice inspect model_call --default runnable=agic:review --arg draft=draft.md
too alice inspect model_call --default runnable=agic:review --input -
```

`--thread` includes the target's current inspectable thread history using normal
conversation conversion. Without `--thread`, prospective preparation starts
with no prior conversation. The option is rejected if there is no unambiguous
current thread.

`--allow` supplies capabilities needed for preparation and request projection;
it does not execute tools.

## Structured ModelCall View

With no view option, inspection displays the entire normalized `ModelCall` in a
stable structured layout. The human form contains, when present:

- selector and historical/prospective state;
- authored model-selection facts and model options present in the call;
- basis facts used during prompt rendering;
- rendered instructions;
- rendered context as incorporated into messages;
- ordered conversation messages and content parts;
- tool definitions and tool-choice configuration;
- service or adapter-facing model configuration; and
- source and resolution diagnostics relevant to the prepared call.

The presentation must preserve ordering and distinguish absent, empty, and
redacted values. Large instructions, messages, tool schemas, and binary parts
may be bounded in the default human form, with explicit truncation markers.
`--full` removes presentation truncation. JSON is never lossy.

Historical mode reconstructs this view from the exact persisted normalized
call. Prospective mode uses the same preparation functions as execution and
stops immediately after constructing the first call. It must not simulate a
model response or invent later tool-loop calls.

## Prospective Runtime Facts

Prompt rendering may depend on runtime facts that do not exist until a run is
accepted. Read-only prospective inspection uses explicit virtual facts:

- run identifier: `<preview-run>`;
- thread identifier: `<preview-thread>` unless `--thread` supplies one;
- current date and time: captured once in UTC at inspection start; and
- step path: the first model-step path that normal preparation would assign.

The structured output identifies these facts as preview values. Repeating an
inspection can therefore differ when authored prompts depend on time or mutable
setup. The output is exact for the displayed preparation basis, not a guarantee
about a later run.

## Provider Request JSON

`--request` resolves the exact model target from
`--default model=MODEL_ID` and follows it to the concrete adapter. It uses the
same provider-payload builder as transport execution. The result is the
sanitized provider-specific JSON body after model and adapter option merging.
Transport-only fields such as the endpoint, credentials, and headers are not
part of this output.

The CLI writes only the complete sanitized provider-native JSON body to stdout.
It does not wrap the body in a Toolang envelope, add headings, or interleave
diagnostics, which keeps the result directly consumable by JSON tools.

The JSON body is the actual provider-specific request body, not an approximation
derived independently from `ModelCall`. Each bundled adapter must share one
payload-building path between inspection and sending so the emitted request
cannot drift from transport behavior.

Request projection is a distinct optional adapter capability, represented by a
runtime-checkable protocol such as `InspectableModelAdapter`. It accepts a
concrete model target whose configuration includes the streaming choice and a
normalized call, then returns the provider JSON body. It performs no network
I/O.

All bundled model adapters implement the capability through their existing
payload builders, including response, chat-completion, messages, and
generate-content payloads. A third-party adapter that does not implement it
continues to execute normally, but `--request` reports the capability
as unsupported.

### Sanitization

Sanitization occurs before presentation:

- transport credentials and configured headers are excluded because only the
  JSON body is projected;
- conventional secret body keys and any header-shaped maps returned by a
  third-party adapter are redacted recursively;
- every returned value must be JSON-safe; and
- projection fails closed when an adapter cannot produce a safe
  representation.

No presentation option can reveal excluded values. `--full` and `--json` cannot
be combined with `--request`.

Read-only `--request` performs exact model and adapter resolution and
payload projection but does not call the provider, create a run, append to a
thread, write execution records, or update accounting.

## Output Contracts

`--json` for the normalized call has a stable envelope:

```json
{
  "kind": "model_call",
  "target": "model_call",
  "state": "prospective",
  "call": {},
  "basis": {},
  "diagnostics": []
}
```

`--request` does not use an envelope. Its entire stdout document is
the provider-native JSON request body. For example, an OpenAI Responses adapter
can emit a body shaped like:

```json
{
  "model": "gpt-5",
  "instructions": "Review the change.",
  "input": [],
  "tools": []
}
```

The exact keys are provider- and adapter-owned. Redacted body values use an
explicit JSON-safe marker rather than being omitted when omission would change
the apparent request shape. Credential transport fields never enter the body.

## Persistence

Read-only call and request inspection add no execution records and require no
store schema change. Historical projection uses the persisted normalized
`ModelCall` plus the model ID supplied by the user; provider request bodies are
not newly captured or backfilled.

No credential may enter logs or inspection output.

## Conditional Routing

The unified dispatcher derives routing from the parsed target and options:

| Target and view | Store | Program/input preparation | Model resolution | Writable | Network |
| --- | --- | --- | --- | --- | --- |
| thread, run, step, or historical call | read | no | no | no | no |
| historical call `--request` | read | no | exact default model and adapter | no | no |
| prospective call | only for `--thread` | yes | no provider adapter | no | no |
| prospective call `--request` | only for `--thread` | yes | exact default model and request projection | no | no |

Roaming or visiting prospective inspection may use normal dependency
materialization. Historical normalized-call inspection remains layout-only and
no-fetch. Historical request projection may resolve an already available model
from current setup, but it does not fetch target dependencies or contact the
provider.

This replaces any blanket assumption that every inspect command is layout-only.
The dispatcher selects the narrowest requirements before resources are opened.

## Core Design

Keep responsibilities with their owning concepts:

- language and setup packages continue to own authored resolution;
- the executor's `agic` preparation owns normalized `ModelCall` construction;
- model adapters own provider-specific payload construction;
- inspection owns fail-closed sanitization of projected JSON bodies;
- inspection owns the typed target union, parser, dispatcher, view selection,
  requirements, documents, and presentation; and
- CLI orchestration resolves concrete environment and routing choices before
  calling core services.

Extract the existing first-call preparation into a side-effect-free service
used by both normal `agic` execution and prospective inspection. Do not create a
second prompt-rendering implementation in inspection.

## Compatibility Constraints

- Existing inspect selectors, output, and flags retain their current meaning.
- Existing thread, run, and step targets move behind the shared dispatcher
  without behavior changes; model calls do not bypass that dispatcher.
- Existing `ModelAdapter` implementations remain valid; request inspection is
  an optional additive capability.
- Normal `agic` execution retains its prompt, tool-loop, and provider behavior.
- Historical normalized calls remain inspectable for existing stores.
- The execution HTTP API is unchanged in this scope.
- Public command parsing must reject ambiguous legacy spellings rather than
  silently reinterpret them.

## Design Touchpoints And Likely Files

Likely touchpoints, subject to implementation-time verification:

- the existing inspect command module, or a focused extracted inspection
  package, for the typed target union, canonical parser, dispatcher,
  requirements, documents, and renderers;
- `src/toolang/cli/` for option declaration and concrete resource opening after
  dispatch requirements are selected;
- `src/toolang/execution/inspection/` for model-call and request resolution
  services when those behaviors are extracted from CLI orchestration;
- `src/toolang/execution/executor/prepare.py` for reusable side-effect-free
  first-call preparation;
- `src/toolang/execution/executor/steps/model.py` for shared request preparation
  and one-call execution reuse;
- existing model catalog and setup resolution for exact `MODEL_ID` lookup;
- `src/toolang/plugin/model/` contracts for request projection capability;
- bundled provider adapter payload builders for shared projection and
  transport preparation; and
- CLI, executor, adapter, event, persistence, migration, security, and routing
  tests under `tests/`.

Implementation scope must be refined to exact files before editing and must
preserve unrelated worktree changes.

## Acceptance Tests

### Selectors And Validation

- Parse thread IDs, run IDs, step paths, and model-call targets through the same
  public parser into distinct typed variants.
- Dispatch every parsed variant through the same public dispatcher.
- Assert the CLI contains no model-call-specific bypass around dispatch.
- Parse historical nested paths such as `model_call@run_id.0.1`.
- Parse the prospective `model_call` selector.
- Distinguish `model_call@...` from existing control targets before applying the
  generic `@` grammar.
- Reject a historical non-model step with a model-step-specific error.
- Reject unknown `agic` names, malformed selectors, and ambiguous thread use.
- Parse `--request` as a parameterless flag and require exactly one model
  binding through `--default model=MODEL_ID`.
- Reject aliases, wildcards, filters, malformed exact identities, unknown
  models, and unavailable models.
- Reject `--send` as an unsupported inspect option.
- Reject `--request --json` and `--request --full` as
  conflicting view selectors.
- Reject any send form for a historical selector.
- Reject prospective-only input, thread, or allow options on historical
  selectors.

### Structured Normalized Calls

- With no options, historical inspection displays the complete persisted call
  in structured form.
- With no options, prospective inspection displays the complete prepared first
  call in the same structured vocabulary.
- Rendered instruct and context content appears in its execution-equivalent
  location.
- Message and tool ordering is preserved.
- Absent, empty, truncated, and redacted values remain distinguishable.
- `--full` removes human truncation; `--json` is lossless.
- Prospective inspection does not create runs, steps, threads, or accounting
  records.
- Time and virtual identifiers used for preparation appear in the basis.

### Provider Requests

- `--request` writes exactly one provider-native JSON body to stdout
  and performs no network I/O.
- Request stdout has no Toolang envelope, headings, or diagnostics and parses
  as one JSON document.
- Historical request projection uses the supplied model ID and may differ from
  the model originally recorded on the step.
- The same normalized call can be projected through two supported model IDs,
  each using the correct adapter and provider body schema.
- Each bundled adapter projects the same payload that its transport consumes.
- Adapter and model options merge identically in projection and execution.
- Streaming selection is represented correctly.
- Historical projection works with existing persisted model calls and requires
  no schema migration or request backfill.
- Unsupported third-party adapters report `unsupported` while normal execution
  remains usable.
- API keys, authorization values, custom-header values, URL secrets, and
  adapter-declared secret body paths never appear in human output, JSON, events,
  logs, or storage.

### Routing And Regression

- Derive resource requirements before opening execution, authored-state, or
  provider resources.
- Historical inspection performs no dependency fetch or current-state
  preparation.
- Prospective read-only forms prepare only the state required by their view.
- No model-call inspect form writes execution state or calls a provider.
- Existing inspect targets retain their behavior.
- Existing syntax and semantic error ordering for thread, run, and step targets
  remain unchanged after moving them behind the dispatcher.
- Existing normal `agic` execution and default offline tests remain unchanged.

## Risks

- Duplicated payload construction could make displayed requests inaccurate.
  Adapters must share one payload-building path between projection and
  transport.
- Provider requests can contain secrets outside conventional headers. The
  projection protocol must be fail-closed and adapter-aware.
- Prospective calls can differ from later execution because of time, mutable
  setup, or thread changes. Preview basis facts and send-time re-preparation
  make the boundary explicit.
- Historical projection can differ from the historical transport because it
  uses a model ID and adapter configuration resolved now. Output and
  documentation must call it a projection, not a recorded request.
- Prefix and delimiter overlap can make dispatch ambiguous. A single parser
  with reserved-prefix precedence and a closed target union keeps grammar
  decisions centralized.

## Open Questions

None. This definition chooses a structured normalized-call default view,
parameterless `--request` as a read-only provider-native JSON body using the
default model binding, and no send mode.
