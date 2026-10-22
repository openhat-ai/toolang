# Plugins

Toolang uses small entry-point contracts for runtime integrations. Shared
protocols and canonical value types live in `toolang.base`. Plugins should
raise `toolang.base.errors.ToolangError` for user-facing configuration, input,
or runtime failures.

## Plugin Families

The public plugin families are:

- `toolset`;
- `sandbox`;
- `model_catalog`;
- `model_adapter`.

Only these configuration families are consumed; unknown families are ignored.

Toolang core owns runtime lifecycle, scheduling, durable records, trace events,
HTTP APIs, and response projection. Plugins own only their family-specific
integration behavior and do not mutate durable runtime truth directly.

## Family Roles

### Toolset

`Toolset.tools()` returns a stable mapping of leaf names to `Tool` instances.
Subclass `Tool` from `toolang.base.protocols.tool`; value types live in
`toolang.base.types.tool`.

```python
class Tool:
    name: str
    def definition(self) -> ToolDefinition: ...
    def bind_arguments(self, arguments) -> dict[str, Any]: ...
    async def invoke(self, arguments, context) -> ToolResult: ...
    def summary(self, arguments, result=None) -> str | None: ...
    def paths(self, arguments, context) -> Mapping[str, tuple[str, ...]] | None: ...
```

`ToolResult(output={}, error=None)` has one extensible field: `output`, a JSON
object. A non-null `error` reports failure and may accompany partial output.
Raised exceptions become tool errors; cancellation remains executor-owned.
Call IDs, timing, and execution records are not plugin result fields.

`bind_arguments` synchronously returns a new mapping of supplied values. Its
default implementation copies the mapping, leaving argument semantics to the
plugin. Overrides must be pure and idempotent. The executor binds before path
preflight and passes the bound values to `paths` and `invoke`; a binding error
produces a failed Tool Step without calling either. Call records and `summary`
retain the raw input for diagnostics.

The two optional methods default to `None`:

- `summary`: plain call wording from arguments/result only, without I/O,
  styling, markers, or timing. No result means running; `result.error`
  distinguishes failure from success. This is separate from the static
  `ToolDefinition.description`. Executor masks sensitive arguments, isolates
  inputs, falls back on missing/None/blank/nontext/throwing summaries, and saves the wording.
  Fallback retains the first supplied argument in schema order or input order when
  no schema key matches; previews are masked and bounded to 80 characters.
  Executor prefixes running wording with `Canceled:` on cancellation, falling
  back to its generic cancellation wording when no summary is available.
  Progress reads saved summaries,
  never plugins, and displays a separate textual `[toolset]` prefix with no result
  blocks. Summaries need only describe the action and target.
- `paths`: workspace names mapped to tuples of normalized root-relative
  paths, for example `{"repo": ("/src/main.py",)}`. None means unsupported;
  `{}` means this call has no workspace paths. It must not execute the operation
  or mutate arguments. Runtime owns rules discovery, recall, and retry; actual
  path authorization still belongs to the operation. Base path helpers reuse
  invocation-local resolutions so preflight and execution address the same
  targets. This is not a sandbox for arbitrary shell commands.

