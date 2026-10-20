# Top thread column

Approved on 2026-10-20 for implementation in PR #869.

Add `THREAD` between `AGENT` and `RUN` in `too top` and `too AGENT top`,
including Tree/List, `--once` and non-TTY text output.

- Show the full owning root's thread ID on every row, left-aligned like `RUN`.
  Use `-` when unavailable; retain existing clipping, sorting and navigation.
- Reuse `LiveTreeSnapshot.thread_id` for live rows. Carry persisted `runs.thread`
  through an optional `RunNode.thread_id` for completed roots, including pages
  and retirement lookups. No database migration or renderer I/O is needed.
- Touchpoints: `cli/common/top_view.py`, `execution/schemas.py`,
  `stats/recent.py`, `stats/_sql.py`, their existing tests and the current Top docs.
- Acceptance: verify column order and ownership in Tree/List and snapshots;
  completed/offline/paged roots retain their thread; missing fields remain
  compatible; terminal alignment and numeric heading clicks still work.

Full IDs use more terminal width under the existing clipping rules. No open
questions remain. Run the repository's default verification before committing.
