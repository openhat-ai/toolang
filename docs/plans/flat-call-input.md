# Flat Call Input and Output Bindings

## Goal and Scope

Approved on 2026-09-08 as a refactor with a breaking protocol and storage
cutover. Use one input structure for prompts, script invocations, runnable
execution, HTTP, runtime tools, and persistence. Separate reusable local values
from their output bindings. Preserve evaluation, signature validation, value
presence, dimensions, and reference provenance.

Primary input is shortened to **input**; named inputs are **arguments**
(singular: **argument**). `_` identifies input; other keys identify arguments.
The complete input object uses the existing name `CallInput`.

CLI capture forms, synopsis, and help panels are outside this PR. Their current
behavior remains unchanged. The terminology is documented in
[Call Input](../call-input.md).

## Canonical Types

```python
CallInput[str]({"_": "Review this change.", "count": "2"})
CallInput[Value]({"_": "Review this change.", "count": 2})
RunnableInput: TypeAlias = CallInput[Value]

@dataclass(frozen=True, slots=True)
class Local:
    value: Value | TypedRef
    dim: Literal[0, 1] = 0

@dataclass(frozen=True, slots=True)
class Output:
    local: Local
    binding: str | None = None
```

`CallInput[T]`, owned by `toolang.lang.input`, is one immutable mapping. It
copies the supplied mapping, exposes the read-only mapping interface, and
serializes directly as an object. It has no primary/named compartments or
wrapper field. `RunnableInput` is a type alias with no independent validation
or serialization. Do not retain `RunnableInputRaw`, `NamedInputSource`, or
`NamedInputSources`.

The type parameter distinguishes unresolved Content strings from evaluated
values. Source resolution evaluates Content once; direct-value calls never
interpret strings as Content. Source input is evaluated before arguments;
argument order and prompt provenance are retained. Resolution and persistence
boundaries retain canonical immutable value snapshots.

Local maps use `{"_": Local(value=input_value, dim=0)}`. Names belong to the
outer map. Outputs reuse Local through `Output(local=local, binding=name)`:

- `binding=None` leaves a result unbound; `"_"` updates the current local;
  another canonical name selects a named local.
- `dim=0` is one item, including an array-valued item. `dim=1` is a collection,
  including empty or unbound collections. Local owns dimension validation.
- `output=None` means no result. `Output(Local(None))` is a supplied JSON null
  result. Serialization must preserve the distinction.
- Actual-value references use `output/local/value`; `output/local` selects
  Local, and `output/binding` selects the destination. There are no old aliases.

The executor's working `Local` retains value, shape, type, and provenance for
live Flow evaluation. The durable `Local` represents a stored value and its
dimension. Their responsibilities remain distinct.

## Presence and Validation

- Omit `_` for absent input. Empty text, empty typed arrays, `0`, and `false`
  remain supplied values. Never infer presence from value truthiness.
- Primary null is invalid; omission is represented by key absence. Arguments
  may contain JSON null when their declared types accept it. A supplied null
  local must not be silently dropped when collecting a child call's input.
- Source maps contain strings only. Prompt parameter names may contain
  hyphens; runnable boundaries retain canonical runnable identifier rules.
- Validate supplied names, required/optional arguments, forbidden input, and
  declared types at the runnable boundary. An omitted signature still implies
  required `_: Part[]`; `()` accepts neither input nor arguments.
- Collectors reject duplicate assignments before constructing the mapping.
  Both HTTP input endpoints reject duplicate input members before dictionaries
  discard them. Ordinary nested JSON values retain their existing contract.
- Preserve existing policy-prefix and conflicting-input-source checks.

## Boundary Definitions

These are the input-bearing contracts affected by the cutover:

| Boundary | Definition | Input representation |
| --- | --- | --- |
| Prompt, script, and authored runnable input | `CallInput[str]` | Flat source strings |
| Resolved runnable input | `RunnableInput = CallInput[Value]` | Flat accepted values |
| Runnable request | `RunnableRequest[T]` in `execution/schemas.py` | `ref` plus `input: CallInput[T]` |
| Direct HTTP run | `RunCreateRequest` in `api/schemas.py` | `RunnableRequest[object]`, decoded against the signature |
| Authored HTTP run | `AuthoredRunRequest` in `api/schemas.py` | `RunnableRequest[str]` |
| Internal authored run | `RunRequest` in `execution/schemas.py` | `RunnableRequest[str]` plus execution policy |
| Run and Execute controls | `RunControlPayload`, `ExecuteControlPayload` in `execution/records.py` | `CallInput[Value | TypedRef]` |
| Steer and Cancel controls | `SteerControlPayload`, `CancelControlPayload` in `execution/records.py` | Flat concrete input under `_`, or an empty Cancel map |
| Authored provenance | `RunControlPayload.authored_input` | `CallInput[str] | None` |
| Run/Step records, end events, and HTTP output | `Output` in `execution/types.py` | `local` plus optional `binding` |

