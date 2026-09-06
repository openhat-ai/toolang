# Execution history (PR4)

## Goal and scope

Provide modular, bounded execution-history reads for inspection, compaction,
and ModelCall assembly. Keep `RunHistory` and `RunStore` as the public names.

PR4 adds the record-reading foundation, Views, pagination, and lightweight
output reads. Preserve existing CLI/API behavior. Do not change ModelCall
assembly or message persistence, add runtime tools, trigger recalls, or execute
compaction. No record schema change, MVCC, message log, or HistoryBlock abstraction.
Later contracts below describe integration requirements, not PR4 runtime work.

## Responsibilities and methods

`RunStore` owns database queries, reference resolution, and transactions.
`RunHistory` coordinates reads and constructs fixed-scope Views. Views interpret
supplied records without database access, model budgets, or message generation.
RunExecutor remains responsible for execution; ThreadManager for create/fork/rewind.

Use output for the execution artifact and Run detail for inspection data.
Keep Run selection separate from output reading.

| RunHistory method | Change | Responsibility |
| --- | --- | --- |
| `list_threads`, `list_runs` | Keep | Filtered lists and summaries. |
| `describe_threads`, `describe_runs` | Keep | Batched summaries of selected records. |
| `get_thread`, `get_run` | Keep | Existing inspection details. |
| `thread_view(thread, ...)` | Add | Read a fixed logical Thread scope. |
| `run_view(run, ...)` | Add | Read a fixed Run/Step scope and required dependencies. |
| `next_page(cursor)` | Add | Continue the captured read without repeating its target. |
| `get_output(run)` | Add | Return the resolved typed output, without Steps, controls, or ModelCall reconstruction. |
| `get_model_call(step)` | Add facade | Reuse the existing stored-call reconstruction. |
| `get_compaction(thread)` | Add reader | Locate the paired compact Thread's latest successful output and its reference; no execution. |

The initial read sets bounds and page limits; continuation retains that scope.
Pointer inspection and physical execution-tree queries retain existing Store APIs.

Both View reads accept `begin`, `end`, `limit`, and `reverse`. Defaults read the
whole scope; a positive limit pages it. `reverse` selects from the tail, but each
page retains natural order. A View's optional `cursor` continues through
`next_page(cursor)`. Cursors contain membership and lifecycle markers, not bodies,
and work with a reopened read-only Store.

Thread pages count root Runs and include their child membership. Run pages count
Steps in numeric order followed by raw owned controls. Required controls also
appear in `RunView.dependencies`, separately from paginated `entries`; these are
references' supporting facts, not extra messages. `timeline()` describes Step
boundaries in the selected page, not a global ordering of raw record pages.
Limits count primary records, not bytes; callers account for dependencies and
oversized individual records in their own budgets.

Replace `get_run_result` with composition of `get_run` and `get_output` where
resolved output is needed in a detail response. Reuse an already-read detail;
do not rebuild it. `get_run` retains its existing stored-output representation.
`get_output` returns None for an existing Run without output and raises KeyError
for a missing Run; it does not flatten typed output into message Parts.

Remove `latest_thread_result`: select through `ThreadView.runs(reverse=True)`,
then read output or detail as needed. Thread-latest callers retain their selection
policy: the latest succeeded visible root with nonempty rendered output. Preserve
existing CLI/API names, response shapes, and errors, without retaining result-named
aliases in RunHistory.

| View method/property | Responsibility |
| --- | --- |
| `ThreadView.record`, `head` | One Thread and its captured logical-view position. |
| `ThreadView.runs(reverse=False)` | Visible root Runs in either traversal order. |
| `ThreadView.contains(run)` | Logical membership. |
| `ThreadView.tree()` | Visible roots and child Runs for structural inspection. |
| `RunView.record` | The physical Run. |
| `RunView.steps(reverse=False)` | Steps in the selected scope, ordered by numeric StepRef components. |
| `RunView.controls()` | Raw controls, including unconsumed and terminal controls. |
| `RunView.timeline()` | Consumption/interruption relationships as execution facts, not Messages. |
| `RunView.messages()` | Later: saved message templates selected through recorded call references. |
| `RunView.tail()` | Later: facts beyond the selected recorded-message cutoff. |

ThreadView replaces ThreadViews' multi-Thread interface. Shared pure resolution
still serves Store mutation checks; cross-Thread fork protection stays outside
a single View. RunView is new.

## Three reading routes

- **Inspection:** preserve lists, details, all control statuses, partial outputs,
  errors, pointer fields, child/Step structure, physical ownership, logical
  membership, and the actual recorded model/tool calls. Do not filter raw facts
  through a conversation-only policy.
- **Assembly:** read saved ModelCall message templates, not a fresh interpretation
  of old Steps/controls. Traverse root Runs from newest to oldest when choosing
  context, then emit selected messages in their original order.
