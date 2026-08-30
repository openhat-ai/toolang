# Model Structured Output Contracts

## Status

Proposed. Implementation requires explicit human approval and is split into two
changes after this definition is merged.

## Goal

Represent a runnable's output contract as normalized JSON Schema on every
logical model call, independent of behavioral instructions and provider wire
formats. Preserve that contract for the complete turn so tool calls, model
continuations, State reloads, and output repair all pursue one stable result.

Use `continuation` for the typed model API while retaining the compact `cont`
key in canonical and durable payloads.

## Success Criteria

- `ModelCall.structured_output` is either the final value's normalized JSON
  Schema or `None`; it has no wrapper or Agic/type-name identity.
- The output contract is resolved once when an Agic invocation starts and is
  identical on every model call in that invocation.
- Logical instructions no longer contain the output contract.
- Built-in model adapters translate the normalized schema to their provider
  request and keep provider-specific response details out of the logical call.
- Core output coercion still returns Toolang `Array`, `Struct`, and scalar
  values and retains the existing single repair attempt when permitted.
- Typed model and execution values use `continuation`; canonical JSON and
  durable Step payloads continue to use `cont`.
- Every newly persisted model call records its exact structured-output schema;
  historical calls without the field remain readable.
- The explicit `model-call` inspect projector presents the schema as an
  independent `Structured Output` section in a separate implementation change.
- The default offline verification suite passes after each implementation
  change.

## Current Behavior

An authored `AgicDecl.output` is a Toolang type name. If it is absent, execution
uses `Part[]`. The default instruction template embeds this type in an
`<output-contract>` block, so the contract is part of `ModelCall.instructions`
and can disappear when another instruction template is selected.

`ModelCall` currently carries instructions, messages, tools, and `cont`.
Adapters have no common output-contract input; provider-specific target options
can add format controls directly to a request, but those controls are neither
normalized nor captured as part of the logical call.

At the end of an Agic invocation, execution coerces the terminal assistant
message to the then-current Agic output type. Structured and scalar failures
may trigger one tools-disabled repair model call. A State reload can replace the
prepared Agic before this coercion, which also allows the output type to change
during the invocation.

Model-call persistence content-addresses instructions, messages, and tools,
then stores `cont` inline. The inspect projector reconstructs those fields and
the selected Step result, but it cannot distinguish a structured-output
contract from ordinary instructions.

## Logical Model Call

The public normalized call becomes:

```python
@dataclass(frozen=True, slots=True)
class ModelCall:
    instructions: str
    messages: list[Message]
    tools: tuple[ToolDefinition, ...] = ()
    structured_output: dict[str, object] | None = None
    continuation: ModelContinuation | None = None
```

`structured_output` describes the expected final JSON value directly. There is
no `ModelStructuredOutput` type and no field for an Agic name, Toolang type
name, schema name, or format label. Internal `$defs` names and `$ref` targets
remain valid JSON Schema implementation details.

`ModelCallResult` does not gain a structured-output field. It continues to hold
the raw normalized assistant `Message`, tool calls, usage, and provider
continuation. Validation and conversion of the terminal value remain executor
responsibilities. A provider-required schema name is adapter-owned and uses the
stable value `output`; it is not added to `ModelCall`.

## Schema Normalization

Execution derives the schema from the invocation's authored output type and the
same `StructDecl` graph used by `coerce_output`:

| Toolang output | `structured_output` |
| --- | --- |
| absent, `Part`, `Part[]`, or `Text` | `None` |
| `Number` | `{"type": "number"}` |
| `Boolean` | `{"type": "boolean"}` |
| `Json` | `{}` |
| `T[]` | an array schema whose `items` is the schema for `T` |
| declared struct | an object schema for its fields |

Struct schemas use `properties`, list non-optional fields in `required`, and
set `additionalProperties` to `false`. Optional fields may be omitted but do
not implicitly accept `null`. Referenced and recursive structs use `$defs` and
local `$ref` values. Toolang declaration names may appear only as `$defs` keys;
the root schema has no generated `title` or Toolang-specific annotation.

Schema construction is deterministic: object keys follow canonical JSON key
ordering, `required` and `$defs` are sorted by name, and semantically identical
inputs produce equal dictionaries and canonical JSON. An unknown or invalid
output type fails during invocation preparation, before the first model Step.
Schema normalization is package-owned authored-language behavior and therefore
lives with Toolang type coercion rather than in adapters or persistence.

## Turn Lifetime

Invocation preparation freezes two related values before the first model Step:

- the public normalized JSON Schema placed on `ModelCall`; and
- the private Toolang output binding needed to coerce the result into `Array`,
  `Struct`, or a scalar.

The private binding consists of the output type and the relevant struct
declarations. It is executor state, not a new public model-call type or durable
field. Final coercion and repair use this frozen binding.

