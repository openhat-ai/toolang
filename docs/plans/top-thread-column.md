# Top thread column

Status: proposed; awaiting maintainer approval. Definition only.

## Goal and scope

Show the owning physical root's thread ID beside its Run ID in `too top` and
`too AGENT top`, including live descendants and recently completed roots.
Success means the identity remains visible through completion, paging and
offline history reads whenever the source supplies it.

Apply the column to interactive Tree/List, `--once` and non-TTY text output.
Keep existing commands, flags, ordering, accounting, navigation and styling.
Thread grouping, thread titles, thread sorting and Details are outside scope.
The existing [Top design](../design/top.md) remains the baseline for other behavior.

## Presentation

Use this column order:

```text
AGENT THREAD RUN MODEL TOOL INPUT CACHED OUTPUT SPEND TIME+ STATUS
```

- `THREAD` contains the complete thread ID, without shortening its prefix or
  substituting a title. Repeat the owning root's thread on every descendant row,
  matching the existing `RUN` ownership convention. Show `-` when unavailable.
- Left-align `AGENT`, `THREAD` and `RUN`; retain numeric alignment. Derive the
  thread width from the heading and projected values using terminal cell widths,
  with a minimum of six cells. Tree branches remain in `STATUS`.
- Preserve the existing single-line, right-edge ellipsis clipping and footer at
  narrow widths. Long IDs consume space and can move later columns beyond the
  viewport; do not add wrapping, hidden-column modes or horizontal scrolling.
- Keep numeric heading click targets aligned after inserting the column.
  Clicking `THREAD` does not change sorting.

## Data and ownership

Live projection already receives `LiveTreeSnapshot.thread_id`; carry it into
each projected `Row` in the same tree. Do not infer identity from Run IDs or
query a store from the renderer.

Completed roots currently use `RunNode` without a thread field. Add an optional,
keyword-only `thread_id: str | None = None` field to that schema and populate it
in `stats.recent._root_node` from the persisted `runs.thread` value. The normal
completion-page query already selects `thread`; add it to
`RECENT_ROOTS_BY_IDS` so retirement lookups use the same conversion. No database
migration or additional per-row query is needed.

For live rows, the tree-level identity remains authoritative; for standalone
completed rows, use `RunNode.thread_id`. Existing observation, paging and JSON
serialization carry the added optional field without a separate protocol or
API-version change. Payloads that omit it still decode and render `-` for
completed rows. New sources supply the persisted identity after restart or when
the root was never observed live. Preserve existing completion bounds and
read-only observation.

## Implementation touchpoints

- `src/toolang/cli/common/top_view.py`: row projection, column widths, heading,
  values and click positions.
- `src/toolang/execution/schemas.py`, `src/toolang/stats/recent.py` and
  `src/toolang/stats/_sql.py`: additive completed-root identity transport.
- Existing tests in `tests/unit/cli/test_top.py`,
  `tests/unit/stats/test_recent.py`, `tests/unit/up/test_top_observer.py` and
  `tests/unit/api/test_top_api.py`: acceptance coverage below.
- `docs/design/top.md`: document the shipped column and ownership rule.
  Generate any changelog entry through `too aid.too update_changelog`, following
  [repository rules](../../AGENTS.md#public-documentation).

## Acceptance and verification

| Scenario | Pass condition |
| --- | --- |
| Team/agent scopes, Tree/List and snapshot text | `THREAD` is between `AGENT` and `RUN`; every visible row uses its owning root's thread. |
| Multiple roots, threads and sources | Each row retains the correct source/root association through folding, selection and numeric sorting. |
| Live root completes, including retirement outside the first page | Its thread survives replacement by the durable completion without duplicate rows. |
| Offline/restarted observation and additional completion pages | Thread IDs come from stored roots even without a preceding live snapshot. |
| HTTP and JSON round trips; older payload omits the field | New completion identities survive serialization; missing optional identity decodes and displays `-`. |
| Empty data, Unicode/long IDs and resize at 40/80/100/140/200 cells | Headings/values align by cell width, rows stay single-line, styles/footer remain intact and numeric mouse targets match their headings. |

Run the focused acceptance tests, then all default checks required by
[AGENTS.md](../../AGENTS.md#verification) before committing implementation.
For this definition, verify source statements and links and run `git diff --check`.

## Risks and open questions

Full thread IDs increase width pressure; the chosen behavior retains existing
clipping and prioritizes ownership columns. Older sources may lack completed
thread identities until upgraded; show unknown values without extra lookups.
No open product questions remain beyond maintainer approval of this scope.
