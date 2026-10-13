# Simplify top around current runtime activity

Status: proposed; human approval is required before implementation. This PR
defines behavior only. On approval, this plan supersedes the observation,
activity eligibility, statistics ranges, and controls in
[Top activity](top-activity.md) and [Top dashboard](top-live-layout.md).
Their terminal layout and navigation rules remain unless replaced below.

## Goal and scope

Make `too top` a read-only observer of currently registered runtime instances:
discover instances, periodically sample absolute snapshots, and render current
work plus consumption accumulated by each runtime. Success means identical
results with the Hub running, stopped, or absent, and counters independent of
when or how many clients open top.

In scope: runtime discovery and snapshot reads, current-work presentation,
session consumption, removal of top's Hub/event/window dependencies, and the
explicit CLI changes below. Out of scope: time series, rates, charts, retention
or compaction, historical browsing, new metrics dependencies/exporters, execution
control, changes to chat/script progress, and global removal of existing activity
APIs or accounting storage.

## Observation contract

- `too top` observes registrations under the resolved root. `too alice top`
  applies the existing layout/home selection to those registrations; it does not
  read a stopped agent's store as a fallback. Preserve resident, visiting,
  roaming, script, and sandbox instance discovery and observation-root behavior.
- Identity is the registered instance, not the agent name. Deduplicate repeated
  discovery of the same instance; show separate instances of the same agent.
  Use the registration ownership token and executor session as the observation
  generation. Reject late reads after replacement, removal, or session change.
  Registration/startup must identify the owning executor session before a store
  can be sampled, so an old store session cannot masquerade as a new runtime.
- Keep PID/create-time and sandbox ownership checks in discovery. They establish
  which instances exist; they are not presence metrics. A successful discovery
  removes absent instances and their cached values. A discovery error preserves
  the last set as stale and retries; it must not assert an empty scope.
- `execution` owns a query-free current snapshot reader over committed runtime
  records and accounting. `up` owns discovery, source selection, and per-instance
  sampling. Top owns presentation. This path must not import `teaming`, connect
  to a Hub/backend, acquire an executor, or create/migrate stores.
- Prefer a registered, accessible local store opened read-only. This supports
  embedded host scripts without adding an HTTP server. For instances whose store
  is inaccessible but whose registered agent endpoint is accessible, use a new
  `GET /api/v1/activity/current` endpoint backed by the same reader. Pass the
  expected session to both readers and reject a different session. Do not start
  a service or fall back to Hub HTTP/cached activity. Unsupported schemas or
  unusable sources are explicitly unavailable.
- Sample immediately, then use `--refresh` as the discovery/data refresh interval,
  default **1 second**, accepting finite positive seconds. Use a 2-second source
  read deadline and at most four concurrent source reads. Allow only one read per
  instance in flight, skip missed ticks, and never queue old samples. Input and
  resize repaint immediately using the latest completed snapshots.
- Each instance read is one atomic replacement. Instances update independently;
  there is no cross-runtime transaction. A failed read freezes its last snapshot,
  marks it stale, and excludes it from fresh header totals with explicit partial
  coverage. Recovery replaces it with a fresh snapshot; no event replay, cursor,
  subscription, or reconnect baseline is required. Keep only the latest sample.
- `--once` and non-TTY output perform one discovery/sample round. No instances
  is a successful empty result. Discovery/read failures render explicit partial
  or unavailable coverage and return a nonzero exit status.

## Snapshot and consumption

Define `RuntimeActivitySnapshot` and `RuntimeActivityNode` in
`execution/schemas.py`; keep the legacy `ActivitySnapshot` contract. A snapshot
contains session, revision, sample timestamp, cumulative metrics, active counts,
current threads/root runs/paths, and coverage. `up` attaches registration identity,
agent label, placement, and drivers. No presence, ranges, terminal results, output
payloads, progress counters, or event cursors are included.

- Consumption covers the **owning executor session**, from its start through the
  sample. It includes completed work in that session while the instance remains
  registered. Restart starts a new session; store lifetime history is not shown.
  The header sums current instances, so removing one can reduce displayed totals.
  This is a scope total, not a durable total across departed runtimes.
