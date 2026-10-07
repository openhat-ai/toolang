# Signature-boundary Input Handling

Status: approved for implementation after PR #700 merged. Definition baseline:
`origin/main` at `0db8486b`.

## Goal and success criteria

Before invoking a target, bind and coerce supplied values against its input
signature. Reject values that cannot be converted before executing the target.
Execution and durable runnable inputs must use the resulting values; raw caller
data remains diagnostic evidence. Preserve already-bound values when restoring
the same contract.

Success means the four failures below have regression coverage, the existing
input/output contracts remain intact, and implementation verification passes.

## Confirmed changes

| Boundary | Current failure | Required change |
| --- | --- | --- |
| Model-requested `exec` | Execution uses coerced values, but its control references raw tool JSON; a successful passthrough Run can have unreadable typed output. | Persist the target-bound values and build replacement locals from those values. |
| Python `@tool` invocation | `count: int` receives `"7"` or `"bad"` unchanged; unknown arguments are silently dropped. | Bind names and coerce types before path preflight and invocation; reject invalid calls. |
| Python `@tool` schema | Postponed annotations such as `int`, `bool`, and `list[str]` become `{}`. | Resolve input annotations once and derive schemas and coercion from the same resolved types. |
| `rerun` | A stored Text `"7"` is rejected when the fresh target expects Number. | Rebind restored native values against the selected target before admission. |

## Scope

In scope: these four fixes; function-tool loading and filesystem wrappers needed
to carry normalized arguments; offline regression tests; plugin/input contract
documentation and implementation changelog entries.

Out of scope: new Flow syntax, async/await capabilities, output coercion rules,
new Toolang scalar/array/struct conversions, retry semantics, historical record
repair, configuration parsing, or universal validation of third-party/MCP JSON
schemas. Hand-written `Tool` implementations continue to own their argument
semantics; remote services continue to own their target schemas.

## Shared rules

1. The target contract determines conversion. Raw-value equality is not an
   acceptance criterion. Do not force Python and Toolang into one type system.
2. Resolve the target and its signature from the same captured State or loaded
   tool used for invocation. Failed conversion must not admit a child/Run,
   commit an exec transfer, run path hooks, or enter a function body.
3. Keep source `ToolCall.input` for inspection and correlated provider replies.
   It is not a typed runnable input record. Store resolved runnable values or
   references to values already satisfying that contract, never raw JSON
   references merely relabeled with the target type.
4. Restoration is not source decoding. Preserve native Json strings, Parts,
   arrays, structs, and absent arguments. Re-enter coercion only at a new
   invocation boundary; do not reinterpret strings during history reads/retry.

The existing [Call Input](../call-input.md) contract remains authoritative for
Toolang values and the distinction between authored source, direct JSON, and
native values.

## Function-tool binding and schemas

- Prepare the callable's signature and resolved input annotations when creating
  the function tool. Reuse Pydantic v2, already a dependency, for value adapters
  and schema generation. Resolve forward references in the callable's namespace;
  an unresolved input annotation is a preparation error naming the parameter.
  Do not quietly turn unsupported annotations into `{}`.
- Support the existing JSON-facing primitives, `None`, `list[T]`,
  `dict[str, T]`, optional/union types, `Literal`, and Pydantic's built-in strict
  type metadata; recurse into containers. Arbitrary custom validators are outside
  this scope because repeated binding must be pure and idempotent.
  Missing annotations and `Any` retain native JSON values without parsing
  strings. Other annotation families fail tool preparation in this scope;
  function return annotations do not define a new output-validation boundary.
- Use Pydantic's lax conversions, with finite numeric values required and an
  explicit pre-conversion Boolean guard on numeric branches, recursively through
  containers. `True` must not become `1` or `1.0`; a union with a Boolean branch
  may still accept it as Boolean. This preserves the bundled tools' existing
  Boolean/number separation. Explicit strict types remain strict. `"7"` can
  become `7`; `"bad"` cannot. Do not add blanket `str(value)` conversion or parse
  arbitrary strings as JSON collections. Schema describes the target types;
  accepting convertible source values is intentional.
- Bind positional-or-keyword and keyword-only parameters by name. Reject
  unknown names unless a real `**kwargs` parameter exists; bind its extra values
  using its annotation, or `Any` when unannotated. Reflect that policy in
  `additionalProperties`. Reject positional-only parameters and `*args` during
  tool preparation instead of exposing unusable keyword fields.
