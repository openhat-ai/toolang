# Flat Call Input

## Status and Goal

Approved for implementation on 2026-09-08. Work type: refactor.
The user explicitly selected a direct cutover without compatibility for old
records or HTTP clients. The refactor preserves evaluation, binding, and
signature validation semantics; the explicitly approved protocol and storage
cutover is part of its scope.
CLI capture and help changes remain a separate improvement.

Use one flat key structure for complete call input across parsing, execution,
HTTP, runtime tools, locals, and persistence:

```json
{"_": "Review this change.", "arg1": "security", "arg2": 2}
```

Primary input is called **input**; named inputs are **arguments** (singular:
**argument**). `_` identifies input; other keys identify declared arguments.
Remove the separate primary/named compartments and duplicate input classes.

## Current Implementation

Verified against `df4ba46a`:

- `CallInput` / `RunnableInputRaw` separate `_` from a tuple of
  `NamedInputSource(name, source)`; `RunnableInput` separates `primary` from
  `named`.
- Authored HTTP sends `_` plus `named`; direct HTTP sends a primary parts list
  plus sibling `args`. Runtime tools already accept a flat input object.
- Run, Execute, Steer, and Cancel controls persist an ordered list of named
  `Local` entries. Authored input persists `primary` plus `named`.
- Control-value references use `payload/input/INDEX/value`. Execution locals
  are already keyed by `_` and argument names.
- RunStore currently accepts only schema 42. There is no general store upgrade
  framework; older schemas are rejected.

## Canonical Model

Retain the name `CallInput` for prompt input, script invocation input, and
runnable input. It represents the complete supplied input across source,
evaluation, execution, and persistence stages. The name does not restrict it
to a particular call target or emphasize its mapping implementation.

Keep one immutable, generic mapping type, `CallInput[T]`, owned by
`toolang.lang.input`. Its constructor accepts one mapping; it exposes the
standard read-only mapping interface and serializes directly as an object,
without a `values` or `data` wrapper.

```python
CallInput[str]({"_": "Review this change.", "arg1": "security", "arg2": "2"})
CallInput[Value]({"_": "Review this change.", "arg1": "security", "arg2": 2})
```

The type parameter distinguishes source text from evaluated values. These
stages remain necessary: evaluating an already resolved value as Content could
expand prompts or includes twice. Keep `RunnableInput` as the existing semantic
name for resolved runnable values, expressed only as a type alias:

```python
RunnableInput: TypeAlias = CallInput[Value]
```

Use `RunnableInput` in execution-facing annotations and constructors and
`CallInput[str]` at source boundaries. The alias has no separate class, fields,
validation, or serializer. Runtime instance checks use `CallInput`, not a
parameterized alias. The old `primary=` / `named=` constructor is removed;
retaining this semantic name does not retain the old representation.

Remove the independent `RunnableInput` class, the empty `RunnableInputRaw`
subclass, `NamedInputSource`, and `NamedInputSources`. Do not add a separate
raw-stage alias. Retain `CallInputHeader` because it describes parser boundaries,
not the resulting input object.

- `CallInput[str]`: each present value is unresolved Content source text.
- `CallInput[Value]`: each present value has been resolved and signature-checked.
- Execution-owned annotations may use `CallInput[Value | TypedRef]` for durable
  input values. The language package must not import execution reference types.
- No `.primary`, `.named`, or separate primary/named constructor parameters.
  Access input with `values["_"]` and arguments with `values[name]`.
- Freeze the mapping at construction; retain existing canonical value
  validation and immutable value snapshots at resolution boundaries.

### Related Definition Audit

| Current definitions | Decision |
| --- | --- |
| `RunnableInputRaw(CallInput)` | Empty subclass; use `CallInput[str]` directly. |
| `RunnableInput` and `CallInput` | Their current value stages differ; consolidate implementation and retain `RunnableInput = CallInput[Value]` as a semantic alias. |
| `NamedInputSource` and `NamedInputSources` | Flat map entries replace both carriers. |
| `DirectRunnableRequest` and `RunnableRequest` | Their split input representations converge on one generic `RunnableRequest[T]`. |
| `RunCreateRequest`, `AuthoredRunRequest`, and `RunRequest` | Shared envelope fields are an overlap, but HTTP validation and execution request semantics differ. Reuse the generic input/request definitions; retain boundary validation without introducing another hierarchy solely for five shared fields. |

