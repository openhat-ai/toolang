# Pluggable web search backends

Status: Proposed; awaiting human confirmation. Feature definition only.

## Goal and scope

Keep `web/search` as the stable public search tool while allowing built-in and
third-party search backends. Ship DDGS and Tavily adapters, including Tavily
keyless and API-key access. A third-party package must be usable through
installation and configuration without changing Toolang or replacing `web`.

This proposal supersedes the provider-selection and default-backend decisions
in [the earlier search plan](web-search-backend-configuration.md). Preserve its
DDGS behavior when DDGS is selected. Browser automation, MCP header repairs,
cross-provider fallback, persistent caching, and `web/extract` implementation
are outside this scope.

## Verified constraints

- Toolang locks DDGS 9.16.0. Neither that distribution's engine registry nor
  the [current upstream engine list](https://github.com/deedy5/ddgs#engines)
  includes Tavily. DDGS is one backend implementation, not the extension host.
- Toolset loading rejects duplicate effective names. Registering another `web`
  does not override the built-in toolset; keep that rule.
- [Plugin configuration](../design/plugins.md#configuration-rule) gives each
  factory its own merged table. Nested backend tables can remain owned by
  `web`, without adding a global plugin configuration family.
- [Tavily keyless access](https://docs.tavily.com/documentation/keyless) uses
  `X-Tavily-Access-Mode: keyless`; API-key access uses a Bearer token. Both
  return the same search schema. A local four-query smoke sample succeeded
  through Tavily in all four cases and through current DDGS defaults in two;
  this is connectivity evidence, not an availability or relevance guarantee.

## Extension boundary

```text
web/search                         Stable model-facing interface
  WebToolset                       Validation, deadlines, output, summaries
    WebSearchBackend               Typed asynchronous search contract
      ddgs                         DDGS engines and existing fallback
      tavily                       Direct Tavily Search API
      acme                         Installed third-party implementation
```

`web` owns the tool schema, query/domain validation, final URL/domain filtering,
result limits, total deadline, diagnostics envelope, and `ToolResult` mapping.
It receives a concrete backend instance from its factory and does not read
environment variables or discover plugins during invocation.

Each backend owns request construction, native domain filters or `site:`
syntax, transport, response normalization, and provider-specific error mapping.
The DDGS adapter owns its engine order, Yahoo compatibility, five-second attempt
budgets, and cancellable process workers. Tavily uses asynchronous HTTP and
closes request resources on completion or cancellation.

The `web` factory is the composition boundary: resolve its merged configuration,
select one entry point, and invoke that backend's factory with a fresh copy of
only its nested table. Backend factories resolve their own endpoint defaults
and secret references from the already-prepared workload environment, then
construct adapters with concrete values. Factory loading makes no network
requests. Unselected backend modules and factories are not loaded.

## Discovery and configuration

Use the Python entry-point group `toolang.web_search_backend`. Both bundled
backends and user packages register a `create_backend(config)` factory:

```toml
[project.entry-points."toolang.web_search_backend"]
acme = "acme_search:create_backend"
```

Discovery belongs to the web plugin and follows the existing entry-point factory
pattern. Do not extend `PLUGIN_FAMILIES` or pass all plugin configuration into
`web`. Exactly one registration must match the selected name; missing or
duplicate registrations, an invalid contract, or a mismatched instance name
produce an actionable configuration error. Never choose by installation order
or silently substitute another backend. Existing toolset collision rules stay.

Example Toolang configuration:

```toml
[plugin.toolset.web]
search_backend = "tavily"
top_k = 5
timeout = 15

[plugin.toolset.web.backends.tavily]
auth = "auto"
api_key_env = "TAVILY_API_KEY"
```

To use a user package, set `search_backend = "acme"` and put its configuration
under `[plugin.toolset.web.backends.acme]`. The backend owns that table's keys.
Root/agent merging follows the existing nested-table contract. Include backend
entry-point provenance in tool-setup revisions alongside toolset provenance;
backend installation/version changes must not reuse an old tool generation.

Tavily-specific fields are `auth` (`auto`, `keyless`, or `api_key`, default
`auto`), `api_key_env` (default `TAVILY_API_KEY`), and `api_url` (default
`https://api.tavily.com`). `auto` uses a nonempty key when present and keyless
access otherwise; `keyless` always uses keyless access; `api_key` requires the
named environment variable. A rejected key never downgrades to keyless. Reject
literal API-key configuration. Do not expose credentials in diagnostics.

Tavily sends basic search requests with generated answers, raw page content,
and automatic parameter selection disabled. Use native `include_domains`,
request at most the provider's supported result limit, and apply the common
domain filter afterward. Fewer results than `top_k` remain valid.

## Contract and results

Expose `WebSearchBackend` in `base/protocols/web.py` and normalized immutable
records in `base/types/web.py`. Keep provider implementations in `plugin/web/`.

The protocol has `name`, `preferred_backends: tuple[str, ...]`, and
`async search(request, *, on_attempt) -> WebSearchResponse`. `on_attempt` is a
synchronous callback receiving each completed `WebSearchAttempt`; the host keeps
these records even when its total deadline cancels the backend. The DDGS adapter
reports its current attempt during cancellation cleanup. If no attempt was
reported, the host supplies a provider-level failure/timeout attempt.

- Request: validated `query`, `top_k`, normalized `domains`, optional
  `preferred_backend`, and remaining `timeout` in seconds.
- Result: `title`, HTTP(S) `url`, and `snippet`; text fields may be null.
- Response: ordered results and selected backend/engine.
- Attempt: backend/engine, status, elapsed milliseconds, optional result count,
  safe error text, error kind, and HTTP status when actually observed.
- `WebSearchBackendError`, defined in `base/errors.py`, carries safe diagnostics
  and a kind of `auth`, `quota`, `rate_limited`, `timeout`, or `unavailable`.
  Adapters translate expected transport/provider failures into this exception;
  cancellation and programming errors propagate.

The host preserves `query`, `domains`, `results`, `status`, `backend`, and
`attempts`, and adds `provider` for the selected entry-point name. For DDGS,
`provider` is `ddgs` and `backend` remains the successful engine; for Tavily,
both are `tavily`. Existing attempt fields retain their meanings. Failures
continue to return both structured diagnostics and `ToolResult.error`.

An explicit successful API response with no results returns `status="empty"`
and no tool error. When all returned URLs are removed locally, return
`status="filtered_out"` and no tool error. DDGS exhaustion keeps its current
unavailable/error behavior because scraper emptiness cannot establish a true
zero-result search. Do not infer HTTP status from DDGS's no-results exception.

Retain the public `preferred_backend` argument. Its choices come from the
selected instance: DDGS exposes its current curated engines when unpinned;
Tavily exposes none. Non-null unsupported values fail before network access.
This argument never changes the configured provider. Provider choice remains
user configuration, rather than a model-controlled credential/billing choice.

## Defaults and compatibility

Resolve selection once at the factory boundary:

| Authored configuration | Effective behavior |
| --- | --- |
| `search_backend` supplied | Load exactly that registered backend |
| Only legacy top-level `backend` supplied | Select DDGS and preserve its expression |
| Neither selector supplied | Select Tavily with its configured/default auth mode |

Keep `top_k=5` and the 15-second total timeout. Moving the unconfigured default
to Tavily is an intentional behavior change included in this proposed approval.
Do not auto-detect a different provider from unrelated environment variables.

DDGS-specific configuration becomes `[plugin.toolset.web.backends.ddgs]` with
the existing `backend` expression. Keep the old top-level `backend` as a
compatibility input. Reject simultaneous old and nested expressions, and reject
a legacy DDGS expression combined with a non-DDGS `search_backend`. Explicit
DDGS expressions retain their existing full-budget behavior and disable model
engine preferences; an unpinned DDGS instance retains the existing engine chain.

There is no automatic fallback between providers in this version. A failing
configured provider returns its own diagnostic; users can select another.
Future cross-provider policies must explicitly define allowed providers,
credential failures, deadlines, and costs.

## Future extraction

Define `WebExtractBackend` and `toolang.web_extract_backend` only when adding
`web/extract`. It will have an independent `extract_backend` selection. One
package may register both capabilities, but search-only backends never need a
dummy extraction method. Keep search ranking and page extraction as separate
contracts; do not add a generic capability registry now.

Users needing a different public tool schema or interaction model can still
publish a uniquely named toolset. Ordinary search-provider integration uses
the backend contract; replacing the built-in `web` namespace is not required.

## Implementation touchpoints and acceptance

- `base/protocols/web.py`, `base/types/web.py`, `base/errors.py`: public backend
  contract, records, and expected failure vocabulary.
- `plugin/web/`: configuration, selection/loading, DDGS/Yahoo and Tavily
  implementations; `plugin/toolsets/web.py`: stable tool wrapper and factory.
- `pyproject.toml`: bundled backend entry points. Keep dependency versions and
  installation unchanged; isolate DDGS imports inside its implementation.
- `setup/tools.py`, `setup/watcher.py`: backend provenance in setup revisions.
- `tests/unit/plugin/`: an external fake backend selected without code changes;
  unselected factories untouched; duplicate/missing/name/contract failures;
  root/agent config isolation; legacy selection and preference compatibility;
  normalized success, empty, filtered, and error envelopes; domain restrictions;
  total deadlines, DDGS process cancellation, and HTTP cancellation/cleanup;
  Tavily auth modes and HTTP error mapping without credential leakage.
- Preserve the existing DDGS regression suite. Add deterministic HTTP fixtures;
  the default suite remains offline. Run all checks in `AGENTS.md` before code
  commits. Separately exercise Tavily keyless and configured-key smoke checks
  when available, recording relevance/domain correctness and limitations.
- Update `docs/design/tools.md` and `docs/design/plugins.md` after implementation;
  maintain the user-visible default/configuration change through the required
  changelog workflow.

## Risks and approval

Anonymous Tavily access remains rate-limited and region/IP dependent. Changing
the default changes the recipient of unconfigured queries. DDGS's internal
engines remain subject to upstream markup changes. Publishing the backend
protocol creates a Python API compatibility obligation.

No technical questions remain open in this proposal. Human confirmation is
required for the proposed contract and the Tavily default before implementation.
