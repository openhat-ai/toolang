# Reliable default web search

Status: Initial backend configuration approved on 2026-09-28; reliability repair
approved by the user on 2026-09-29 after reproducing search failures.

## Goal and success criteria

An unconfigured `web.search` tries independent search backends when a service
fails or returns no usable results. Users do not need an API key or backend
configuration. A successful search preserves the existing result schema.

## Findings

- The lockfile used DDGS 9.14.4. Google returned a redirect page with no parsed
  results; DDGS 9.16.0 received HTTP 429 on the same network. Both appeared as
  `No results found`. Brave returned relevant Fly.io documentation.
- DDGS 9.16.0 updates the Google and DuckDuckGo implementations. Its multi-backend
  aggregator can still discard a successful backend's results after another
  backend fails; an offline reproduction confirmed this in both versions.
- Toolang searched once, then filtered domains locally, with no fallback after
  errors or an empty filtered result.

## Scope and decisions

- Require `ddgs>=9.16.0,<10` and lock 9.16.0.
- With no configured `backend`, try `brave`, `google`, then `duckduckgo` in
  separate, sequential DDGS calls. Stop at the first usable result set, even
  if it contains fewer than `top_k` results. Do not merge providers or use the
  DDGS multi-backend aggregator for this default path.
- Preserve an explicit `backend` string as one DDGS expression. Single names,
  `auto`, and comma-separated expressions remain supported; these explicit
  choices do not receive the default fallback chain.
- Retry on DDGS exceptions, an attempt timeout, empty results, or results
  entirely removed by URL/domain filtering. Propagate cancellation and
  unexpected programming errors.
- Keep the existing total `timeout` (15 seconds by default). Cap each attempt
  at five seconds, including worker startup, and keep process workers
  cancellable. The total deadline covers all attempts.
- Add `site:hostname` for one domain and `(site:a OR site:b)` for multiple
  domains. Keep strict hostname/subdomain filtering afterward and discard
  malformed or non-HTTP(S) result URLs. Preserve the original query in output.
- If all attempts fail, raise a `ToolangError` saying the search services
  returned no usable results and listing the attempted backends. Preserve the
  total-timeout error. Log backend, elapsed time, error, and raw/usable result
  counts at debug level; do not claim that no relevant pages exist.
- Keep `backend` out of model-facing arguments. Do not add proxy, region,
  caching, paid-provider, or other toolset changes.

## Implementation touchpoints and acceptance tests

- `pyproject.toml`, `uv.lock`: update DDGS and its dependency graph.
- `src/toolang/plugin/toolsets/web.py`: default fallback, deadlines, domain
  query construction, filtering, and diagnostics.
- `tests/unit/plugin/test_web_search.py`: offline tests for failures and empty
  results followed by success; later-backend success; explicit selection;
  all-backend failure; domain scoping and strict filtering; per-attempt and
  total deadlines; cancellation; unexpected errors.
- Preserve process isolation and existing tool/result schemas. Run the focused
  plugin tests and all default repository checks. Run live smoke checks
  separately from the deterministic default test suite.

## Risks and open questions

Free search endpoints still vary by IP, region, throttling, and markup changes;
these defaults improve resilience but cannot guarantee availability. Explicit
DDGS multi-backend expressions retain upstream aggregation behavior. The
initial backend order is based on a small local sample, not a reliability SLA.
No open questions block this repair.