`ToolContext` exposes `home`, `room` (the plugin's private storage directory),
and an immutable workspace map. It belongs to one invocation, not a whole Run;
subsequent calls receive newly captured grants. Do not retain it on shared tools.
Only the corresponding built-in tools receive specialized contexts: runtime
operations, history access, effective service credentials, or agent management.
Ordinary tools receive none of those dependencies.

The used function adapter remains available: annotate a sync/async function with
`@tool(summary=..., paths=...)`, then `create_function_tool(function)`.
At creation, the adapter resolves input annotations and prepares Pydantic v2
adapters for both binding and inferred JSON schemas. Supported annotations are
`str`, `int`, `float`, `bool`, `None`, `list[T]`, `dict[str, T]`, unions/optionals,
`Literal`, and Pydantic strict types, recursively. Missing annotations and `Any`
preserve supplied native values. Unresolved or unsupported input annotations,
positional-only parameters, `*args`, and invalid defaults fail preparation.

Binding accepts Pydantic's lax conversions, such as `"7"` to `int` and `"false"`
to `bool`. Numeric branches reject Booleans and non-finite values; strict types
remain strict. Strings are not parsed as JSON collections or obtained by a
blanket `str(value)` conversion. Required arguments must be supplied. Unknown
names fail unless the function declares `**kwargs`, whose annotation binds each
extra value. Omitted defaults remain omitted in hooks and are applied by Python
at invocation. An explicit `context` parameter receives the runtime context;
callers cannot supply it, even through `**kwargs`.

Hooks have exactly the Tool signatures. `summary` sees raw supplied arguments;
`paths` sees bound supplied arguments, including during direct calls outside
the executor. Explicit `@tool(parameters=...)` schemas
are preserved, including domain constraints, but do not replace Python binding
or add JSON-Schema validation; domain checks still belong to the plugin. Return
`ToolResult` directly, or let the adapter wrap a dict, None (empty output), or a
JSON value (`{"value": value}`). Return annotations do not add output validation.
The unused Typer adapter is not provided.

The history toolset receives `HistoryToolContext.history`, a read-only `ToolHistory`
interface bound by the executor to the current agent Store and caller Thread.
Its plugin depends only on base contracts, never the Store or executor. Each
read uses a short-lived read-only Store connection in a worker thread, so it
cannot hold the executor's Store lock. Interruption discards the late result;
the worker closes its connection when the read finishes or fails.

### Sandbox

Sandbox plugins provide runtime execution environments.

`SandboxRef.runtime_id` is the immutable identifier used for lifecycle control.
Plugins also return a structured `runtime_kind` and optional `runtime_name` for
human inspection; they do not preformat CLI labels. The built-in host sandbox
uses `process`, Docker uses `container`, and the default is `workload`.

Core persists the reference before calling
`attach(plan, ref, progress=...)`. `attach` may start process-local observers
and emit `ProgressEvent` updates within the closed `runtime.create` and
`runtime.start` stages, but progress is presentation-only and must not decide
readiness or lifecycle state. Callbacks are never stored in a plan, request,
reference, or persisted state.

`attach` does not forward raw foreground output to the controller terminal.
For an inherited-output plan, `wait(ref)` begins forwarding only after core has
reported readiness and handed the terminal to foreground logs. Cancellation
stops forwarding before runtime cleanup progress begins.

Progress labels are complete, short, verb-first sentences. Running labels end
in `...`; checkpoints and terminal outcomes use simple past without terminal
punctuation; failures use `Failed to VERB`. Labels do not contain agent names,
commands, environment values, secrets, or raw logs. Useful bounded context is
part of the sentence when safe, while `detail` is reserved for diagnostics.

### Model Catalog

Model catalog plugins return immutable `CatalogSnapshot` declarations containing
`CatalogProvider` and `CatalogModel` values. Neutral providers declare `adapter`,
`api`, and environment requirement names; Setup translates them into resolved
runtime `Provider`/`Model` records. The protocol also accepts internal
`ModelCatalogSnapshot` values. Neither form lets the catalog execute a model call.

The bundled and user-selected static file uses the flat
`{providers: [...], models: [...]}` format. Convert upstream models.dev data before
selection. Its importer retains npm provenance for resolver mapping; neutral
catalogs can name an adapter directly. Catalog plugins do not install packages
named by metadata. [Models](models.md#catalog-plugins) owns selection and merging.

### Model Adapter

Model adapter plugins execute one turn for a setup-resolved `Model` and a
`ModelCall`, returning `ModelCallResult`. Connection facts come from
`model._toolang.route`; ownership comes from `model.provider`.
`invoke(model, request, *, environ)` and
`stream(model, request, *, environ, on_event)` receive only declared environment
values from the run-pinned setup. Both calls are asynchronous, and streaming
adapters await their model-part handler. External adapters must adopt these
signatures; there is no separate route argument.

Every `ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` requires `part: int`.
Assign zero-based ordinals on first observation, for every Part kind, and return
the final assistant message in that order. Each Part begins and ends once; its
end payload must equal the final Part. Text and `ReasoningDelta` fragments must
concatenate to their Part's text. Reconcile final snapshots by emitting only an
unobserved suffix; reject contradictions. Final-only Parts need no synthetic
deltas. `ReasoningDelta` carries text only: assemble signature fragments inside
the adapter and attach native fields to the completed Part.

Reasoning uses `ReasoningPart`, not `ToolCallPart.reasoning`. Native signatures
and minimal JSON metadata belong to their original Part. Nonempty native fields
require `provider` and metadata keys `adapter` and `model`. Encode them only for
matching assistant history. Before propagating graceful failure/cancellation,
flush buffered readable reasoning and clear native fields on incomplete units.
Call-level continuation must not accumulate per-Part reasoning or signatures.

Execution-store compatibility follows [Durable Truth](execution.md#durable-truth).
External adapters must implement the indexed stream contract; incompatible
writable stores are rejected without automatic migration or reset.

Adapters own one protocol shape and its optional default endpoint. They do not
discover models, match providers, calculate availability, or own pricing.

## Loading

Built-in and external plugins use the same Python entry-point groups:
`toolang.toolset`, `toolang.sandbox`, `toolang.model_catalog`, and
`toolang.model_adapter`. Each entry names a family factory; a distribution may
register multiple entries. Loaders depend on base contracts and select concrete
implementations through these factories.

Installed-plugin inspection reads entry-point and distribution metadata without
calling factories or loading agent configuration. Resource inspection instead
uses the selected Setup/State and allow policy. [CLI](cli.md#resource-and-plugin-inspection)
and [Queries](queries.md) own these views.

Toolset and leaf identities start with an ASCII letter and contain only ASCII
letters and underscores; `__` is reserved. A plugin may return a bare leaf key
or `<toolset>/<leaf>`. Queries use `<toolset>/<leaf>`, model APIs use
`<toolset>__<leaf>`, and human labels use `<toolset>.<leaf>`.

A toolset with exactly one leading underscore is internal. Only entry points
whose installed distribution metadata identifies `toolang` may register one;
a Python module name grants no authority. External plugins cannot claim
internal toolsets or the reserved `runtime` plugin/toolset name. Runtime-owned
toolsets stay with execution because they depend on its services.

## Configuration Rule

CLI and setup call sites resolve paths, endpoints, and configuration layers
before constructing plugins. Core modules receive concrete plugin instances or
configuration values; plugins do not read CLI state implicitly.

Every plugin uses the same canonical TOML shape:

```toml
[plugin.toolset.fs]
max_chars = 20000

[plugin.sandbox.docker]
root = "/root/.toolang"
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'

[plugin.model_catalog.ollama]
timeout = 3

[plugin.model_adapter.responses]
```

The grammar is `[plugin.<family>.<entry-point-name>]`. Root configuration is
merged with agent configuration, with the agent winning at each key and nested
tables preserved. Plugin configuration is passed through unchanged: the
configuration layer never dereferences environment-variable names or resolves
secrets. A concrete implementation owns those semantics at its runtime
boundary.

Each factory receives a fresh mapping whose authored values come only from its
own merged table. Core may add concrete runtime inputs owned by that plugin,
such as the selected static catalog path or process environment, but it does
not expose peer plugins, other families, the full `[plugin]` table, or other
core configuration. Installed collection plugins receive an empty mapping when
they have no table. Removed shapes such as `[tools]`, `[sandbox.config]`, and
`[models.catalogs]` are invalid; there is no legacy fallback or merge path.

Core selection remains separate from plugin-owned configuration. For example,
`[sandbox]` selects `driver` and optional `target`, while
`[plugin.sandbox.<driver>]` configures the selected implementation. Sandbox
lifecycle recovery re-reads the current root and agent plugin tables instead
of persisting plugin configuration or secrets in runtime state.
Docker's `root` maps the agent root inside the guest; it does not change the
selected `TOOLANG_ROOT` or mount its discovery directory.

Authored plugin configuration is intended for non-sensitive values and secret
references such as environment-variable names, never secret values. Concrete
implementations must validate their own sensitive fields because a generic
mapping loader cannot infer whether an arbitrary string is sensitive.
Sandbox-specific dotenv materialization is runtime transport and does not
change or resolve a plugin's configuration mapping.

The Docker sandbox exposes all names explicitly authored in `PLACE/.env` or
`HOME/.env`. Host-process-only names must match `environment_allow_pattern`, which is
a configurable full-match regular expression with a built-in allowlist for
Toolang, bootstrap, proxy, certificate, and common model-provider variables.
The concrete plugin still resolves any exposed secret-reference name from its
guest process environment.

Only configured external model catalogs are instantiated; the three built-in
catalogs are always loaded. After snapshots are merged, the resolver maps raw
npm metadata to installed adapters, resolves provider and model routes,
interprets environment availability, and stores only non-secret runtime facts.
See [models.md](models.md) for the complete boundary.
