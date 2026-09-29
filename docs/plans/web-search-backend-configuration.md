# Reliable default web search

Status: Initial backend configuration approved on 2026-09-28; reliability repair
approved by the user on 2026-09-29 after reproducing search failures. The user
also approved the model-facing interface, guidance, and live comparison update
on 2026-09-29.

## Goal and success criteria

An unconfigured `web.search` tries independent search backends when a service
fails or returns no usable results. Users do not need an API key or backend
configuration. A successful search preserves the existing result fields.

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

- Require `ddgs>=9.16.0,<10` and lock 9.16.0. A small Yahoo adapter
  recognizes both observed `relsrch` and `algo` result containers, retains DDGS
  request/redirect handling, and avoids duplicated title/favicon URLs. It uses
  instance-local subclassing, never a global DDGS registry patch.
- With no configured `backend`, try `brave`, `yahoo`, `google`, then `duckduckgo` in
  separate, sequential DDGS calls. Stop at the first usable result set, even
  if it contains fewer than `top_k` results. Do not merge providers or use the
  DDGS multi-backend aggregator for this default path.
- Preserve an explicit `backend` string as one DDGS expression. Single names,
  `auto`, and comma-separated expressions remain supported; these explicit
  choices do not receive the default fallback chain.
- Retry on DDGS exceptions, an attempt timeout, empty results, or results
  entirely removed by URL/domain filtering. Propagate cancellation and
  unexpected programming errors.
- Keep the existing total `timeout` (15 seconds by default). Cap each default
  fallback attempt at five seconds, including worker startup, and keep process
  workers cancellable. The total deadline covers all attempts. An explicit
  backend expression retains the full configured total budget; DDGS request
  timeouts remain capped at five seconds in both cases.
- Add `site:hostname` for one domain and `(site:a OR site:b)` for multiple
  domains. Keep strict hostname/subdomain filtering afterward and discard
  malformed or non-HTTP(S) result URLs, including invalid ports and unescaped
  whitespace. Preserve the original query in output.
- If all attempts fail, report that the search services returned no usable
  results and list the attempted backends. Preserve the total-timeout message. Log backend, elapsed time, error, and usable result
  counts at debug level; do not claim that no relevant pages exist.
- Add optional model-facing `preferred_backend`: a single curated engine to
  try first, followed by the normal default chain without duplicates. Leave it
  unset for normal searches. Offer Brave, Google, DuckDuckGo, Yahoo, Mojeek,
  and Startpage; never pass a model-supplied expression to DDGS. Explicit plugin
  backend configuration takes precedence and disables model preferences.
- Publish an explicit typed input schema with descriptions for query, result
  limit, hostname restrictions, and backend preference. Reject blank queries,
  malformed domains, and unsupported preferences before contacting providers.
- Preserve query/domains/results in successful output and add status, selected
  backend, and compact attempt diagnostics. Exhaustion and total timeout return
  `ToolResult(error=...)` with the same structured diagnostics and recovery
  guidance, so runtime error status and model-visible evidence are both kept.
  Never infer HTTP status or claim that no pages exist from DDGS's ambiguous
  no-results exception. Propagate cancellation and unexpected errors.
- Guide the model to use focused queries, restrict to authoritative hostnames
  when appropriate, treat snippets as discovery evidence, inspect attempt
  diagnostics, and avoid repeating identical failed calls immediately.
- Compare the prior PR head (b22d883d) with the updated implementation using
  real tool invocations, identical English/Chinese and domain-scoped queries,
  alternating run order, and recorded results/latency/failures. Keep live tests
  opt-in and report limitations; do not claim a reliability improvement unless
  measurements show one. Also exercise the new schema with a real model.
- Do not add proxy requirements, paid providers, persistent caches, or changes
  to other toolsets.

## Implementation touchpoints and acceptance tests

- `pyproject.toml`, `uv.lock`: update DDGS and its dependency graph.
- `src/toolang/plugin/toolsets/_web_yahoo.py`: Yahoo layout compatibility.
- `src/toolang/plugin/toolsets/web.py`: default fallback, deadlines, domain
  query construction, filtering, and diagnostics.
- `tests/unit/plugin/test_web_search.py`: offline tests for failures and empty
  results followed by success; later-backend success; explicit selection;
  all-backend failure; domain scoping and strict filtering; per-attempt and
  total deadlines; cancellation; unexpected errors.
- `scripts/benchmark_web_search.py` and `docs/evaluations/`: reproducible live
  comparison and measured outcomes.
- Preserve process isolation and existing successful result fields. Run the focused
  plugin tests and all default repository checks. Run live smoke checks
  separately from the deterministic default test suite.

## Risks and open questions

Free search endpoints still vary by IP, region, throttling, and markup changes;
these defaults improve resilience but cannot guarantee availability. Explicit
DDGS multi-backend expressions retain upstream aggregation behavior. The
initial backend order is based on a small local sample, not a reliability SLA.
No open questions block this repair.
