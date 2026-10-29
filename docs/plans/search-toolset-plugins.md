# Search toolset plugins

Status: Direction confirmed on 2026-10-29. Definition only; provider
implementations need their own scoped definitions.

Search integrations are ordinary toolset plugins using the existing
[plugin](../design/plugins.md) and [tool](../design/tools.md) contracts:

```toml
[project.entry-points."toolang.toolset"]
tavily = "tavily_toolang:create_toolset"
```

## Decisions

- Provider packages such as `tavily-toolang` and `serpapi-toolang` expose
  independent names, for example `tavily/search` and `serpapi/search`.
  These are proposed packages, not existing implementations.
- Each plugin owns its parameters, result content, configuration, credentials,
  and errors. Configuration uses its own `[plugin.toolset.NAME]` table.
- Installed tools are exposed and callable under existing allow/disallow and
  run resource policy. Do not inspect credentials or probe API availability
  to decide whether a tool is registered, listed, or selectable.
- Missing credentials, authentication failures, and service outages are ordinary
  invocation errors. They must not hide tools or block unrelated toolsets.
  Follow existing environment handling and keep credentials out of diagnostics.
- The Tavily integration should work without provider configuration or a key.
  Keyed providers may require credentials when invoked.
- Add no search-specific adapter family, catalog, readiness protocol, router,
  rotation, fallback, or shared search API. Keep the existing DDGS-backed
  `web/search` unchanged.
- If combined search is needed later, define a separate ordinary toolset that
  owns composition and adaptation to the provider APIs.

## Touchpoints and acceptance

Provider package `pyproject.toml`, implementation, and tests own the work;
no new Toolang core contract is required. Verify that:

- Installing a package exposes its tools without additional activation config.
- Multiple provider packages coexist; existing duplicate-name checks remain.
- Missing credentials and API outages do not affect tool exposure; user policy
  can exclude tools, and actual calls report provider errors.
- Each plugin retains its own API and contacts only its provider.
- Tavily supports empty provider configuration and no key.

Keep tests offline by default and preserve existing DDGS behavior. Provider
definitions must specify their APIs and document credentials, service limits,
and any charges. This change modifies only this plan.