- **Compaction:** traverse the selected Runs and their Steps forward; read raw
  facts, related controls, and outputs without rebuilding every ModelCall.
  Their presentation and chunking need not match the original message sequence.

These routes share record selection and resolution, not message-generation
rules. Child internals enter structural inspection or explicit child reads, not
the parent conversation. Do not duplicate a parent tool result with child output.

`timeline()` follows Step relations: preceded_by before begin, output at end,
and aborted_by after the interrupted output. Keep input dependencies separate.
A control referenced at both interruption and later adoption remains one fact
with both relations, not two independently generated user messages.

## Ranges, pagination, and budgets

- Use half-open ranges. Thread selection currently uses root Run boundaries;
  RunView supports bounded Step reads. A future context horizon inside a Run
  must also respect legal message/tool-exchange cut positions.
- Pagination is not context truncation: record pages may cross an exchange.
  Preserve references and order; do not invent results or treat cancellation
  alone as evidence of missing records.
- Capture read membership and its upper bound. Appends and later rewind do not
  alter an existing paginated read. If retry deletes/replaces dependencies, or
  other required facts change, fail that read explicitly rather than mixing
  versions. Continuations work after restart without a long-lived transaction.
- Read content incrementally, not all execution detail before returning page 1.
  Page limits belong to the reader; model-cost estimates, retention budgets, and
  stopping decisions belong to the consumer. No universal 256 KiB policy.

## Recorded messages and cache stability (later assembly work)

A message template is a role plus content containing fully qualified pointers.
Bind actual objects, not a relative Run or "latest" template. Preserve Parts
when expanding pointers.

```text
one recorded template -> one actual Message
same count, order, roles, and expanded content as that ModelCall
```

Reuse unchanged templates/sequences by reference; never concatenate full calls
or deduplicate by content equality. Append-only storage works only while the
prefix is reused. Runtime changes must record the new sequence without changing
old calls' saved representation.

Read all selected history against an explicit recorded-call basis, not a mixture
of each Run's independently chosen latest call. New tail facts are assembled by
the current runtime and recorded with that call. Storage location or a content
pointer's target does not by itself determine a message's sequence position.

With an unchanged summary, moving a Run from now into near must preserve the
message prefix. Do not serialize near/now separators, regenerate historical
context, or replace completed Runs with final-output-only messages.

```text
far  = summary covering [begin, end)
near = history from end to the current root Run
now  = current Run facts adopted before this ModelCall
```

For full-prefix context, begin is the Thread start. Assembly must not silently
omit history between summary.end and near. Root-only compaction can retain the
latest historical root Run as the new exclusive end, leaving room for growth.
The trigger threshold and post-compaction retention target are distinct; now
also consumes budget. Failure to fit the minimum retained context is explicit.

## Compaction Run input/output contract

Compaction uses the following summary-and-coverage shape in Run input/output:

```yaml
thread: ThreadRef
begin: RunRef | StepRef | None
end: RunRef | StepRef
summary: Text
```

The metadata describes coverage `[begin, end)`, independently of ThreadView
structure or read snapshots. None means coverage from the Thread start; an
explicit start is also allowed. Keep fully qualified references; do not resolve
None to a captured first Run or add head/view metadata. Compaction execution
remains deferred.

Explicit begins permit segments such as `[None, r3)`, `[r3, r6)`. A segment is not
automatically a complete far. Summary selection and merging belong to consumers,
not ThreadView. Future compaction reads facts through `_too/history` using
thread/begin/end; that tool and existing runtime-tool changes are deferred.

## Touchpoints and verification

Implement record reads and coordination in `execution/history.py` and Store;
extract pure `thread_view.py` and `run_view.py` with tests. Preserve transactional
fork/rewind/retry checks. Keep schemas and errors in their owning modules.
Adapt result consumers in the Run/Thread API routers and local chat backend to
the consolidated methods without changing their public behavior.

Acceptance covers existing inspection responses, nested fork/rewind membership,
numeric Step order, control consumption/interruption relations, partial output,
child isolation, page concatenation, bounded loading, appends/rewind/retry during
paging, and restart reconstruction. Output-only reads must not rebuild calls.
Verify resolved typed outputs, missing Runs, absent outputs, and unchanged latest
output selection and detail responses. Test the compaction output reader using
persisted fixtures, without executing tools.
Existing ModelCall assembly and exposed runtime tools must remain unchanged.

Current storage has full per-call message-hash lists, not the template coverage
and reuse relations needed by messages()/tail(). Define their physical encoding
in the message-persistence follow-up; do not guess slices or ship placeholder
methods in PR4. Existing ephemeral runtime results remain a separate deferred
gap; the reader must not claim they were durably recorded.

Run Ruff check/format, ty, and the complete offline test suite before committing.