`CallInputHeader` (capture boundaries), `InputResolution` (parts and prompt
provenance), `RunSpec` (prepared invocation), and `BoundRun` (accepted execution)
have distinct responsibilities. Working Flow state and durable locals remain
distinct responsibilities; the durable Local is reused in output values.

### Local Values and Output Bindings

The user extended the refactor to separate output binding from the local value:

```python
@dataclass(frozen=True, slots=True)
class Local:
    value: Value | TypedRef
    dim: Literal[0, 1] = 0

@dataclass(frozen=True, slots=True)
class Output:
    local: Local
    binding: str | None = None
```

- Local maps use `{"_": Local(value=input_value, dim=0)}`. Names live in the
  outer mapping; Local has no `name` field.
- Run/Step outputs and end events use `Output(local=local, binding=name)`.
  `binding=None` means unbound, `binding="_"` updates the current local, and
  other names select named locals. The binding does not carry dimension.
- Keep `dim` validation and value snapshots in Local. Array values may be one
  item (`dim=0`) or a collection (`dim=1`), including unbound results.
- Durable output is `{"local": {"value": ..., "dim": 0}, "binding": "_"}`.
  HTTP and events reuse Local's typed projection inside the same envelope.
- `output=None` means no output; `Output(Local(None), binding)` is a supplied
  JSON null result. Preserve this distinction through serialization.
- Actual-value references use `output/local/value`. The intermediate
  `output/local` selects Local; `output/binding` selects its destination. Update
  retry/rerun, child calls, history, inspection, and message reconstruction
  together, with no legacy field aliases.
- Include this output cutover in the same unreleased RunStore schema 43.

## Presence and Validation

- An absent `_` key means no primary input. `"_": ""` is explicit empty text;
  an empty typed array is an explicit empty array. Never use truthiness to
  determine presence: `0`, `false`, and empty values remain supplied values.
- Source maps contain strings only. Explicit primary `null` is rejected in
  the new protocol; absence is expressed by omitting `_`. This does not add
  nullable primary input semantics. A named argument may contain JSON null
  when its declared type accepts it.
- Names retain their existing boundary rules: prompt parameters may contain
  hyphens; runnable inputs use canonical runnable argument names. `_` is reserved for primary
  input; other supplied names must match the target signature. Preserve
  required/optional arguments, forbidden primary input, and all type coercion.
- Reject duplicate assignments before collecting them into a mapping. HTTP
  decoding must also reject duplicate members of the input object before a
  normal dictionary could overwrite them; ordinary nested JSON value decoding
  otherwise retains its existing contract.
- Preserve source argument evaluation order and prompt provenance. Mapping
  member order does not determine argument binding or durable references.
- A no-primary signature requires `_` to be absent; omitted signatures still
  imply required `_: Part[]`. Capture markers and CLI synopsis are governed by
  the separately discussed CLI definition; this change adds no capture forms.

Text adapters collect source strings into one map. Resolution accepts that map
and the concrete signature, evaluates Content once, and returns one value map.
Value adapters decode supplied values against the signature without Content
evaluation. Existing policy-prefix and conflicting-input-source checks remain.

## HTTP and Runtime Tools

Keep endpoint paths and the outer request envelope (`thread_id`, `request_id`,
`runnable`, `model`, and `policy`). Both run endpoints use the same runnable
object keys, `ref` and `input`:

```json
{
  "runnable": {
    "ref": "flow:demo",
    "input": {"_": "Review this change.", "arg1": "security", "arg2": 2}
  }
}
```

This is a request fragment; the normal outer request fields remain required.