- Keep omitted optional arguments omitted in the bound mapping. Python applies
  function defaults at invocation; validate those defaults against the declared
  types without coercing them during preparation. A wrong-typed default is an
  author error, not a fallback for invalid caller input. Missing required values
  and explicit null for a non-nullable parameter fail before execution.
- Exclude the reserved `context` parameter from the public signature/schema and
  inject it only from the runtime. Caller-supplied `context` is rejected even
  with `**kwargs`. It must not become a silently ignored input.
- Preserve explicit `@tool(parameters=...)` schemas, including descriptions,
  defaults, enum/minimum/minLength restrictions and pinned web-backend choices.
  An override does not disable callable signature binding or provide additional
  coercions. Domain restrictions remain plugin-owned, as today; this work does
  not introduce a general JSON-Schema validator. Missing annotations require
  adding Python annotations when a plugin wants typed coercion.

[TypeAdapter](https://docs.pydantic.dev/latest/concepts/type_adapter/) supports
reusable validation and schema generation;
[Pydantic's conversion table](https://docs.pydantic.dev/latest/concepts/conversion_table/)
defines the underlying lax behavior, subject to the numeric guards above.
Toolang's existing language coercions remain separate and unchanged.

### Hook ordering and wrappers

Add one non-abstract `Tool.bind_arguments(arguments)` hook: a synchronous,
side-effect-free normalization returning a new mapping. The default copies the
mapping; function tools enforce the contract above. Standard binding must be
idempotent and must not retain call data on shared tool instances.

The executor binds after resolving the loaded tool and before `paths` or any
operation. Pass the bound mapping to both preflight and invocation. Retain raw
arguments for `summary` and call records. Binding errors use the existing failed
Tool Step/error reply path, without calling `paths` or `invoke`.

`LoadedTool` forwards binding. The filesystem wrapper rejects public `workspace`
and `cwd` arguments, binds the public values before resolving paths, and injects
its authorized workspace only when delegating to the inner callable. Do not
expose that injected argument or `context` in the public schema. Use the existing
invocation-local path cache for preflight and execution.

Direct function-tool `paths` and `invoke` calls enforce the same binding; they
must not become bypasses when used outside the executor. Rechecking an already
bound mapping is safe and must not mutate it. Summaries remain best-effort raw
diagnostics, including when binding fails. Existing sync-thread dispatch,
async/awaitable handling, cancellation and `ToolResult` behavior are unchanged.

This deliberately changes the documented function `paths` hook from raw values
to bound supplied values. Omitted defaults remain omitted. Update
[Plugins](../plugins.md) and add migration guidance for hooks that parsed raw
numeric/Boolean strings themselves.

## Runnable invocation and restoration

### Exec

Continue using `resolve_public_input` for the selected target. Give the resulting
native mapping to `prepare_execute` as both execution input and control input.
Retain the raw model call and the exec control's causal links for inspection.
Do not weaken Store validation or attempt coercion when reading output. Primary
and named inputs follow the same rule, including nested Parts and empty arrays.

### Rerun

In the rerun admission path, after `_source_spec` restores the source invocation,
resolve the selected runnable/module in the captured fresh State and call
`bind_runnable_input` with those native values and the target module's structs.
Replace the new specification's input before `_prepare_run_spec` and Store
admission. Apply this for each rerun, not only when textual type names differ.

Do not use `decode_runnable_input` or evaluate stored authored source again.
Unknown/removed arguments, newly required arguments, and unconvertible values
reject the entire rerun before a new Run/control is created. Optional missing
arguments remain missing. Leave the source Run and its records untouched; the
new entry control stores the rebound values. Existing identity/module checks,
launch context, resource limits and output contracts remain in force.

Keep this rebinding out of the shared `_source_spec` and generic
`_prepare_run_spec` paths: retry uses the recorded State and restores its
existing contract, while `RunSpec` already requires resolved input. Preserve
retry compatibility checks and committed-prefix restoration. Existing native
conversion limits apply; this plan does not add element-wise conversion between
arbitrary typed arrays or structural migrations between incompatible structs.

## Implementation sequence and touchpoints

1. [x] Implement function binding and annotation-derived schemas together with
   the new hook, executor ordering, and wrapper forwarding. Keep this one
   reviewable change so preflight cannot lag behind invocation normalization.
   Touch `base/protocols/tool.py`, `base/utils/function_tools.py`,
   `plugin/toolsets/loading.py`, `plugin/toolsets/fs.py`, and
   `execution/executor/steps/tool.py` under `src/toolang/`.
2. [x] Correct exec's control input in
   `src/toolang/execution/executor/tool_runtime.py`; add runtime-call persistence
   and rejection regressions.
3. [x] Add rerun-only native rebinding in
   `src/toolang/execution/executor/executor.py`; cover local and HTTP admission
   and unchanged retry behavior.
4. [x] Update `docs/plugins.md` and `docs/call-input.md`; generate the relevant
   `CHANGELOG.md` compatibility/fix entries through `too aide.too update_changelog`.
5. [x] Run the acceptance scenarios below and the repository's default
   verification before each implementation commit. Fetch/rebase and verify
   again before handing off each PR; humans approve scope and merging.

## Acceptance scenarios

| Scenario | Pass condition |
| --- | --- |
| Function coercion | Sync and async callables receive declared types for scalar and nested container inputs; invalid values invoke neither paths nor body. Cover finite numbers, fractions, Boolean conversions and nullable unions; numeric scalars/containers reject Boolean values while Boolean union branches retain them. |
| Names/defaults/context | Required/unknown arguments fail; annotated `**kwargs` converts extras; omitted defaults stay omitted in hooks; wrong-typed defaults and unusable signatures fail preparation; caller `context` is rejected. |
| Annotation/schema | Postponed `int`, `bool`, `list[str]`, unions and resolvable forward references produce typed schemas; unresolved/unsupported input types fail clearly. Bundled `math_add.values` is an array of numbers. |
| Explicit schemas and bundled tools | Web search keeps its domain/backend constraints and descriptions; signature coercion still runs. Web/shell/filesystem numeric arguments and `math_add.values` still reject Boolean values before body entry. Plugin-owned domain validation and valid calls continue to work. |
| Preflight and wrappers | Loaded/function/filesystem tools use equivalent bound values in paths and execution; public `workspace`/`cwd` cannot bypass filesystem binding; raw records/summaries stay inspectable and input mappings are not mutated. Cover direct and executor calls. |
| Exec persistence | Part, Part[], Number, Boolean, Json strings, nested Parts and empty arrays survive execution, database reopen and event replay; primary/named invalid values leave no exec control. Compare run/async run/spawn to protect working routes. |
| Rerun conversion | Text `"7"` becomes Number `7` under the fresh signature, for primary and named inputs; the new control has native values and the source is unchanged. Cover both same and changed State revisions. |
| Rerun rejection | `"bad"` to Number, changed required/unknown arguments and incompatible typed arrays/structs fail before admission; no partial Run or source mutation occurs. |
| Restoration | Native Json strings, null named values, multimodal Parts, arrays, module-local structs and optional omissions remain intact under unchanged contracts; retry preserves its State and committed prefix. |
| HTTP and regression | Direct JSON, authored CLI/chat, scheduler, Flow calls/collections/reduce/repeat, outputs and await retain existing coercion/failure behavior. HTTP rerun failure remains 409. |

Use `tests/unit/base/test_function_tools.py`, `test_tool_protocol.py`, relevant
`tests/unit/plugin/` cases, `tests/integration/execution/test_shared_tool_execution.py`,
`test_agic_runtime_call_scenarios.py`, `test_flow_scenarios.py`, and
`tests/integration/api/test_remote_runs.py`. Keep tests offline and deterministic.
Default checks: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`, and `uv run pytest -n auto`.

## Compatibility, risks and approval

- Previously ignored unknown arguments and wrong-typed inputs/defaults may now
  fail. Plugin authors must remove undeclared keys, use real `**kwargs` when
  intended, provide resolvable supported annotations, and fix invalid defaults.
- Normalization before path preflight changes its input contract; wrapper tests
  and the hook migration note are required. Hand-written tools retain their
  existing behavior through the default hook.
- Stronger inferred schemas may change model-generated calls. Verify the bundled
  tools and existing provider schema preparation without adding live-provider
  tests to the default suite.
- No persistence format migration is required. Already-corrupt historical exec
  records are not repaired; affected work must be invoked again with valid input.

Open questions: none. Human approval was given after merging the definition;
independent review alone does not grant implementation approval.

## Independent review

An independent reviewer checked this plan against the code on 2026-10-07. One
P2 finding identified implicit Boolean-to-number conversion bypassing existing
plugin guards; the numeric policy and bundled-tool acceptance cases now address
it. Focused re-review found no remaining actionable or blocking findings.
