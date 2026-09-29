# Web search live evaluation — 2026-09-29

The final candidate improved successful default searches from **1/12 to 7/12**
in this local sample. It still failed five calls; this is evidence of a useful
repair, not evidence of production-wide availability.

## Method

- Baseline: PR #636 at `b22d883d`, already containing independent fallback and
  timeout fixes. Candidate: the interface and Yahoo adapter changes in the same
  PR. Both environments used locked DDGS 9.16.0 and primp 1.3.1.
- Six fixed queries, two rounds, alternating before/after order, two-second
  pauses, same machine and network. Each invocation used a separate process and
  the actual `web.search` tool with an empty plugin configuration.
- Cases cover the reported Fly.io query, the same query scoped to fly.io,
  English and Chinese Python documentation, SQLite WAL, and two allowed domains.
- Success means at least one usable URL. Expected-domain hits are checked
  separately; domain membership is not a full relevance or document-quality
  assessment. Page contents were not fetched by this benchmark.
- Latencies include tool worker startup and fallback, excluding interpreter
  startup. Failed calls remain in all denominators and latency statistics.
- JSONL records preserve arguments, errors, attempts, titles, URLs, and timing;
  snippets are omitted from the committed records.

Reproduce with separately installed checkouts:

```sh
uv run python scripts/benchmark_web_search.py \
  --before /path/to/before/.venv/bin/python \
  --after /path/to/after/.venv/bin/python \
  --output /tmp/web-search.jsonl
```

## Results

| Final paired comparison | Before | After |
| --- | ---: | ---: |
| Calls with usable results | 1/12 | 7/12 |
| Returned URLs on expected domains | 5/5 | 35/35 |
| Median latency, all calls | 2.706 s | 3.033 s |
| Median latency, failed calls | 2.722 s | 4.359 s |
| Maximum latency | 3.260 s | 5.062 s |

| Case | Before successes | After successes |
| --- | ---: | ---: |
| Fly.io | 1/2 | 2/2 |
| Fly.io with domain | 0/2 | 1/2 |
| Python with domain | 0/2 | 2/2 |
| SQLite WAL | 0/2 | 0/2 |
| Chinese Python with domain | 0/2 | 2/2 |
| Multiple domains | 0/2 | 0/2 |

Records: [final comparison](yahoo-fix.jsonl).

An earlier interface-only experiment had **0/12 successes in both versions**;
medians were 2.879 s before and 2.754 s after. The new interface preserved
diagnostics for all 12 failures, but did not itself restore availability.
Those [records](interface-only.jsonl) are retained to avoid presenting only the
successful phase.

## Verified causes and remaining failures

The [DDGS 9.16.0 Yahoo extractor](https://github.com/deedy5/ddgs/blob/v9.16.0/ddgs/engines/yahoo.py)
selects `relsrch` containers. Actual HTTP 200 pages also used `algo` containers
without that class, causing relevant results to disappear. Its link selector
could concatenate title and favicon links. The instance-local adapter handles
both layouts and chooses a single result link; synthetic fixtures cover both
layouts, advertisement exclusion, title cleanup, and redirect decoding.

Direct diagnostics also observed Brave/Google HTTP 429 and DuckDuckGo HTTP 202.
DDGS often reduces these to `No results found`; the tool does not infer an HTTP
status from that exception. Increasing a timeout cannot repair those responses
or an incompatible extractor.

In the final sample, four failed calls had empty Yahoo output and one had a
connection error. A subsequent SQLite diagnostic returned HTTP 200 containing
Yahoo's temporary-search-problem message and no result headings. A subsequent
multi-domain request returned seven parseable results. These follow-up probes
are excluded from the paired score; they show variability, not a fixed
multi-domain parser failure. An exploratory Bing RSS response was also rejected
as a candidate backend because HTTP 200 results were unrelated to the queries.

The extra provider increases failed-call latency. The default total deadline
remains 15 seconds and each fallback attempt is capped at five seconds; offline
regressions cover deadline exhaustion and cancellation. Yahoo compatibility
depends on an internal DDGS engine and must be reviewed on dependency upgrades.

## Real model smoke test

The same `.too` prompt used `web/*`, disabled recall/context, requested two
official URLs, required exact supplied tool names, limited search calls to two,
and prohibited inventing URLs. Both runs used `deepseek/deepseek-flash`, input
`Fly.io Machines deployment documentation`, and a 25,000-token run limit.

- Baseline `run_xtasha9e`: two failed searches; final answer reported the
  limitation without URLs. CLI duration: 14 seconds.
- Interface-only `run_vkndyrdq`: two failed searches; final answer used the
  diagnostic information. CLI duration: 10 seconds.
- Final `run_4znxqfb6`: two successful searches via automatic Yahoo fallback,
  with no backend preference supplied. Final answer cited
  `https://docs.fly.io/machines` and `https://docs.fly.io/launch/deploy`, both
  present in the [recorded tool outputs](model-final.json). CLI duration: nine
  seconds. This verifies discovery and tool integration, not page contents.

An unscored setup run first hit a 6,000-token limit after inventing a tool-name
prefix. The exact-name instruction and larger limit were then shared by all
three scored smoke runs. One model run per version is not a model-quality
benchmark; optional preference-based recovery is covered deterministically,
not demonstrated by these default-path model calls.
