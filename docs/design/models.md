# Models

Toolang separates model knowledge, runtime readiness, and protocol execution.
The catalog describes what exists; adapters describe how to call one protocol;
the setup watcher captures source data and lazily resolves memoized model views.

## Core Terms

| Term | Meaning |
| --- | --- |
| `Provider` | One provider record in the flat catalog |
| `Model` | One model record linked by `provider` and an exact `ref` |
| `ModelProvider` | Typed per-model connection overrides, including optional `ProviderToolang` declarations |
| `ModelCatalog` | A plugin that returns an immutable provider/model snapshot |
| `ModelAdapter` | A plugin that invokes one wire protocol |
| `ModelRequest` | One run's concrete model demand |
| `ModelRoute` | The effective connection published at `Model._toolang.route` |
| `ModelCall` | One model call: content plus effective controls |
| `Model views` | All catalog records and the routable, allowed records, stored as ordered references |

There is no model-provider plugin layer. A provider does not execute calls, and
an adapter does not discover models, match providers, own prices, or determine
availability.

## Static Catalog

Toolang loads only the flat catalog format. The bundled data is the
[2026-10-18 release](https://github.com/openhat-ai/models/releases/download/2026-10-18/catalog.json)
(232 providers, 2,547 models):

```json
{
  "providers": [{"id": "openai", "name": "OpenAI", "env": ["OPENAI_API_KEY"], "npm": "@ai-sdk/openai"}],
  "models": [{"id": "gpt-5", "provider": "openai", "name": "GPT-5", "modalities": {"input": ["text"], "output": ["text"]}, "limit": {"context": 200000, "output": 8192}}]
}
```

Each provider appears once in `providers[]`; each model appears once in
`models[]` with a `provider` owner ID and no `ref`. Toolang composes
`<provider-id>/<model-id>` when a ref is needed. If a model needs a connection
override, its object belongs in `override`. Provider order and
model order are significant and are preserved. Root, agent-home, environment,
and CLI-selected catalog files must use this format. Toolang rejects nested
models.dev provider maps and combined/raw models.dev catalogs; an external
program must convert them before use.

Toolang selects the catalog file in this order:

1. command-level `--catalog PATH`, where supported;
2. `TOOLANG_MODEL_CATALOG`;
3. the active agent home `catalog.json`;
4. `AGENT_ROOT/catalog.json`;
5. the bundled flat catalog.

Within each root/home scope, `plugin.model_catalog.models_dev.path` takes
precedence over its implicit catalog. Roaming fetch stages the selected project
catalog at `AGENT_ROOT/catalog.json`.

A higher-priority file fully replaces lower-priority files. Toolang does not
merge multiple static files or download catalog data during startup. Explicit
CLI and environment paths are filename-agnostic. Implicit discovery recognizes
`catalog.json`; `models.json` has no special meaning. When no catalog is
selected, Toolang uses the bundled data.

When no agent is selected, inspection uses only the root source and root model
context; it does not read an implicit `agents/default`. In a Docker guest, an
external `--catalog` source is mounted read-only and
`TOOLANG_MODEL_CATALOG` is rewritten to its guest path.

Inspection uses the selected root or agent configuration and process environment,
not a running session or sandbox. It does not require the agent to be running.
[Queries](queries.md) owns filtering and public record projections.

The flat importer validates both arrays, unique provider/model identities,
provider references, and known field types. It drops unknown additive fields,
parses prices as finite floats, and treats a zero `limit` value as unknown by
omitting that key.

## Model Selection

Configure authorization and preference together, at root or agent scope:

```toml
[allow]
models = ["openai/*", "google/*", "*"]
# A collection query string is also accepted: "openai/*, google/*, *"
[model]
ref = "openai/gpt-5"
effort = "medium"
[compact.model]
ref = "openai/gpt-5"
effort = "low"
```

Without `allow.models`, all models retain their exact bundled/selected catalog
order. There is no implicit provider-priority sort. With `allow.models`, matching
records are ordered by query branch and then by their original catalog position;
unmatched records remain after matches in their original relative order. Allow
membership changes the model status and effective view, but does not delete
records from the all view. The effective view contains models that are both
routable and allowed. `providers` likewise retain catalog-file order.

Omit `[model]` to use the first model in the routable-and-allowed view.
Omit `compact.model` to follow the current thread model identity, without inheriting
its `effort` or `max_output`. The thread model is the current root Run's model
binding, including for nested Runs; a model-free root uses the calling child's
model as the reference. Session/request restrictions still apply. Both default
and explicit compact models must be ready, allowed, and support tool calls.
Invalid choices or parameters fail without silently selecting another model.

### Automatic compaction configuration

All `[compact]` fields are optional in root and agent `config.toml`:

```toml
[compact]
# model omitted: use the current thread model
summary = 4096
recent = "30%"
trigger = "80%"
```

- `summary`: soft target for summary length; default 4096 tokens.
- `recent`: soft target for retained historical Steps; default 30%. It excludes
  the summary, fixed instructions, and the current Run. Execution preserves
  complete Steps and paired tool calls/results within the retained view.
- `trigger`: complete-request input budget; default 80%. Preflight compacts when
  the estimated input exceeds the smaller of this value and the calling model's
  safe input allowance (input limit, output reservation, and estimation margin).

Each size accepts a positive integer token count or a percentage string in
`(0%, 100%]`, including decimals such as `"2.5%"`. All percentages refer to the
**current thread model's context window**, not the compact model or current
history size. For a 200,000-token window, `summary = "2%"`, `recent = 60000`, and
`trigger = "80%"` mean 4,000, 60,000, and 160,000 tokens respectively. Conversion
rounds down, with a minimum of one token. Recent must be less than trigger.

[Execution](execution.md#history-compaction) owns retained-history selection,
batching, durable coverage, and adoption of a successful summary. These settings
are budgets for that mechanism, not a guarantee that required input will fit.

Fields inherit independently: agent config, root config, built-in defaults.
Override only the model with `TOOLANG_COMPACT_MODEL='openai/gpt-5 effort=low'`
or `too alice start --compact-model 'openai/gpt-5 effort=low'`. Model precedence is
CLI, environment, agent config, root config, then the thread model.
`--compact-model` also applies to `serve` and `chat` when starting a runtime;
it cannot reconfigure an already running agent. Accepted Runs retain their
captured configuration.

## Catalog Plugins

Catalog plugins use the `toolang.model_catalog` entry-point group and return an
immutable provider/model snapshot. The built-in `models_dev` plugin reads the
flat cata format for its selected static file; it does not convert raw models.dev
provider maps or combined catalogs. External catalog plugins and the built-in
Ollama and llama.cpp plugins may still return `CatalogSnapshot` or
`ModelCatalogSnapshot` directly.

`CatalogSnapshot`, `CatalogProvider`, and `CatalogModel` are neutral plugin
declarations. `CatalogModel.provider_id` associates the model with its provider;
no `_toolang` data is required. Setup translates them into internal records.
Provider and model sequences preserve the order supplied by each source, and
merging rejects duplicate provider/model identities without sorting records.
Provider routes belong to the declaring catalog plugin; core provider override
tables are rejected.

A catalog plugin receives concrete configuration from its factory call. It
must not read global CLI state or install packages. Local catalog plugins probe
only their configured/default endpoint and use short timeouts. `SetupWatcher`
re-probes local catalogs as needed, but publishes a new setup revision only when
captured configuration, environment, catalog content, plugin inputs, or semantic
probe results change. Unchanged file timestamps and repeated identical probes
do not create a new revision. Model catalog data is never written to a persistent
`.setup/models` cache.

Each published `AgentSetup` pins one revision's configuration, environment, and
catalog source snapshots. Its synchronous accessors materialize resources on
first use and memoize them only for that setup instance:

- `setup.models()` and `setup.providers()` return all ordered model/provider
  records, including unready, allow-excluded, and empty-provider records.
- `setup.models_effective()` and `setup.providers_effective()` return routable,
  allowed records in the same relative order.
- Model status is one compact bit field with `ROUTABLE` and `ALLOWED` bits.
  Allow policy never deletes a model from the all view.
- All/effective model views are immutable tuples referencing the same model records;
  provider views preserve catalog-file order. No model collection query indexes
  are built during setup loading.
- `setup.tools()` returns the allow-filtered tools; `setup.tools(all=True)` is
  the pre-allow view used for inspection and internal algorithms.
- `setup.toolsets()`, `setup.adapters()`, and `setup.catalogs()` load their
  plugin families independently. Model route resolution uses the captured
  adapters and source snapshots without loading toolsets.

Inspection filters and runnable model directives use `tq-json` transiently on
these records. Setup stores model sequences without a collection wrapper or matcher cache.
A source or setup change causes the watcher to publish a new generation;
existing references keep their captured inputs and memoized views. The watcher
retains the last good setup when source validation or dynamic probes fail.

External catalog entry points are opt-in. Configure one by its entry-point name:

```toml
[plugin.model_catalog.company]
url = "https://catalog.example/models.json"
credential_env = "COMPANY_CATALOG_TOKEN"
```

The merged mapping is passed unchanged to the catalog factory; the plugin owns
resolution of `credential_env` when it needs the credential. Built-in
`models_dev`, `ollama`, and `llama_cpp` catalogs remain enabled.

## One-Time Route Resolution

When model data is first accessed, the setup resolver merges captured catalog
snapshots and enriches every provider with its default route and every model
with its effective route:

```text
ProviderToolang: { env: declared rule, adapter: declared adapter, route: ModelRoute }
ModelToolang:    { provider: string, status: ROUTABLE | ALLOWED, route: ModelRoute, local: bool }
ModelRoute:      { adapter: string?, api: string?, env: rule?, headers, options, api_env_missing: bool }
```

The `ROUTABLE` bit records route readiness; `ALLOWED` records policy membership.
The effective view is their intersection. Provider metadata retains trusted
catalog declarations plus its effective default route; each model carries its
own effective route. Model-level protocol and API overrides remain catalog
facts, without injected resolution fields. Adapters receive
`(model, request, *, environ)`.

`Provider.api` stays the raw catalog value. Setup resolves model/provider API
values, adapter defaults, and templates into `route.api`. Route environment
rules contain names only; actual values remain in `setup.envs`.
`local` preserves the declaring catalog's origin across merging. Missing API
template variables set `api_env_missing`, prevent readiness, and produce the
public `no_env` tag rather than `no_api`.

A missing/uninstalled adapter or invalid selected catalog mode makes a route
non-routable; an unresolved API or unmet environment requirements also prevent
routing. An empty env rule means no credential is required. Setup resolves each
field independently. Headers and options are recursively immutable; adapters
copy them into mutable provider request payloads.

A declared `provider.mode` must select an object in `experimental.modes`.
Missing or invalid selections make only that model non-routable, with empty
effective headers/options; other models still publish. The all view preserves
its source declarations, and a corrected catalog revision can restore
routability.

The resolver applies model-level connection overrides before provider defaults,
then the adapter's protocol default; resolves provider environment rules; and
checks installed-adapter and local-probe state. The resolved route environment
contains names only, never secret values.

For provider ID `vercel`, Gateway model routes default to
`https://ai-gateway.vercel.sh/coding-agent/v1` so Vercel can classify new
requests as coding-agent traffic. Explicit provider/model API routes still take
precedence; other provider IDs are unchanged. The Messages adapter appends
`/messages` to this base, producing `/coding-agent/v1/messages`.

## Adapter Plugins

Adapter plugins use the `toolang.model_adapter` entry-point group and implement
the [Model Adapter contract](plugins.md#model-adapter). Both asynchronous call
methods receive a resolved `Model`, a `ModelCall`, and keyword-only `environ`;
streaming additionally receives keyword-only `on_event`. The canonical protocol
is [base/protocols/model.py](../../src/toolang/base/protocols/model.py).

Built-in adapters are:

- `chat_completions`;
- `responses`;
- `messages`;
- `generate_content`.

Adapter factory configuration uses the same plugin grammar:

```toml
[plugin.model_adapter.responses]
```

Only this merged table is passed to the `responses` factory. The built-in
adapters currently define no authored plugin options; external adapters may
define their own non-sensitive values and secret-reference fields.

Adapters receive the effective connection, the resolved `Model`, and the
`ModelCall`. They translate
canonical messages and tools, normalize streaming, usage, cache, reasoning,
and audio meters, and preserve protocol state needed by later calls. For
example, the Generate Content adapter retains Gemini thought signatures on
their original Parts and restores them on compatible subsequent tool-call turns. The Messages
adapter likewise preserves signed Anthropic thinking and redacted-thinking
blocks and replays them before the associated tool use.

Model selection expressions accept the convenience controls `effort` and
`max_output`:

```text
effort     = auto | LEVEL | TOKENS
max_output = auto | TOKENS
```

These expressions resolve to `ModelRequest.reasoning` and `ModelRequest.max_output`.
A dispatched ModelCall instead carries `reasoning` and the resolved inclusive
`max_output_tokens`; it does not carry an `effort` convenience field.
Omitted controls inherit; `auto` cancels the inherited control. Automatic reasoning
sends no control. Explicit effort, `none`, and token budgets may be attempted even
when `reasoning_options` is missing. Enforce known constraints and exhaustive
enumerations; adapters reject controls they cannot encode. Never silently
downgrade a request after provider rejection.

Explicit `max_output` takes precedence over an authored provider output option
and is clamped to the catalog route's `limit.output` when known. Otherwise the
automatic allowance starts from that route output limit, or 32768 when it is
unknown, regardless of reasoning capability metadata or effort. A known joint
context caps this candidate at one quarter; explicit reasoning tokens `R` can
raise it again to `R+1024`. The known route output limit clamps the final allowance.
Output must be positive and exceed explicit reasoning tokens. Explicit output
controls bypass the automatic context fraction and reasoning headroom. Known
context/input limits still require room for input and the estimation margin;
explicit output or reasoning controls can leave no room and fail before dispatch.

These are host policy values, never inferred service defaults or catalog fields.
An independent input limit does not imply an output limit. With neither context
nor input capacity known, local input admission is unavailable. Unknown limits
can still lead to provider rejection; a larger allowance reduces truncation risk
but cannot guarantee a complete response.

### Input admission

Given the resolved inclusive output allowance `O`, known joint context `C`,
and independent input limit `I`, the available input is:

| Known capacity | Before estimation margin |
| --- | --- |
| Both `C` and `I` | `min(C - O, I)` |
| Only `C` | `C - O` |
| Only `I` | `I`; output does not consume this independent limit |
| Neither | No local input-capacity check |

Subtract `max(1024, ceil(K / 20))`, where `K` is the smaller known capacity
or the sole known capacity. A nonpositive result fails before dispatch.
For `C=32000`, `I=10000`, and `O=8000`, the margin is 1024 and the input budget
is 8976 tokens.

### Adapter controls and routing

Adapters can implement `ModelOutputOptions.output_allowance(options)` to normalize
authored output aliases before admission. They send the resolved allowance unchanged. Known context/input limits reserve output and an estimation
margin; existing compaction handles input overflow. Unknown context remains unknown.

Adapters translate `effort` and `budget_tokens` to their wire protocol and
reject unsupported or conflicting combinations. A provider-native reasoning
toggle is an adapter implementation detail, not a third canonical control:
`effort = none` becomes the adapter's disabled form. The Chat Completions adapter includes only small,
explicit dialect mappings for well-known compatible providers; unknown provider
extensions are not inferred.

An external adapter should contain no provider matching table. If a new npm
package needs to use it automatically, add that small mapping to the resolver;
a catalog can also declare the adapter explicitly through its provider or
model connection declarations. Core `[models.providers.*]` overrides remain
unsupported.

## Local Providers

Ollama and llama.cpp publish neutral catalog declarations for the configured
service route. Confirmed positive generation allowances become `limit.output`,
even when the API permits overrides. Unlimited or unknown values remain absent;
training context never substitutes for deployed context.

Ollama uses `/api/tags`, optionally `/api/show` and `/api/ps`: matching loaded
`context_length` takes precedence over configured `num_ctx`; positive configured
`num_predict` provides output allowance. llama.cpp uses `/v1/models` and optional
model-specific `/props` with `autoload=false`: context comes from serving `n_ctx`,
not `n_ctx_train`. Prediction settings are used only when established; the default
`n_predict=-1` placeholder in b10566 does not reveal global server configuration.

Optional detail failures retain list facts. Discovery never loads models or
changes settings. Local models retain zero API token prices; compute cost is
outside token accounting. Endpoint changes obtain fresh facts through existing
setup refresh; running calls keep their published snapshot.

Configure discovery independently from the resolved provider call route:

```toml
[plugin.model_catalog.ollama]
endpoint = "http://127.0.0.1:11434"
timeout = 2.0
# Optional headers authenticate discovery; they are never exported in catalog data.
# headers = { Authorization = "Bearer ..." }

[plugin.model_catalog.llama_cpp]
endpoint = "http://127.0.0.1:8080/v1"
```

When omitted, the built-ins use `OLLAMA_HOST`, `LLAMA_CPP_HOST`, and then their
loopback defaults. In a Toolang Docker guest, the defaults use
`TOOLANG_HOST_GATEWAY`; loopback values from those two environment variables are
rewritten to the gateway as well. An authored plugin `endpoint` is exact and is
never rewritten, so it can deliberately select a service running inside the
guest. Configure routes through the owning catalog plugin; core
`[models.providers.<name>]` overrides are not supported.

## Inspection and export

Model/provider inspection reads one Setup generation. The effective view contains
ready, allowed models and their providers. The diagnostic view includes unready
and excluded entries plus empty providers, without granting execution access.
Provider ready/total counts always cover all owned models.

Model records preserve catalog fields and add `ref`, tags, and safe route
metadata. Provider inspection reports its default route and stored counts, not
a nested model collection. Neither JSON projection is the flat catalog input
format. Inspection skips default/compact-model validation so broken choices
remain diagnosable.

[Queries](queries.md) owns record fields, tags, ordering, and query semantics;
[CLI](cli.md#resource-and-plugin-inspection) owns inventory commands. Installed
catalog/adapter discovery reads metadata without constructing Setup or factories.
Model-call controls are structured request fields, not query syntax.

## Runtime Configuration

Catalog plugins own provider routes. Root or agent configuration filters the
published models and selects an exact default:

```toml
[allow]
models = ["gateway/*"]

[model]
ref = "gateway/chat"
effort = "high"
```

`SetupWatcher` captures `allow.models` with each revision. With no allow query,
model order is exactly catalog order. With allow queries, matching records are
ordered by query branch and catalog position within each branch; unmatched
records remain at the end in their original relative order. All records remain
available for inspection, while `models_effective()` contains only records
marked both routable and allowed. Request and runnable policy can only narrow
that ready view. Query matching uses `tq-json` without precomputed model
collection indexes.
`[model]`, `[talk.model]`, and `[compact.model]` share three typed fields:
`ref` (an exact model or selection sentinel), `effort` (a level, a non-negative
integer reasoning budget, or `"auto"`), and `max_output` (a positive integer or
`"auto"`). Unknown fields and wrong types fail. Model capability checks remain
lazy and final handler/resource validation happens before Run admission.

Ordinary model fields apply in root/agent or project-layer order, followed by
existing environment and startup overrides. A parameter-only table requires an
inherited model. Supplying an exact `ref`, even unchanged, clears inherited
parameters before applying the same table's assignments; `"auto"` clears only
that control. `ref = "default"` retains the preceding ordinary source's value;
`ref = "unset"` clears its configured preference, leaving entry fallback rules
unchanged. Explicit `/model unset` selects a model-free Run.

Talk overlays its model tables on the resolved ordinary model or handler fallback.
Its `default` restores that baseline and its `unset` suppresses fallback. See
[driver policy](drivers.md#threads-and-observation). Compact overlays its own
model tables on the calling root's identity with independent effort/output
settings. Parameter-only compact tables customize that identity; `default`
restores it, while `unset` is invalid. Exact compact environment/startup choices
apply after its configuration layers.

String inputs use `[ref=]MODEL` plus `effort=VALUE` and `max_output=VALUE`.
The `ref=` prefix is optional, and a bare model must be first; named assignments
may appear in any order. Duplicate identities/parameters and empty values fail.
The same grammar is accepted by `TOOLANG_DEFAULT_MODEL`, `TOOLANG_COMPACT_MODEL`,
`--default model=BODY`, `--model BODY`, `--compact-model BODY`, `/model BODY`, and
`:model BODY`, retaining each surface's existing restrictions and lifetime.
Quote multi-token shell values, for example `--model 'ref=gateway/chat effort=high'`.

Only structured model tables are read from configuration files. Migrate
`[default].model` to `[model]` with `ref`, `effort`, and `max_output` fields,
and compact model strings to `[compact.model]`. The old `[default]` namespace
and scalar compact model settings have no effect. Model string inputs use
`unset` to clear a preference; `none` is not a model selection sentinel.
There is no arbitrary provider-parameter map.

`[models.providers.*]`, `[models].default`, and `[models.aliases.*]` are rejected.
Custom model identities and aliases will be supplied by a future custom catalog rather than
by a parallel runtime route mechanism.

## Runtime Calls and Accounting

`ModelCall` contains provider-neutral instructions, messages, tools, an optional
normalized JSON Schema in `output_schema`, and optional opaque
`continuation` data. Built-in adapters translate the schema and continuation to
their provider request fields. Native schema controls are used only when the
resolved model advertises structured-output support; other targets receive the
deterministic provider-wire schema directive. `ModelCallResult` contains the
assistant message, tool calls, normalized usage, and next continuation.
Canonical and durable JSON use the compact `cont` key. Streaming emits ordered
`ModelPartStart`, `ModelPartDelta`, and `ModelPartEnd` updates.

Returned reasoning is an assistant-only `ReasoningPart(text, signature=None)`.
The text is exactly what the provider returns, which may be a summary. Opaque
signatures or encrypted state are kept in `signature`; `provider` and JSON-only
`provider_metadata` identify the resolved provider, adapter, model, and native
block. Gemini signatures can also belong to `TextPart` or `ToolCallPart`, including
empty text. These fields belong to Parts, independently of tool calls. A token
count without returned reasoning does not create a Part.

All four built-in adapters preserve these Parts in Step output and selected
assistant history. Compatible provider/adapter/model history reconstructs native
reasoning from the Parts after reopening or forking a thread. A different scope
omits reasoning and native fields. Content transformations discard native fields.
Continuation holds call-level state such as a Responses cursor, not a second
copy of reasoning history. An interrupted native unit retains only its readable
prefix; completed units retain their native fields.

Raw Parts and machine inspection expose reasoning. Answer extraction, structured
output, child-run user context, and human progress/results exclude it. This does
not enable provider summaries automatically or add a reasoning display option.

The runtime records inclusive token totals plus cache read/write, visible,
reasoning, audio, and provider-specific meters. Each model step stores its model
ref, setup revision, and normalized call, including effective reasoning. The
revision is provenance only: no historical setup table or model replay feature
is provided.

Run and rerun model requests use flat `ref`, `reasoning`, and `max_output`
fields. Model Step reports contain `usage`, `cost`, `invoked`, and `cont`.
ModelCost stores the selected amount/currency/source/coverage plus pricing,
meters, and estimate lines; there is no `accounting` envelope or separate pair of
reported/estimated amount wrappers. Historical costs use recorded amounts,
not current catalog prices. [Execution](execution.md#terminal-usage-and-cost-records)
owns terminal subtree reports.

Cost selection is `reported`, `estimated`, `zero`, or `unknown`. A USD provider
report wins, including zero. A non-USD report is selected only when no catalog
estimate exists; an available estimate takes precedence over that report.
`zero` requires a complete estimate whose rate lines are explicitly free and no
provider report. A positive rate rounded to zero remains `estimated`. Partial
estimates retain `complete: false`; unknown costs are not free. USD summaries and
limits omit selected non-USD amounts.

Call totals settle to six fractional USD digits, rounding half up after all
components are calculated. Accumulation and budget comparison use integer
micro-USD units; amounts must be between zero and 999,999,999.999999 USD.
Accounting uses numeric fields; rates and intermediate lines are not rounded
before final settlement. The records change intentionally does not support old
formats. [Records](records.md) owns the persisted shapes, and
[accounting.py](../../src/toolang/execution/accounting.py) owns cost settlement.

## Model Response Recovery

Each Agic run allows at most two automatic attempts to recover response errors,
shared across all its model turns, including turns used for typed-output repair.
The separate output-contract repair does not replenish this allowance. Every
attempt also counts toward the run's model-call limit. Output and reasoning
budgets remain unchanged.

- Built-in adapters reject truncated responses, streams that end before a
  terminal event, malformed or non-object tool arguments, and missing
  function names. Empty argument strings mean `{}`. The runtime discards the
  failed turn's tool calls and requests a complete, concise replacement. It does
  not repair JSON or execute the valid siblings of a malformed call.
- Built-in adapters retry connection failures, timeouts, interrupted transport,
  and HTTP 408, 409, 429, 500, 502, 503, 504, or 529. Known transient error
  codes inside streamed responses use the same recovery policy. Explicit quota
  exhaustion and `x-should-retry: false` remain terminal. Network retries wait for
  the shared recovery attempt number in seconds (one or two), or longer if
  `Retry-After` requires it. A valid `retry-after-ms` takes precedence and is
  converted from milliseconds. The run time limit
  and cancellation remain effective while waiting. Steering preserves the
  remaining backoff deadline.
- Authentication, invalid requests, explicit refusal, provider rejection, and
  unclassified errors remain terminal. SDK-internal retries are disabled so attempts are visible and
  bounded by the runtime.

Each failed attempt is stored as a failed model step with available partial text
and reported usage, including text received in the final failing chunk. In Messages
streams, a known rejection or truncation takes precedence over a later stream
failure. Missing usage remains unknown. Recovery keeps the preceding valid message
history and continuation; it does not replay successful tools or
add incomplete tool calls to the next request. Retries can incur provider charges,
including when a disconnected request's usage is unavailable.

A complete terminal response remains usable if its optional stream tail is lost.
Observer failures are propagated separately and never classified as provider
transport errors. Native stream completion follows the
[Messages event lifecycle](https://platform.claude.com/docs/en/build-with-claude/streaming)
and [Generate Content finish reasons](https://ai.google.dev/api/generate-content#FinishReason).
