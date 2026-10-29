# Pluggable search adapters

Status: Proposed; awaiting confirmation of the complete definition.
Feature definition only; do not implement.

## Goal and scope

Keep one model-facing `web/search` tool. Like model adapters, independently
installed search backends implement a shared execution contract. Users can
install `tavily-toolang` and search without configuration, an account, or a key.
Multiple backends support rotation and fallback, while a call can select one
backend or a smaller pool. Paid providers are supported without making their
credentials a prerequisite for default search.

Deliver the host contract/router, the existing bundled DDGS adapter, and
separate Tavily and SerpAPI plugin distributions. These package names describe
proposed deliverables, not already implemented packages. Other providers below
are extension candidates, not additional implementations in this scope.

Exclude separate `tavily/search` or `serpapi/search` tools, a search catalog,
browser automation, extraction, MCP service repairs, merged search results,
persistent caches, distributed rotation state, and a pricing catalog.

## Architecture

```text
web/search(query, backends=...)
  WebToolset                  Schema, validation, deadlines, output
    SearchRouter              Selection, rotation, fallback
      SearchAdapter        Provider execution contract
        ddgs                  Bundled, last resort
        tavily                tavily-toolang distribution
        serpapi               serpapi-toolang distribution
        other installed plugins
```

`SearchAdapter.search()` corresponds to `ModelAdapter.invoke()`, not
`ModelCatalog.snapshot()`: it executes a request. Discovering installed entry
points and checking local requirements supplies the small inventory needed
here; do not introduce a second catalog protocol or reuse the model catalog.

The host owns the public schema, input validation, final URL/domain filtering,
result limit, router, deadlines, diagnostics, and `ToolResult` mapping. Backend
implementations own transport, provider-native parameters, parsing, and expected
error translation. They may use REST, MCP, or a provider's native model search;
transport is not part of the host contract.

The `web` factory composes concrete backend instances and a router. Each backend
factory receives only its own fresh configuration mapping, resolves endpoint
defaults and credential references from the prepared workload environment, and
constructs an adapter with concrete values. Invocation does not read environment
variables or discover packages. Factories and local availability checks make no
network requests.

## Installation and configuration

Use `toolang.search_adapter` entry points and the existing factory pattern.
For example, the Tavily distribution registers:

```toml
[project.entry-points."toolang.search_adapter"]
tavily = "tavily_toolang:create_search_adapter"
```

`create_search_adapter(config)` accepts an empty mapping. Installed, locally available
backends participate automatically; no activation list is required. A key-only
backend without credentials reports unavailable and does not break other
backends. Installation must be into the environment running Toolang.

Keep backend configuration inside the web plugin, without a new global
`PLUGIN_FAMILIES` member:

```toml
[plugin.toolset.web]
# Optional restriction; omit to admit all installed backends.
search_backends = ["tavily", "serpapi"]
top_k = 5
timeout = 15

[plugin.toolset.web.backends.serpapi]
api_key_env = "SERPAPI_API_KEY"
```

Root/agent layers use the existing nested-table merge contract. A configured
`search_backends` list is the allowed pool, not merely a preference. Unknown
names, duplicate names, empty lists, ambiguous entry-point registrations,
invalid contracts, or mismatched instance names fail before network access.
Unadmitted plugins are not imported or instantiated. Installation order never
chooses a winner. Include backend entry-point provenance in setup revisions.

Each plugin owns its credential conventions. Supplying the expected environment
variable to the selected workload is enough for a key-only plugin; no TOML is
required. Credential-ready
paid plugins can enter the automatic pool. Missing paid credentials do not block
keyless plugins. Do not obtain accounts/keys automatically or put secret values
in configuration, diagnostics, or logs.

## Credential readiness and request outcomes

Replace a boolean `is_available()` with a structured, generation-local
`SearchReadiness` snapshot. Its fields are `state` (`ready`,
`missing_credentials`, or `missing_configuration`), `auth_mode` (`none`,
`keyless`, or `api_key`), `credential_present`, `missing_env` (names only),
and an optional safe `reason`. `ready` means locally eligible to attempt a
request; it does not certify the credential or remote service.