Every model Step in the invocation receives the same schema object by value,
including Steps after ordinary tool calls, steering input, provider
continuation, runtime reload, and the output-repair message. Runtime reload may
refresh instructions, tools, model selection, and other prepared State for the
next Step, but it cannot change the current invocation's output binding or
schema. A separately invoked runnable, child Run, or transferred execution
starts its own invocation and resolves its own contract.

The existing repair policy remains: `Part`, `Part[]`, and `Text` are not
repaired; structured and scalar outputs may make one additional model call when
the model-call limit permits it. Repair disables tools and retains the same
`structured_output` value. The corrective user message may explain the prior
validation failure, but the schema itself is not copied into logical
instructions or messages.

## Adapter Boundary

Each built-in adapter consumes `ModelCall.structured_output` when constructing
the actual provider request:

- if the protocol has a native JSON Schema output control, the adapter maps the
  normalized schema to that control and supplies any provider-required fixed
  wrapper values such as the name `output` and strict mode;
- otherwise, the adapter adds a deterministic provider-request-only output
  directive that contains the schema and requests one unwrapped JSON value.

The fallback directive exists only in the actual provider request. It does not
mutate `ModelCall.instructions`, `ModelCall.messages`, or persisted logical
history. Streaming and non-streaming request builders must produce equivalent
output controls.

Provider continuation may allow an adapter to omit or reuse instructions,
messages, or an output control on the wire. That optimization is
provider-specific; every logical `ModelCall` remains self-contained and carries
the complete schema. Continuation never owns or implies the schema.

Normalized structured output owns the provider fields used for output format.
If `ModelTarget.options` also supplies the same adapter-owned field while
`structured_output` is not `None`, request construction fails with a clear
conflict error rather than silently choosing one value. Options remain
unchanged when no normalized schema is present.

Provider responses remain ordinary normalized Messages. Adapters do not parse
the JSON text into a Toolang value and do not synthesize a logical result from
provider metadata. This keeps provider transport, model-call normalization,
and Toolang result coercion separate.

## Instructions

The default instruction template removes `<output-contract>` entirely.
`ModelCall.instructions` contains behavioral and operating guidance that is
expected to remain mostly stable across the invocation. Selecting `instruct:
none` or a custom instruction template therefore no longer disables the output
contract.

The authored runtime context may continue to expose `runnable.output` for
general template compatibility, but built-in instructions do not use it as the
model output contract. Custom prose that mentions an output type is ordinary
authored instruction text and does not override `structured_output`.

## Continuation Naming

Typed Python vocabulary uses the full word `continuation` consistently:

- `ModelCall.cont` becomes `ModelCall.continuation`;
- `ModelCallResult.cont` becomes `ModelCallResult.continuation`;
- `ModelCallRefs.cont` becomes `ModelCallRefs.continuation`;
- `ModelStepNoted.cont` becomes `ModelStepNoted.continuation`;
- executor state, helpers, diagnostics, and adapter-local variables use
  `continuation` where they represent the complete concept.

This intentionally supersedes the typed-code naming decision in
`agent-state-revisions.md`. It is a direct plugin API rename with no `.cont`
property or constructor alias; built-in and external model adapters must update
their Python usage. `ModelContinuation` remains the type name.

Compact protocol and durable representations retain the field name `cont`.
This includes normalized model-call JSON, Step `given` and `noted` payloads,
fixtures, and existing run databases. Provider wire fields such as
`previous_response_id` keep their provider names. The explicit boundary mapping
is therefore:

```text
typed core `continuation` <-> canonical/durable `cont` <-> provider wire fields
```

## Persistence And Compatibility

New normalized model-call JSON has exactly these top-level fields:

```json
{
  "instructions": "...",
  "messages": [],
  "tools": [],
  "structured_output": null,
  "cont": null
}
```

The compact stored model Step call adds the same `structured_output` field next
to its existing content references and `cont`. The schema is stored inline as
canonical JSON-compatible data. It is expected to be materially smaller than
message and toolset history, and this avoids a runs database schema change or a
new content-address table. Repeated schemas may be deduplicated in a future
storage-only change without altering normalized model calls.

Readers accept both the legacy four-field stored call and the new five-field
shape. A missing `structured_output` field decodes as `None`; readers never try
to recover a schema from historical instruction text. Writers always emit the
new field, including explicit `null`, so absence unambiguously identifies
legacy data. Existing `cont` data is read and written unchanged, and no runs
database migration is required.

## Inspect Presentation

The second implementation change extends only the explicit `model-call`
projector. Its human section order becomes:

```text
Instructions
Messages N
Tools N
Structured Output
Continuation
```

`Structured Output` renders the recorded JSON Schema as indented JSON within
the existing 80-column section treatment. `None` renders a concise dim `None`.
The section is independent of Instructions and Messages; `[=] assistant`
continues to represent the selected model Step's raw response. It is not the
validated logical result.

Canonical `--json` uses `model_call_to_data()` and includes
`structured_output` without presentation transformation. Historical calls
whose field was absent therefore project `structured_output: null`. Projector
dispatch, subject grammar, message-part rendering, result attachment, and
footer behavior defined by the existing inspect plans do not change.

