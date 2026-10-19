# Top header and diagnostic audit

Status: approved by the maintainer on 2026-10-19 for implementation in PR #864.

## Goal and scope

Make `too top` and `too AGENT top` quiet during normal empty, idle and transition
states. Show zero only for known empty observations, keep genuinely unknown
values distinct, and reserve notices for failures that prevent observation.
Agent running/total counts are independent of root Run counts.

This plan supersedes the header styling, placement-only agent sentence and
separate bottom coverage line in [Top recent activity](top-recent-activity.md#header-and-unified-table).
Keep the existing execution/accounting contracts and the stopped-instance fix
in [PR #864](https://github.com/openhat-ai/toolang/pull/864). The latest requested
layout reserves the bottom line for function keys, without error messages.

## Audit of current situations

The table separates existing behavior from proposed treatment. This is not a
request to turn every unknown observation into a warning.

| Situation | Current behavior | Proposed treatment |
| --- | --- | --- |
| Complete empty inventory | Empty aggregates are zero. | Keep zeros; no notice. |
| Installed stopped agent with readable history | Main requires a live snapshot; PR #864 corrects this. | Running/active zero; retain historical completions/usage; no live notice. |
| Newly installed inactive agent without runs.db | Read-only attachment fails; completion and usage become unavailable. Reproduced. | Treat the not-yet-created history store as empty: zero durable counts/usage, without creating a database or warning. Do not extend this to an established store disappearing during observation. |
| Valid empty runs.db or no matching window records | Queries produce zero. Reproduced. | Keep zero; no notice. Old lifetime records do not make an empty current window an error. |
| Running idle agent | Fresh empty live snapshot has zero active roots. | Agent running count includes it; no notice. |
| Running embedded script with no HTTP endpoint | Normal embedded registration has an empty endpoint; Top nevertheless emits live unavailable. Reproduced. | Count the running agent normally. Unsupported live observation stays unknown, without an error notice; durable usage/completions remain readable. |
| First source poll still pending | SourceActivity defaults to live_stale=true, so the renderer emits live unavailable before any failed read. Reproduced. | Pending values stay unknown briefly, without warning/updating suffixes. Do not infer a failed read or zero work. |
| Agent preparing/starting | Discovery normalizes transitional state to unknown and sets a diagnostic, making the inventory partial. | A known preparing/starting agent is not yet running; count zero for its running contribution, keep membership, and show no error. |
| Missing/invalid drivers metadata on a known running agent | An item diagnostic marks inventory partial; schemas suppress otherwise readable agent/run/stat summaries. Reproduced. | Ignore this metadata for Top counts and statistics; preserve valid results. |
| Period/Recent changes, obsolete poll results or epoch replacement | New generations load asynchronously; old responses are rejected. | Normal refresh behavior; no warning. Preserve existing generation and terminal-fact rules. |
| Readable usage sample older than five seconds | Renderer unconditionally adds stats as of TIMESTAMP. Reproduced. | Age alone is not a failure: retain the latest valid sample and omit the notice, including during slow scans. |
| Provider omits tokens/pricing | Only affected quantities become unknown; no source-error notice. Reproduced. | Retain field-level unknown values quietly. Do not add stats partial, or turn unknown prices into zero cost. |
| Failed/canceled task or model/tool call | Existing run statuses and counters represent the outcome. | A work outcome is not a Top observation error; no monitoring notice. |
| More completed roots than the loaded page | Renderer adds N/M completed loaded (PgDn). Reproduced. | Normal pagination; remove the footer message. Keep counts over the full window and existing paging. |
| Additional completion page fails | page_error is separate from summary counts; renderer emits completion page unavailable. Reproduced. | Preserve known totals/rows, keep retry and structured page_error, and omit a global metric/footer warning. |
| Confirmed stop/departure or recoverably obsolete registration | Retained sources supply history; departed live polling is retired. | Recompute current agent membership, retain eligible history, and omit live notices. Skip only registrations confirmed obsolete; an unresolved live member is not proof of departure. |
| Missing/corrupt/initializing stats.db | RecentReader never opens stats.db. Existing tests verify independence. | No Top error or repair; this cache is irrelevant to Top. |

## Failures that cannot safely become zero

| Actual failure | Correct treatment |
| --- | --- |
| Agent root/registry cannot be enumerated due to permissions or I/O failure | Current membership is unknown; never display a successful empty roster. A compact agents unavailable notice may be justified. |
| An existing agent's liveness cannot be determined, including corrupt/incompatible runtime state or failed container inspection | Keep known total/placement counts and independent readable metrics. Its running contribution is unknown, not zero. A notice affects agent liveness only. |
| A published, expected live endpoint fails to provide a snapshot | Distinguish a failed request from the normal absence of an endpoint. Keep agent liveness and durable facts independent; active coverage is unknown and cached rows freeze. |
| An existing runs.db is unreadable, corrupt, incompatible with the queries, or its records cannot be decoded | Affected durable totals are unknown. Do not replace previous activity/spend with zero or poison other independently readable metrics. |
| A previously readable history store disappears during the session | Preserve known facts and mark current coverage unknown; do not reinterpret established history as a newly unused agent. |

Only actual observation failures may produce a concise group notice on the
scope/clock line. Do not expose raw exception strings, paths or tracebacks in
the summary/footer. A brief failed refresh should retry without replacing
valid observations with zeros. Missing provider fields, sample age and loading
alone do not justify notices. Existing JSON diagnostic fields retain causes.

## Requested presentation

- First summary: `N running, M total: R resident, V visiting, O roaming`.
  Use agent-instance identities consistently with existing placement counts;
  include installed stopped residents and exclude departed retained sources.
  A running agent contributes one regardless of its number of root Runs.
- Separate membership certainty from liveness certainty. With a complete roster
  and uncertain liveness, only running is unknown; total/placements remain exact.
  Drivers metadata must not gate usable counts, usage or completions.
- Preserve one blank line above/below the three summaries. Add two terminal
  cells on both sides, including wrapped continuations. Retain the caption
  column/gutter and show captions only on the first line of each group.
- Use restrained htop-inspired emphasis: normal foreground dominates, with
  cyan as the only accent within the summary values. Only known nonzero numeric
  tokens, including their currency signs and compact suffixes, use cyan. Zero
  values (including `$0.0000`), unknown `-`, field names and punctuation retain
  normal foreground. Right captions, including windows, use terminal dim.
  Keep normal weight, no summary backgrounds, and no separate green/red palette
  for running/failed/canceled values. Table styles remain unchanged.
- Remove all metric suffixes such as partial/updating/unavailable. Most frames
  have no top-line notice. A necessary notice sits between scope and the clock;
  the clock keeps its eight-cell right edge. Never wrap or overlap this line.
- Remove the separate bottom coverage line and give its row to the viewport.
  Keep only function keys at the bottom. Compact their current 81-cell text
  when necessary, keeping F10 visible. All key bindings remain active.
- At narrow widths, clip/combine notices before scope, wrap metric sentences
  inside the padded region and omit captions before losing metric content.
  Use terminal cell widths, including Unicode names, for every budget.

## Evidence, acceptance and touchpoints

Audit inspected `up/observation.py`, `up/top.py`, `up/schemas.py`,
`cli/common/top_view.py`, `stats/recent.py`, `stats/projection.py` and the embedded
registration call site in `cli/toolang/commands/script.py`. Temporary fixtures
reproduced the cases marked above without modifying implementation/tests.
The 169 existing tests in `test_recent.py`, `test_runtime_observation.py`,
`test_top_observer.py` and `test_top.py` passed on the PR #864 worktree.

Acceptance coverage must include all audited cases, keep text/JSON counts
consistent, and preserve known-versus-unknown semantics. Verify 40/80/100/140
columns, wrapping/styles, multiple notices, window switches, selection,
Help/Details/Sort panels, resize and mouse hit targets after reclaiming one row.

Likely implementation files: `src/toolang/up/schemas.py`,
`src/toolang/up/top.py`, `src/toolang/cli/common/top_view.py` and their existing
unit tests; update `docs/top.md` and the runnable-generated changelog when the
behavior ships. Keep missing-history normalization local to Top rather than
silently changing the general recent-statistics reader's missing-source contract.

Risks/open decisions: absent history before the first read cannot prove that a
database never existed before this session; the proposed convention treats an
inactive agent without initialized history as empty, while detecting later loss.
Show a compact notice only after a completed observation reports an actual
failure; pending reads and ordinary retries alone do not produce notices.
Do not introduce a diagnostic Details feature or expand runtime behavior as part
of this presentation work without further agreement.