- `/api/v1/runs/stream` accepts a flat value object. Reuse the signature-driven
  JSON-value decoder used by runtime calls for every entry, including `_`.
  Text, typed scalars, arrays, structs, and multimodal Part values remain
  supported. A parts list belongs under `_`; it is not the input container.
- `/api/v1/runs/authored/stream` accepts the same flat object shape with string
  values. In the example its `arg2` is `"2"`, evaluated and coerced to Number.
  Keep this endpoint distinction so source text and literal values are never
  guessed from their JSON shape.
- Omitted `input` defaults to `{}` consistently; explicit `input: null` is
  invalid. Requiredness is then checked against the selected signature.
- Remove `DirectRunnableRequest`; parameterize the existing
  `RunnableRequest[T]` over the value type and use `CallInput[T]` for its input.
  Keep outer request types only where their endpoint validation differs.
- Remove sibling `args` and the legacy nested `named` list. Reject old
  envelopes with HTTP 422 and a concise flat-input example. Do not reject valid
  declared arguments merely because they are named `primary`, `named`, or
  `args`; those are ordinary keys inside the new input map.
- `_toolang/run` and `_toolang/execute` keep their already flat `input` shape
  and use the same value resolver. Other tool-protocol argument objects are
  not renamed.
- Update remote clients, OpenAPI schemas, HTTP conversion, control responses,
  and inspection projections in the same release. Steer message/cancel reason
  request envelopes stay as their existing commands; their persisted complete
  input maps follow the rules below.

## Persistence and References

Store complete input maps directly in Run, Execute, Steer, and Cancel payloads:

```json
{
  "input": {"_": "Review this change.", "arg1": "security", "arg2": 2},
  "authored_input": {"_": "Review this change.", "arg1": "security", "arg2": "2"}
}
```

`authored_input` remains optional provenance: it records source text, while
`input` records accepted values or typed references. Both share the same keys.
Preserve the existing absence of an authored snapshot separately from an
existing empty snapshot (`null` versus `{}`).

- Use the existing self-describing value codec per map member. Typed arrays,
  structs, parts, and references retain their encodings; the outer input map
  has no value tag, list wrapper, `name`, or per-entry `value` wrapper.
- Call-entry inputs are individual typed values. Their array-ness belongs to
  the value type, not an input `dim` field. Remove input-only `Local` wrapping;
  retain Local dimensions and represent Flow/Step/Run outputs with the
  separate `Output(local=local, binding=name)` envelope.
- Bind runtime locals from the map, preserving typed references and existing
  call-entry coercion. Reconstruct an empty working `_` local where execution
  requires one even if the call signature accepts no input; it is not an
  additional supplied input.
- New control pointers address `payload/input/_` or `payload/input/arg1`,
  followed by the actual value-codec path (for example, `items/!/0` for a boxed
  array item). Remove both the list index and
  the old Local `value` segment. Use `FieldRef` token APIs, never string slicing.
- Retry, rerun, child calls, execute replacement, recall, history, and inspection
  read the new maps directly. No runtime fallback reconstructs a primary/named
  object or scans a named-input list.
- `StepRecord.input` remains a list of dependency references, and output
  bindings use `Output` envelopes containing Local values. Neither represents
  a complete call input.

## Direct Cutover

The user explicitly chose the new structure without compatibility for old
records or clients:

- Increment the current RunStore schema version at implementation time (42 is
  the definition baseline). New stores use only the new map format; existing
  incompatible stores fail the exact-version guard before any schema or data
  mutation. Do not rewrite, reset, delete, or migrate a user's old database.
- HTTP paths stay stable, but their request and input-bearing response formats
  switch atomically to the new contract. Update bundled clients in the same
  release; structurally incompatible requests fail with HTTP 422. A request
  that already has a valid new shape remains valid regardless of client age.
- Remove independent old input classes, alternate codecs, legacy field-path aliases, and
  split-envelope handling. Add no migration command, compatibility adapter,
  automatic conversion, or dual-read/write period.
