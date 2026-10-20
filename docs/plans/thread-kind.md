# Thread kind derived from identity

Approved by the human on 2026-10-20 for PR #874.

## Goal and scope

Use one source of truth for Thread classification. Remove independent Thread
`origin` storage, constructor parameters, and event payload fields. Keep all
other origin concepts unchanged, except the separately approved rename of
`SourceFile.origin` to `layer` with values `root` and `agent`.

## Contract

- `ThreadKind` has `chat`, `talk`, `chore`, `work`, and `exec`. Creation selects
  `kind`; references and records derive it from the ID prefix.
- Durable Thread IDs require one of those prefixes and a nonempty suffix.
  Reject unknown and obsolete prefixes without aliases or migration.
- Thread responses omit classification fields and `channel`; list filtering
  uses `kind` and status, with no channel parameter. Creation events carry the
  Thread ID without duplicate classification.
- Both `pending` and `running` Runs make a Thread active. Its summary reports
  `running`, retains the actual active Run status, and lets CLI steer/cancel
  reach the existing ownership and invocation checks for either state.
- Fork and rewind accept every Thread kind under the existing terminal-anchor,
  visibility, idle, and concurrent-head checks. Fork preserves the source kind
  without creating Talk bindings or chore definitions. Rewind changes history,
  not message cursors or chore checkpoints. Run ID behavior remains unchanged.
- Increment the execution store schema and reject old databases without writes.

## Implementation and acceptance

Touch execution vocabulary, records, stores, events, read schemas, creation
callers, HTTP filtering, tests, and current design documentation. Rename the
SourceFile field and its source-processing consumers separately within the PR.

Verify all five kinds through persistence, events, HTTP reads, and filtering;
reject invalid IDs before writes; verify branch state checks for all kinds; ensure
neither `origin` nor `kind` is a Thread database column. Existing schema-version
rejection tests cover the unsupported predecessor. Cover pending summaries,
status filtering, and CLI control selection. Verify real Talk batches retain
their binding, receipt, and cursor after fork/rewind and restart; verify a
completed chore retains its checkpoint without creating another definition or
replaying work. New messages and manual chores still use the original Thread.
Remove ignored `root_run_id` and `call_kind` test-helper parameters. Run the
default checks.

## Risks and open questions

This intentionally breaks old execution databases, Thread origin consumers,
creation call signatures, channel consumers, and custom Thread prefixes. The
changelog must name replacement fields and the execution-store reset requirement.
No open questions.
