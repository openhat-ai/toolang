# Agent Startup Preparation

Approved on 2026-10-14. Implement on top of
[PR #805](https://github.com/openhat-ai/toolang/pull/805), including its
single-process host `serve` and unified runtime status contracts.

## Goal

Prepare State locally before entering or creating an AgentServer workload.
Show required work through parallel ref/status slots, then one dim summary.
Cached preparation is silent and adds no presentation delay.

## Vocabulary

| Operation | Slot word | Meaning |
| --- | --- | --- |
| `resolve` | `resolving` | Resolve a selector or shorthand to a canonical ref. |
| `fetch` | `fetching` | Download remote source bytes or cap files. |
| `parse` | `parsing` | Parse and validate authored `.too` source. |
| `materialize` | `updating` | Build local cap content, including initial creation. |
| `load` / reuse | none | Read valid cached source or prepared artifacts. |
| `prepare` | summary only | Successfully complete the preparation pipeline. |

A `ref` identifies a source; a `cap` is a capability; a `slot` is a reusable
row; the `summary` is the single final line. An agent is not a cap. Retain
accurate domain names such as `AgentState`, `Program`, `materialize`, and
`publish`. Rename the private downloader to `_fetch_remote_cap_files`: it
fetches and maps bytes rather than publishing a layer.

## Startup And Ownership

- Background host and guest launches share local preparation in
  `up/sandbox.py`, after layout validation and before sandbox planning/workload
  creation. Foreground host `serve` uses that same preparation function before
  entering the server in its existing process.
- Pass concrete layout, workspace additions, and State-owned cap overrides
  (`psyches`, `skills`, `services`, `prompts`) from `ServeSpec`. Guest workspace
  mounts use the returned State. Keep lifecycle ownership, recovery, and State
  build/publication locking from #805; create no new runtime status format.
- Keep server-side initial State validation and subsequent watchers. Unchanged,
  compatible layers may be read/hashed/loaded/composed, but must not be parsed,
  fetched, or materialized again. A `current` pointer alone is not proof of
  freshness. Changed inputs or incompatible caches retain existing refresh and
  rejection behavior.
- Setup remains server-owned because tools, providers, and credentials depend
  on its execution environment. Preserve concurrent server Setup/State refresh.
- Attaching to an existing server does not prepare local State. Preserve local
  one-shot host execution, hidden `_serve`, public commands, and flags.

## Remote Agent Ordering

1. Resolve/fetch remote `.too` source and show its ref in one slot.
2. Parse and validate the agent through its language/State owner. Writing
   `agent.too` is not parse success. A valid cached Program may satisfy this
   gate; reuse the exact parsed Program during State construction.
3. Only after successful parsing, discover and prepare dependencies. This gate
   precedes root/home dependency work for remote-agent startup. Independent
   dependencies use the existing bounded worker pool.

Use one progress session across remote source acquisition and managed launch.
The agent slot disappears before dependency slots appear; emit no intermediate
summary or timer reset. Fetch/parse failure starts no dependencies or server.
Preserve remote-source cache freshness policy. Cache hits in either phase are
silent; show and summarize only work performed.

## Shared CLI Presentation

Cap inspection, publication after saved cap changes, offline `info`, and local
scripts use the same preparation presenter. Inspection still resolves caps
without preparing executable State. Remote-cap validation shows ref rows but
does not produce a preparation summary unless State is subsequently published.

The CLI owner explicitly completes its preparation display before command
records, Setup inspection, or execution progress. Context closure only releases
the display. Visiting routes that only acquire source complete before command
dispatch; routes whose commands prepare dependencies share that command's
session and completion boundary. Keep this ownership in the command registry.

Completing a display is a presenter operation, not a synthetic State event.
Core-owned State completion events may finish the same display. Setup and
runtime events retain their operational presentation.

## Slots And Output

- Active TTY rows contain only `<ref> fetching`, `<ref> updating`, or another
  operation word above. Show no spinner, heading, counters, queued/completed
  rows, completion marks, or per-row timers. Apply `dim` to refs, action words,
  and the final summary; use no accents or bold styling.
- Keep a cap in its slot across stages. On completion, reuse the slot for the
  next active item; without a replacement, remove its line. Bound slots by
  actual concurrency (currently four remote-cap workers), not total work size.
- Respect terminal width and height: shorten refs while retaining the action;
  display the earliest active slots that fit and promote others as work drains.
  Resizing and warning suspension preserve task state and slot ownership.
- Preserve the 150 ms reveal threshold for actual work. Do not wait to animate,
  redraw elapsed-time ticks, or reveal cache checking even when it takes longer
  than the threshold. Very fast updates may skip slots but retain the summary.
- After successful preparation, clear every slot and write exactly one summary
  at the region's original first line, leaving no blank/stale rows. Include
  measured affected-cap counts and total elapsed time; count the agent
  separately. Use a State summary if preparation changed no caps. Full cache
  hits emit no rows, cache-hit messages, or summary.
- Progress and summaries go to stderr. Non-TTY output uses concise operation
  transitions and one summary without ANSI/cursor controls or repeated ticks;
  cache hits remain silent. Preserve stdout, disabled progress, unrelated
  presenter consumers, command-ready output, and warning/error diagnostics.
- Failure/interruption clears transient output, retains actionable error
  reporting and existing exit semantics, and emits no success summary.

Illustrative active rows:

```text
github://example/caps/prompts/search.md@main fetching
github://example/caps/prompts/reviewer.md@main updating
```

When the first task finishes, its slot shows the next active ref or disappears.
After all work succeeds, the same region contains only, for example:

```text
Prepared agent and 4 caps in 1.4s
```

## Concurrency And Events

- Preserve bounded parallel remote-cap preparation, deterministic result order,
  and existing failure/cancellation/publication semantics. Keep root/home layer
  scheduling unchanged apart from the remote parse gate.
- Workers emit structured events; one thread-safe presenter writes the terminal.
  Coalesce slot refreshes to at most ten per second. Rendering performs no
  preparation I/O or network work and does not extend publication/build locks.
- Distinguish task scope and identity so repeated refs in different modules or
  layers cannot collide. Stage completion is distinct from item completion:
  fetched bytes may still need updating. Terminal events are idempotent, and
  late events cannot overwrite a slot's new occupant.
- Track totals internally across batches. An empty active set between batches
  is not overall completion; finish only after preparation returns successfully.
  Ignore events after closure. Model cache hits explicitly: legacy `Fetched`
  labels with cached data must not become visible download rows.
- Add the `parse` preparation stage and backward-compatible operation, source,
  scope, cache, and completion metadata. Do not derive actions from prose labels,
  add HTTP progress endpoints, or add process-to-process progress transport.

## Touchpoints

- `up/{process,sandbox}.py` and CLI runtime, routing, and server acquisition:
  source acquisition, local preparation, and shared progress session.
- `state/{source,acceptance,prepare,state}.py`: parsed-Program reuse, ordering,
  policy parity, cache semantics, and preparation events.
- `base/types/progress.py`, `common/progress.py`, and `cli/common/progress.py`:
  structured events and scoped slot presentation.
- Existing State, startup, routing, and operational-progress tests;
  `docs/agent-state.md`; and `CHANGELOG.md`, generated through
  `too aid.too update_changelog` and verified by a maintainer before merge.

## Acceptance

1. Host/guest local preparation completes before workload creation; foreground
   host stays in one process. Startup overrides match server policy, and guest
   mounts use the same prepared State.
2. A preparation failure creates no workload, reports its cause, and releases
   lifecycle ownership for a successful later retry.
3. Cold remote acquisition parses once before any dependency request; malformed
   source blocks all dependency work and command execution.
4. Warm cache hits emit zero preparation bytes in TTY/plain modes, even after
   the reveal threshold. A new process reuses compatible persisted layers
   without Program parsing, fetching, or materialization.
5. Source changes invalidate parsed reuse; schema/corrupt-cache and remounted
   guest cases retain correct rebuild/path behavior and later watcher refresh.
6. Barrier-controlled tasks prove actual concurrency. Out-of-order completion,
   repeated refs across scopes, duplicate/late events, and multiple batches
   preserve counts, deterministic results, and correct slot ownership.
7. Slots change action in place, accept replacement tasks, and disappear as
   work drains. No spinner/header/counts/completed rows appear. Final success
   clears all rows and leaves one dim summary at the original first line.
8. Cover mixed cache/update phases, agent-only/dependency-only work, narrow and
   short terminals, warning suspension, resizing, fast work, cancellation,
   disabled output, stderr routing, and non-TTY output without ANSI controls.
9. Foreground/background/automatic acquisition share preparation behavior;
   attachment, one-shot execution, and unrelated presenters retain their scope.
10. Ruff lint/format, `ty check`, the default offline pytest suite, and
    `git diff --check` pass before implementation commits.

## Risks

- Local validation adds one pass; preserve expensive-work reuse rather than
  removing the server's freshness check.
- Concurrent source edits, guest config rebasing, or incompatible CLI/guest
  schemas may require server-side rebuilding without launcher progress.
- Valid caches may remain after a later startup failure and are reusable.

No open design questions remain.
