# Bounded execution history (PR4)

Status: proposed definition; implementation awaits approval.

## Goal and scope

Read durable execution facts in consumption order through one bounded history
API and `_too/history`. Reopening the store must reproduce the same page.
Do not change ModelCall assembly, stored ModelCall inputs, recall triggers,
compaction, or existing CLI/API detail responses. No MVCC or new message log.

Today, `RunHistory` provides full details and output resolution, but no paging.
`recent_conversation_messages` scans run/steer controls before Step outputs and
includes child Runs. Leave that legacy ModelCall reader unchanged in this PR;
the later assembly PR will adopt the new reader.

## Reading contract

- `RunHistory.read(target, mode, cursor, limit)` accepts a ThreadRef or RunRef.
  `mode` is `history` or `output`; the caller supplies concrete defaults.
- Thread history follows the logical root-Run view, including fork prefixes and
  excluding rewound Runs. Run history reads only that physical Run. Neither
  expands child internals; callers can read a child explicitly by its RunRef.
- History returns ordered groups of source-labelled entries: consumed control
  payloads and message-bearing Step outputs. Retain Part types, Step/control
  references, and interruption facts; do not render future user-message XML or
  reconstruct stored ModelCalls. This is history data, not adapter-ready input.
- Output mode returns resolved Run outputs, one entry per Run, without listing
  Steps or rebuilding ModelCalls; follow output FieldRefs when needed. For a
  Thread, use the same logical root order.
  Include terminal status and distinguish absent output from an empty value.
  Pending/running Runs are not output entries.
- `_too/history` accepts `target`, `mode`, `cursor`, and `limit`. Default target
  is the current Thread, mode is `history`, and limit is 20 groups (1–100).
  Targets are confined to the current Agent's store; no filesystem selectors.
  Return groups plus `next_cursor`, or a structured error.

## Order and grouping

```text
logical root order
  Step order (numeric StepRef components, not string order)
    preceded_by controls
    Step output
    aborted_by control
```

- Resolve only referenced controls; pending, rejected, revoked, or unconsumed
  controls do not become history entries. Emit each consumed control once per
  Run. An immediate steer first appears after the interrupted output, not again
  when the next Step adopts it. Keep input data dependencies separate.
- Retain run/steer/cancel/recall payloads as distinct facts. Retry, reload, and
  execute remain source metadata rather than invented user messages.
- Include persisted partial assistant output. Do not synthesize missing output,
  finish partial ToolCalls, or execute anything during reading.
- An assistant ToolCall batch and all its associated ToolResults form one
  indivisible group. Correlate using originating Model Step and call IDs, not
  global call-ID uniqueness. Keep intervening control entries in that group.
- Exclude an open exchange from the frozen readable prefix. If execution ended
  without all results, return the available facts as an explicitly incomplete
  group; do not silently drop them or fabricate results. Orphan results are
  likewise diagnostic facts, never presented as a complete exchange.
- A parent `_too/run` result appears once. Do not also append the child's output
  or the parent's aggregate Run output to its Step history.

## Fixed-range pagination

- The first read freezes the logical view and readable upper boundary in one
  store read transaction. Pages move forward through that range only. A fresh
  read is required to see later appends or completion of an open exchange.
- A versioned opaque cursor identifies the target, mode, captured Thread head,
  selected Run range, Step boundary, retry identities, and next group. It stores
  references, not message copies. Later rewind does not change its captured
  membership; nested fork prefixes retain their existing projection semantics.
- Retry can physically delete and reuse Step IDs. Validate captured execution
  identities before resolving a subsequent page; if a selected execution was
  retried or a referenced record is gone, return `stale_cursor`. Do not mix
  attempts. A conservative invalidation for any retry in the selected range is
  acceptable; no generation fields or historical versions are introduced.
- A page contains at most `limit` groups and 256 KiB of serialized JSON, including
  its cursor. Stop before a group that would exceed the byte limit. If one group
  or the cursor alone cannot fit, return `history_too_large` with source refs;
  never split or silently truncate content. Malformed or mismatched cursors are
  errors. An empty range has no continuation.
- Each page resolves its records in one read transaction. No transaction or
  in-memory session survives between requests; cursors work after restart.

## Implementation and acceptance

1. Add the source projection and bounded reads in `execution/history.py`, with
   snapshot queries in `store.py` and caller schemas/errors in their owning
   modules. Reuse ThreadViews and existing Ref codecs; no store schema change.
2. Register `history` in `execution/tools/runtime.py` and dispatch it through the
   ordinary runtime Tool Step lifecycle in `executor/runs/agic.py`. Persist both
   success and error results. Also persist the currently ephemeral `_too/run`
   result and unknown-runtime-tool rejection as ordinary Tool Step results,
   preserving child execution and execute's control-transfer semantics. The
   reader must not reconstruct tool responses from child outputs.
3. Extend execution history and runtime-call tests. Cover ordered multi-steer,
   cancel before output and after partial output, recall ordering, numeric Step
   ordering, complete/incomplete exchanges, and parent/parallel-child isolation.
4. Cover page concatenation versus a single read, exact byte/group limits,
   append/completion/rewind between pages, nested forks, retry invalidation,
   concurrent retry/read, reopened stores, output-only without ModelCall reads,
   invalid cursors/targets, and persisted runtime success/error results.
   Guard existing ModelCall assembly semantics; only the newly available tool
   changes the exposed runtime-tool list.

Run Ruff check/format, ty, and the complete offline test suite.

## Approval points and risks

Approve explicit stale cursors after retry, diagnostic incomplete groups, and
the fixed response-size ceiling before implementation. These keep paging honest
without MVCC or adapter-specific repair. Large indivisible outputs may require a
later field/content reader; this PR reports that limitation explicitly.