The adapter factory checks its configured/default credential variable for a
nonblank value in the [selected workload environment](../design/setup.md#environments),
then captures the snapshot and concrete authentication values. Core does not
scan arbitrary `*_API_KEY` names. Plugins do not independently read dotenv files,
keychains, or another process's environment.

| Local inputs | Readiness |
| --- | --- |
| Tavily auto, no key | ready, keyless, credential_present=false |
| Tavily auto, nonblank key | ready, api_key, credential_present=true |
| SerpAPI, absent/blank key | missing_credentials; missing_env=[SERPAPI_API_KEY] |
| Instance-based adapter, no instance URL | missing_configuration |

Auto selection skips locally unready entries and records the safe reason.
Explicitly pinning one returns that reason without network access. Invalid
configuration types/values and broken plugin contracts remain configuration
errors rather than ordinary unready states.

Setup, listing, and selection never probe authentication, quota, or remote health.
The actual search supplies evidence. Classify its documented response, not just
HTTP status: 403 can be an IP/access gate; 429 can be quota exhaustion. Record
the outcome in `attempts` and apply the defined routing policy. Do not silently
replace an invalid key with anonymous access within an adapter.

Tool parameter descriptions summarize local readiness/auth mode and safe missing
requirements so the model can choose; structured attempts report call outcomes.
No additional generic tool-readiness framework or remote health cache is added.

The caller's shell and the agent may see different variables. Current agent
fallback picks at most one credential; adding search names to that list would
not supply both model and search keys. Keyed users must admit variables through
the existing environment mechanism, normally names in the selected agent file.
Preserve that policy and its startup snapshot: restart to apply key changes.
Keyless paths require no environment edits.

## Model-facing selection and routing

Add optional `backends: list[str] | None = None` to the existing search inputs.
Publish installed/admitted backend names in the item enum; requirements belong
in descriptions. An unknown or disallowed name is an input error, not an
invitation to silently choose a different provider.

```text
web/search(query="...")                                  # Automatic pool
web/search(query="...", backends=["tavily"])              # Exactly Tavily
web/search(query="...", backends=["tavily", "serpapi"])   # Restricted pool
```

Resolve the effective pool by using the call list when supplied, otherwise the
configured pool, otherwise all installed backends. Call selection must be a
subset of the configured allowed pool. Omitted/null selects the default pool;
an empty array is invalid. These are Toolang backend names: DDGS's internal
Google/Brave engine names remain a separate, adapter-owned concern.

- One selected backend is pinned: no rotation or cross-provider fallback,
  including on empty results or errors. An unavailable selection returns an
  actionable failure before contacting another provider.
- A larger pool rotates starting providers round-robin. Explicit lists preserve
  their order; the discovered default pool uses sorted entry-point names.
  Locally unavailable entries are skipped and recorded.
- DDGS is reserved as the final fallback when present in a multi-backend pool.
  It never joins primary rotation and is never appended to an explicit pool
  that excludes it. A DDGS-only pool runs it directly.
- On expected provider failure, per-provider timeout, empty response, or complete
  removal by final URL/domain filtering, continue around the remaining primary
  pool, then DDGS. Invoke each provider at most once per call. Stop at the first
  usable results, even if fewer than `top_k`; do not merge or search in parallel.
- Rotation state is local to the toolset instance/setup generation and keyed by
  the effective ordered pool. Reserve starting turns atomically before awaiting;
  cancellation consumes a reserved turn. Recreating the instance resets state.
  There is no global fairness, persistent health probe, or circuit breaker.

Keep `top_k=5` and the 15-second total deadline. Before each invocation, divide
remaining time equally among providers still to attempt; unused time carries
forward. The last provider gets the remainder and a single provider gets the
full budget. Enforce both each grant and the total deadline. Cancellation and
unexpected programming errors propagate without fallback.

The DDGS adapter retains its engine chain, Yahoo compatibility, five-second
engine attempt caps, explicit-expression behavior, and cancellable process
workers from [the existing plan](web-search-backend-configuration.md).

## Backend contract and output

Expose `SearchAdapter` in `base/protocols/search.py` with `name`,
`readiness: SearchReadiness`, `preferred_backends: tuple[str, ...]`, and
`async search(request, *, on_attempt) -> SearchResponse`. Use immutable records
in `base/types/search.py`:

- Request: validated query, limit, normalized domains, optional adapter-local
  preference, and granted timeout.
- Result: title, HTTP(S) URL, and snippet; text fields may be null.
- Response: ordered results and selected internal backend/engine.
- Attempt: provider, backend/engine, status, elapsed time, optional count, safe
  error, error kind, and HTTP status only when actually observed.

`on_attempt` records completed attempts synchronously so completed diagnostics
survive cancellation. DDGS reports its current attempt in cancellation cleanup.
Supply a provider-level error/timeout attempt if an adapter reports none.
`SearchAdapterError` in `base/errors.py` classifies expected failures as
`auth`, `quota`, `rate_limited`, `timeout`, or `unavailable`.

Preserve query/domains/results/status/backend/attempts in output and add the
serving `provider`. DDGS's `backend` remains its actual engine. Each attempt
identifies its provider, including unavailable skips. Failure keeps structured
diagnostics and `ToolResult.error`; with no usable winner, provider/backend are
null. If all contacted providers complete without operational failures, report
`empty` or `filtered_out` with no tool error. Otherwise exhaustion is
`unavailable`; total deadline exhaustion is `timeout`. DDGS scraper emptiness
remains unavailable rather than authoritative empty.

Retain `preferred_backend` for compatibility: expose the union of admitted
adapters' curated choices, pass the preference only to matching adapters, and
pass null to others. This hint never changes the effective provider pool or
rotation. Descriptions must state that it applies only if that adapter is used.

## Initial adapters and compatibility

Keep DDGS bundled; it does not support Tavily in its locked 9.16.0 engine
registry or the [current upstream engine list](https://github.com/deedy5/ddgs#engines).
No core dependency on an external plugin is added. Keep Tavily and SerpAPI as
independently buildable projects under `packages/tavily-toolang/` and
`packages/serpapi-toolang/`, each depending on compatible Toolang contracts.
Do not introduce a root package workspace.

Tavily supports `auth=auto|keyless|api_key` (default auto), `api_key_env`
(default TAVILY_API_KEY), and `api_url` (default https://api.tavily.com).
Auto uses a present key, otherwise the documented keyless header. An invalid
key never downgrades within the adapter; a missing required key means locally
unavailable. Use basic search, native domain filtering, and disable answers,
raw page content, and automatic parameter selection. Respect provider limits.

SerpAPI initially supports Google organic web results only. Use
SERPAPI_API_KEY by default, allow an `api_key_env` override, and normalize
`organic_results` title/link/snippet. Use native query operators for domains
and retain final hostname filtering. Missing credentials mean unavailable.
Sanitize request URLs in errors because SerpAPI credentials are query parameters.

Preserve legacy top-level `[plugin.toolset.web].backend` as a DDGS-only pin:
when no new configured pool exists, its effective allowed pool is exactly DDGS.
Reject it combined with a call pool or configured pool other than exactly DDGS;
when both select exactly DDGS, apply the legacy engine expression. Reject
simultaneous legacy and nested DDGS engine expressions. Nested DDGS configuration
keeps its `backend` expression and existing preference restrictions.

## Provider candidates

These are independent adapter opportunities, verified against official docs;
free credits do not imply keyless access.

| Provider | Access and fit |
| --- | --- |
| [Tavily](https://docs.tavily.com/documentation/keyless) | Keyless REST and keyed access; initial agent-oriented search plugin |
| [SerpAPI](https://serpapi.com/search-api) | API key required for general searches; initial structured Google SERP plugin |
| [Exa](https://exa.ai/docs/get-started/exa-mcp) | Keyless MCP; regular API uses a key; semantic/content search |
| [Parallel](https://docs.parallel.ai/integrations/mcp/quickstart) | Keyless MCP; regular API uses a key; search excerpts and research |
| [Firecrawl](https://docs.firecrawl.dev/features/search) | Keyless/keyed REST; search and extraction; local keyless smoke was IP-blocked |
| [Serper](https://serper.dev/) | Keyed Google SERP API; unauthenticated local probe returned 403 |
| [Brave](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) | Keyed search API; independent search index |
| [xAI](https://docs.x.ai/developers/tools/web-search) | Keyed model-native search; tokens plus search-tool usage are billed |
| [SearXNG](https://docs.searxng.org/dev/search_api.html) | Instance URL and enabled JSON API required; self-hosted metasearch |

A model-native backend such as xAI implements the same search contract. It owns
model selection, credentials, native tool invocation, and citation normalization.
Return actual structured source references; generated answers or invented URLs
must not be presented as extracted page snippets. It must not recursively call
Toolang's `web/search`. Provider-specific usage can be documented without
turning the registry into a model or pricing catalog.

## Acceptance and touchpoints

- `base/protocols/search.py`, `base/types/search.py`, `base/errors.py`: shared contract.
- `plugin/web/` and `plugin/toolsets/web.py`: factories, discovery, selection,
  routing, DDGS implementation, and stable host interface.
- Core `pyproject.toml`: bundled DDGS entry point; keep dependencies unchanged.
  Plugin package pyprojects/source/tests: Tavily and SerpAPI distributions.
- `setup/tools.py`, `setup/watcher.py`: backend provenance in setup revisions.
- Verify built-wheel installation exposes plugins with no activation config;
  Tavily works without credentials and absent SerpAPI credentials do not block it.
- Offline tests cover default/explicit/subset/single pools, invalid/disallowed
  choices, collisions, local availability, concurrent rotation, fallback,
  DDGS-only and DDGS-last behavior, deadlines/cancellation, normalized outputs,
  config isolation, legacy compatibility, auth mapping, and secret-safe errors.
  Readiness checks cover absent/blank/present keys, keyless selection, environment
  admission, captured inputs, no network probes, and distinction between a ready
  credential and real auth/quota/network failures.
  A configured single-provider pool must leave unrelated factories untouched;
  a per-call pin must contact only its selected service.
- Preserve DDGS regressions; add deterministic HTTP fixtures. Run repository
  code checks before implementation commits. Keep live smoke checks separate
  from the default offline suite and report relevance/availability limitations.
- After implementation update `docs/design/tools.md`, `docs/design/plugins.md`,
  and user-visible changelog coverage through the required workflow.

## Risks and approval

Keyless services still impose network/IP and rate limits. Installing a plugin
changes the default pool; supplying credentials may enable billable calls.
Configured allowed pools and per-call subsets make selection explicit.
Publishing the backend contract creates a Python API compatibility obligation.

No technical questions remain open in this proposal. Confirm the complete
definition before implementation; no implementation code is included here.