## Delivery Sequence And Scope

After this definition PR is approved and merged, implementation is split into
two ready pull requests.

### 1. Core Structured Output Chain

Included:

- JSON Schema normalization from Toolang output declarations;
- frozen invocation output binding and propagation to every `ModelCall`;
- removal of the built-in instruction output-contract block;
- built-in adapter request mapping, fallback, and option-conflict checks;
- typed `continuation` renames throughout model and execution code;
- canonical `cont` boundary mapping;
- inline persistence and legacy read compatibility; and
- unit and integration coverage for the complete chain.

Excluded:

- inspect presentation changes;
- a `ModelStructuredOutput` or logical result wrapper;
- changes to authored output syntax or final Toolang value types;
- new provider response parsing or a `model-result` projector;
- model-catalog selection changes;
- database schema changes or schema content-addressing; and
- pagination, shell completion, or unrelated inspect changes.

### 2. Inspect Model-Call Structured Output

Included:

- the independent human `Structured Output` section;
- canonical JSON projector coverage; and
- directly affected CLI documentation and tests.

Excluded:

- any execution, adapter, schema-generation, or persistence behavior;
- changes to other projectors or subject navigation; and
- rendering the final parsed Toolang value.

## Design Touchpoints

Core change:

- `src/toolang/lang/input.py` and focused language tests: own deterministic
  output-schema construction beside output coercion.
- `src/toolang/base/types/run.py` and model protocol tests: add the schema and
  rename typed continuation fields.
- `src/toolang/execution/executor/prepare.py`,
  `src/toolang/execution/executor/runs/agic.py`, and
  `src/toolang/execution/executor/steps/model.py`: freeze, propagate, repair,
  and coerce against one invocation contract.
- `src/toolang/execution/executor/prompts/instruct.default.md`: remove the
  embedded output contract.
- `src/toolang/plugin/models/adapters/`: translate the normalized schema and
  continuation at provider boundaries.
- `src/toolang/execution/types.py`, `records.py`, `store.py`, `history.py`, and
  `schemas.py`: rename typed fields, persist the schema, and accept legacy
  stored calls.
- execution, store, adapter, and integration tests: cover invariants and exact
  canonical payloads.

Inspect change:

- `src/toolang/cli/toolang/commands/inspect.py`: render the new section from the
  reconstructed logical call.
- `tests/unit/cli/test_inspect_rendering.py` and CLI integration tests: cover
  structured, absent, and legacy schemas in human and JSON modes.
- `docs/api.md`: document the new model-call field and human section.

Exact additional fixture and test files may change with the implementation,
but each pull request remains confined to its concern.

## Acceptance Tests

### Core Change

1. Schema generation covers `Number`, `Boolean`, `Json`, arrays, structs,
   optional fields, nested references, and recursive references; Part and Text
   outputs produce `None`.
2. A custom or absent instruction template changes instructions without
   changing the structured-output schema.
3. Initial, post-tool, post-steer, post-reload, continued, and repair model
   calls carry equal schemas, while a new runnable invocation resolves its own
   schema.
4. A reload that changes the authored output declaration does not change the
   current invocation's final coercion type or schema.
5. Each built-in adapter maps a schema consistently for streaming and
   non-streaming calls, preserves provider continuation behavior, and rejects a
   conflicting raw output-format option.
6. A provider without a native output control receives the deterministic
   request-only fallback while the logical instructions and messages remain
   byte-for-byte unchanged.
7. Valid structured text still coerces to the existing Toolang runtime value;
   invalid text still follows the existing repair limit and error behavior.
8. New model Steps round-trip `structured_output` exactly and keep the durable
   `cont` key; legacy stored calls without the schema rebuild with `None`.
9. Public typed call, result, record, noted, executor, and adapter paths use
   `continuation`, with no remaining typed `.cont` access.
10. The complete default verification passes.

### Inspect Change

1. Human `model-call` output places `Structured Output` between `Tools N` and
   `Continuation` and preserves all existing message, tool-part, and `[=]`
   rendering.
2. Object, array, scalar, empty (`{}`), and absent schemas render without
   losing JSON Schema keys or values.
3. `--json` contains the exact recorded schema and `structured_output: null`
   for unstructured or legacy calls.
4. Other subjects and projectors remain unchanged, and the complete default
   verification passes.

## Risks And Open Questions

Provider-native structured-output features vary by protocol and may reject
some valid JSON Schema keywords. Adapter tests therefore lock down only each
protocol's supported mapping; Toolang's normalized schema and core validation
remain authoritative. Request-only fallback preserves compatibility for other
routes but cannot guarantee provider-side conformance, so executor coercion and
repair remain necessary.

The typed `continuation` rename requires external adapter source updates. This
is an intentional source-level break; retaining compact `cont` payloads avoids
turning it into a durable-data migration.

Inline schema persistence duplicates a stable schema across model Steps. That
bounded storage cost is accepted to keep the first implementation composable
and migration-free. There are no open product questions in this definition.