- Old saved control-field paths do not gain aliases. Only newly generated
  name-based paths resolve under the new schema. Tests create new-format
  stores; old-schema fixtures verify rejection and preservation of old files.

## Implementation Touchpoints

- `src/toolang/lang/input.py`: generic input mapping, source collection, and
  signature resolution; reuse `src/toolang/lang/types.py` value vocabulary.
- `src/toolang/work/inbox.py`: pass file input using the shared flat resolver.
- `src/toolang/execution/{policy,calls,schemas,remote}.py`: flat preparation and
  request contracts; remove split-source carriers and their adapters.
- `src/toolang/cli/toolang/commands/script.py` and `commands/chat/`: collect and
  pass maps; preserve independently approved CLI and Chat syntax behavior.
- `src/toolang/api/{schemas,conversion}.py` and `routers/runs.py`: endpoint
  models, duplicate-input-key detection, decoding, and diagnostics.
- `src/toolang/execution/{types,events,records,store,history,thread_view,runnables}.py`,
  `executor/`, and `tools/runtime.py`: map persistence, name-based references,
  call binding, control handling, and projections; also update `assembly.py`
  and `control_messages.py` for primary-value references. Update CLI output
  projections and API/event schemas for the output envelope.
- Existing language, execution, API, CLI, history, and serialization tests;
  focused rejection fixtures for old formats and databases.
- `docs/{call-input,input-syntax,program,flow-syntax,executor,execution,
  run-step-records,api,toolang-authoring-conventions,concepts,chat,tasks,work}.md`:
  one vocabulary,
  consistent object examples, schema version, reference paths, and cutover.

## Acceptance Tests

1. One `CallInput[T]` implementation handles source and resolved values.
   `RunnableInput` is only its `Value` specialization alias, sharing the same
   validation and serialization. `RunnableInputRaw`, named-source carriers,
   split fields, and sibling `args` do not remain in active input paths.
2. Missing `_`, empty text, empty typed arrays, `0`, and `false` stay distinct;
   reject primary null and preserve allowed null arguments. Cover signatures
   with implicit input, explicit input, named-only input, and no inputs.
3. Preserve immutable snapshots, invalid-name errors, duplicate assignments,
   unknown/missing arguments, optional arguments, and type coercion. Include
   named arguments literally called `primary`, `named`, `args`, and `input`.
4. Source prompts/includes evaluate once; literal direct/runtime values are
   never interpreted as Content. Prompt provenance and evaluation order agree.
5. Local, remote authored, direct HTTP, and runtime-tool calls bind equivalent
   values to the same signature. Test typed arrays, structs, and multimodal
   inputs. Both HTTP schemas emit a flat input object and reject duplicates,
   legacy envelopes, and invalid null containers.
6. Every input-bearing control round-trips as a name-keyed map. Scalar values
   have no redundant wrappers; typed values/references retain their codecs.
   Output envelopes reuse the same Local value and preserve optional bindings,
   dimensions, and absent versus null results. Step dependency-list semantics
   remain unchanged.
7. Exercise execution, child calls, map/scatter/gather arrays, execute, steer,
   cancel, retry/rerun, recall, history, and inspection using new field paths.
   Reordering map members cannot change a field reference's resolved value.
8. New-format stores preserve absent/empty primary values, empty maps, nested
   typed references, referenced child input, and input used by retained Step
   outputs through close/reopen, retry, and rerun.
9. A new runtime rejects old-schema stores before modifying them; fixtures
   prove the old files remain unchanged. Old HTTP envelopes and old input
   codecs fail explicitly. There is no migration or legacy-format code path.
10. Examples, links, and generated OpenAPI are consistent. The default offline
    verification passes before commits:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## Risks and Approval

HTTP clients, Python callers of the removed classes, and saved input field
paths must update. Old records cannot be opened by the new runtime; they remain
intact for use with their matching older runtime. Source and resolved values
must keep distinct evaluation rules even though their structure is identical.

Uniform flattening and the incompatible cutover are user-selected. The user
approved starting the refactor and creating a pull request. No open design
questions remain.