- Runtime writes maintain accounting in the same transaction as execution facts.
  Reuse existing attempt identities, ownership, session aggregates, and checkpoints.
  Reads never build consumption by replaying events or scanning full history.
  Each actual model/tool attempt contributes once; counts settle at begin and
  tokens/cost at end. Retry adds consumption; rewind never refunds it; forked
  references do not duplicate it. Preserve reported/estimated and unknown/partial
  accounting semantics, including CACHED already being included in IN.
- Runtime/header totals count each instance once. Thread/run/step metrics include
  owned descendant consumption; never sum inclusive tree rows into parent totals.
  Preserve TIME+: a run/step's own execution duration, and the sum of top-level
  run durations for thread/runtime. Parallel time can exceed wall time. The reader
  computes current durations at sampling; top never extrapolates stale values or
  includes downtime.
- No new time buckets, sample history, rolling queries, or archive-total mechanism
  are introduced. Cumulative totals update when consumption occurs, independent
  of observation. Existing minute buckets and legacy readers remain for current
  Hub/HTTP consumers; this plan does not authorize a storage migration or remove
  their writes. The new reader uses session totals and current-state indexes only.
- Current work consists of pending/running executions owned by this session.
  Completed executions disappear on the next sample. A short call may never appear
  between samples, but its consumption remains counted. Retain terminal ancestors
  only when needed to identify a still-running descendant, including async launch.
- Bound a snapshot to the newest 200 active root runs, ordered by creation time
  then ID, and 4,000 required path nodes. Derive displayed threads from these roots.
  Report truncation explicitly; full indexed active counts and session totals stay
  independent of these limits. Local filtering/sorting applies to sampled rows,
  with that limitation visible. Never report idle from a truncated/incomplete read.

## Presentation and CLI changes

Keep the alternate screen, aligned/cropped rows, shared colors, Unicode handling,
selection, scrolling, resize behavior, terminal restoration, and title prefix.
The header says `Runtime activity`, shows active thread/root-run counts and session
consumption, and retains the clock. Replace Period/Recent with a fixed indication
that totals cover current runtime sessions. Show sampling failures in the status
bar, without any online/offline/unknown legend or global reconnect state.

| Existing parameter/control | Decision |
| --- | --- |
| `--recent`; F7 Recent | Remove, including duration parsing, presets, expiry, and `ACTIVITY(30m)` headings. |
| `--since`; F8 Period | Remove all/session/rolling/fixed-timestamp selection; current runtime session is the only range. |
| `--active`; Ctrl-A in filter editor | Remove; current work is inherent, with no history/Active toggle. |
| `--refresh` | Keep; change default from 0.1 to 1 second and make it the sample interval, not an independent paint deadline. |
| `--once`, `--title-prefix` | Keep; preserve non-TTY snapshots and terminal/title behavior. |
| `--view agent\|thread\|execution`; F5, a/t/e | Keep; `agent` means one row per runtime instance. Defaults remain agent for root scope and thread for a selected agent; F5 labels become Runtimes/Threads/Runs/Tree. |
| `--tree`; Left/Right folding | Keep execution paths and parentage only; no completion ratios or progress reconstruction. |
| `--sort activity\|spend\|cost\|time`; F6 | Keep `cost` as the spend alias. Activity sorts running before pending before idle, then latest change, with stable identity ties. |
| `--filter`; F4 | Keep literal case-insensitive filtering over sampled IDs, names, states, and summaries; remove historical-descendant search. |
| Enter/Esc Details, paging, Help, Quit | Keep; Details shows current metadata, exact cumulative values, session/sample identity, and coverage. Remove Stats/Total duplication and result loading/Markdown. |

Replace the `S` column with `INSTANCE`; show its full ID in Details. Retain metric
columns and applicable THREAD/RUN/STEP columns. Runtime rows stay visible when
idle so their session totals remain visible; thread/execution views include only
current work, with no branchless offline/idle placeholders. Filtering never alters
header totals. Run/model/tool activity uses name, state, and compact summary, not
streamed content, completed/expected counts, or recently completed results.

## Removal boundaries and implementation order

1. Add the current snapshot contract/reader and session identity validation in
   `execution/{schemas,activity,statistics}.py`; reuse committed accounting rather
   than implementing another event reducer or per-client accumulator.
2. Adapt `up/{observation,activity,core,server,sandbox}.py` and script registration
   in `cli/toolang/commands/script.py` to publish the owning session, sample current
   snapshots, and handle replacement/removal. Add the direct agent endpoint in
   `api/routers/activity.py` without changing existing activity routes.
