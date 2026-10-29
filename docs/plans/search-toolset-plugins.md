# Search toolsets

Status: Bundled Tavily/SerpAPI implementation approved by the user on 2026-10-29.
Independent distributions and a base-package split are deferred.

## Scope

Register ordinary `tavily` and `serpapi` toolsets in the Toolang distribution,
using existing factories, Tool/Toolset contracts, configuration, and allow
policy. Tools remain exposed without credentials or API availability checks;
invocation reports normal provider failures. Each provider owns its API and
native result content. No router, readiness protocol, or shared search schema.

- `tavily/search(query, max_results=5, include_domains=None)`: basic search,
  1–20 results, no generated answer/raw content; keyless when TAVILY_API_KEY is
  absent, keyed otherwise.
- `serpapi/search(q, gl=None, hl=None)`: Google web search, requiring
  SERPAPI_API_KEY only when invoked.
- Each plugin accepts `timeout` (default 15) and `api_key_env`; reject literal
  key configuration. Resolve credentials at the factory boundary from the
  selected workload environment. Bound requests and propagate cancellation.
- Stop registering the unreliable DDGS-backed `web` toolset; retain its source
  and direct tests. Migrate `web/search` calls/selectors to the new APIs.
- Package extraction and any future composite search tool are separate work.

## Touchpoints and acceptance

- `plugin/toolsets/tavily.py`, `serpapi.py`, and `pyproject.toml`: independent
  implementations and ordinary built-in entry points; no new dependencies.
- Tests: default discovery without keys, allow filtering, independent schemas,
  native requests/results, provider errors and safe credential handling,
  deadlines/cancellation, and lifecycle summaries. Keep the default suite offline.
- Verify a built wheel exposes both tools without provider configuration, omits
  `web/search`, and supports a separate live Tavily keyless smoke check.
- Update current tool documentation and generate changelog coverage through
  `too aid.too update_changelog`; run all repository checks before code commits.

Keyless service limits still apply; SerpAPI requires admitted credentials.
Removing `web/search` requires users to update authored tool references and
arguments. There are no open implementation decisions.