Remove `DirectRunnableRequest`; both HTTP runnable objects use the generic
`RunnableRequest[T]`. Retain outer request types where boundary validation
differs, without introducing another inheritance hierarchy for shared fields.
`CallInputHeader` owns parser boundaries, `InputResolution` owns resolved parts
and prompt provenance, `RunSpec` owns prepared execution, and `BoundRun` owns
accepted execution. None is a duplicate input container.

## HTTP and Runtime Tools

Keep endpoint paths and the outer `thread_id`, `request_id`, `runnable`,
`model`, and `policy` envelope. The runnable request is:

```json
{
  "ref": "flow:demo",
  "input": {"_": "Review this change.", "count": 2}
}
```

- `/api/v1/runs/stream` accepts direct values. Use the shared signature-driven
  JSON decoder used by `_toolang/run` and `_toolang/execute`. Support typed
  scalars, arrays, structs, and multimodal Part values. Preserve the HTTP
  boundary's strict Part and user-message validation, including nested Parts.
- `/api/v1/runs/authored/stream` uses the same map with string values (`"2"`
  for the example's count). Keep the endpoint distinction between Content
  source and literal values.
- Omitted `input` defaults to `{}`; explicit `input: null` is invalid.
  Requiredness is checked against the selected signature.
- Remove sibling `args` and the legacy nested named-source list. Invalid old
  shapes fail with HTTP 422. Names such as `primary`, `named`, `args`, and
  `input` remain valid arguments when declared in the signature.
- Update bundled clients, OpenAPI, HTTP conversion, events, control responses,
  and inspection together. Steer message and Cancel reason request envelopes
  remain command-specific; their persisted input follows the flat contract.

## Persistence and References

New stores use **RunStore schema 43**. Run, Execute, Steer, and Cancel payloads
store input as a name-keyed object. `authored_input` retains the source map;
`None` (no snapshot) is distinct from `{}` (an existing empty snapshot).

Each input member uses the existing self-describing value codec. Scalars have
no redundant Local wrapper; arrays, structs, Parts, null arguments, and typed
references retain their encodings. Call-entry array-ness belongs to the value's
type, without an input-only dimension field. Private outputs encode
`{"local": {"value": ..., "dim": 0}, "binding": "_"}`; public outputs use
Local's typed projection inside the same envelope.

Input references use `payload/input/_` or `payload/input/argumentName`, followed
by the value-codec path, such as `items/!/0` for a boxed array item. Use `FieldRef`
token APIs. Map order cannot determine binding or reference identity.

Retry, rerun, child calls, execute replacement, recall, history, and inspection
read maps directly and retain concrete value types and source references.
Reconstruct an empty working `_` local where execution requires one, without
adding a supplied input. `StepRecord.input` remains a dependency-reference
list; it is not complete call input.

## Cutover and Risks

Old records and clients are explicitly unsupported. Incompatible stores must
fail the exact-version guard **before any schema or data mutation**. Do not
migrate, reset, delete, or rewrite old databases. They remain usable with the
matching older runtime.

HTTP paths stay stable while request and response shapes switch together.
Python callers of removed classes and saved field paths must update. Add no
compatibility adapters, dual codecs, old field-path aliases, or migration
commands. Valid new-shaped requests remain valid regardless of client age.

## Implementation and Acceptance

Primary owners are `lang/input.py`, `execution/{calls,policy,schemas,types,
records,events,store,history}.py`, `execution/executor/`, and `api/{schemas,
conversion,routers/runs}.py`. Adapt Script, Chat, Work inbox, inspection, progress,
and history consumers; update their documentation and existing tests.

Acceptance requires:

1. One generic CallInput implementation and only the RunnableInput alias;
   no active split input carriers, temporary wrappers, or legacy codecs.
2. Presence, canonical names, duplicates, unknown/missing arguments, coercion,
   null rules, value snapshots, and argument ordering remain covered.
3. Source prompts/includes evaluate once with retained provenance. Local,
   authored HTTP, direct HTTP, and runtime calls agree on accepted values.
4. All input-bearing controls and outputs round-trip through records, events,
   and HTTP. Preserve output bindings, dimensions, and absent/null results.
5. Exercise child calls, map/scatter/gather, execute, steer, cancel, retry/rerun,
   recall, and history with new references, including nested typed references
   and close/reopen. Reordering input entries must not alter reference values.
6. Old-schema tests prove rejection leaves existing files unchanged. Reject
   old HTTP envelopes and input codecs. Examples and OpenAPI match the new
   contract.
7. The default offline verification passes before every commit:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

No open design questions remain.