3. Replace top's observation wiring in `cli/toolang/commands/top.py` and
   `cli/common/activity.py`. Delete HubProcess connection lookup, backend headers,
   talk context, AgentProcess presence callback, stopped-store fallback,
   CombinedObservation/LocalObservation/HubObservation use, and SSE receive/retry
   loops. Do not acquire messaging identities when opening the terminal.
4. Simplify `cli/common/{activity_view,activity_dashboard}.py`: remove query
   attachment/generations, page/checkpoint/roster staging, window caches/presets,
   presence rendering, recent/failed eligibility, progress counts, historical
   filters, async result loads, and result-render caches. Replace them with
   per-instance snapshot replacement and local view/filter/sort state.
5. Preserve `teaming/{observation,hub_observation,activity,activity_feed,activity_api,
   activity_cache}.py`, backend activity caches/leases, and `work/teaming.py` unless
   removing a symbol proven to have no remaining callers. Preserve canonical
   subscriptions and chat/script progress. Remove top-only tests for discarded
   behavior; retain legacy API/Hub tests and add the acceptance scenarios below.
6. Update focused CLI/API documentation and the superseded top plans, and generate
   breaking-change/migration entries through `too aide.too update_changelog` in the
   implementation PR. Existing `--recent`, `--since`, and `--active` invocations
   must fail as unknown options; remove those arguments when migrating. Historical
   work/results remain available through existing inspection commands and legacy
   APIs. Do not add compatibility no-ops or another window flag.

Likely tests: `tests/unit/{execution,up,cli}/test_*activity*.py`,
`tests/unit/cli/test_top*.py`, `tests/integration/messaging/test_activity_wire.py`,
`tests/integration/cli/test_runtime_progress.py`, and
`tests/system/cli/test_top_e2e.py`. Add focused endpoint tests in the existing API
test suite. This definition changes only this plan file; no runtime or changelog
change ships before approval.

## Acceptance

| Scenario | Pass condition |
| --- | --- |
| Hub absent/stopped/running, no Valkey | Identical top snapshots; no Hub/backend calls or messaging context. |
| Resident/visiting/roaming, embedded script without endpoint, sandbox with inaccessible local store | Every registered instance is sampled through the selected local/direct-HTTP source without starting services. |
| Same agent, multiple instances and duplicate registration scopes | Distinct instance rows; each instance contributes once. |
| Late attachment, short calls, two top clients, skipped samples | Cumulative values agree at the same boundary and include unsampled completed calls without double counting. |
| Parallel/nested/async work, retry/rewind/fork | Correct consumption, TIME+, and required ancestors; no duplicate owned usage or progress rendering. |
| Startup, session restart, PID reuse, replacement, exit during read | No previous-session or late snapshot accepted; departed instances disappear after successful discovery. |
| Slow/failed source, failed discovery, unsupported/missing store | Responsive input and healthy-source updates; bounded reads, explicit stale/partial coverage, no fabricated zeros or false empty scope. |
| Large active scope, small history, large history | Fixed snapshot limits and truthful coverage; counts/totals unaffected by truncation/filter/view, with no per-tick full-history scan. |
| No active work, finished run, stopped agent | Idle runtime row only at runtime level; no recent result/offline rows, prior-session consumption, or historical fallback. |
| CLI/help/Details and wide/narrow/Unicode/resize/exit | Only retained flags/keys work; default sampling is 1 second; current metadata and terminal behavior remain correct. |
| Once/non-TTY with empty, healthy, partial, unavailable scope | One bounded sample round; zero exit status only for complete observations, including the empty case. |
| Existing Hub/activity API and chat/script subscriptions | Existing contracts and progress continue to pass unchanged. |

Definition validation: verify references and current behavior against the source,
then run `git diff --check`. Implementation must add offline deterministic
acceptance tests and pass `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`, and `uv run pytest -n auto` before each commit.

## Risks and open questions

Breaking CLI/window behavior, session-registration races, partial sampling, and
large active scopes are covered by the explicit contracts above. Polling trades
instantaneous event delivery for bounded refresh latency; this is intentional.
Existing legacy bucket storage remains a compatibility cost and can be addressed
only by a separately approved API/storage plan. No implementation-blocking product
questions remain; human review and explicit approval of this definition are pending.
