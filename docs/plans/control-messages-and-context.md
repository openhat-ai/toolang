# Control messages and cancellation

## Goal

Make user intervention visible to subsequent model calls, preserve complete
tool exchanges, and stop shell commands when immediate cancellation is applied.
Keep runtime context independent of run/thread identity and reuse it while visible.

## Scope

- Render applied steer, cancel, and recall controls in execution order. Keep
  multimodal inputs intact and store the rendered structure in message deltas.
- Use `<steer description="...">input</steer>` and
  `<cancel description="The user canceled this run."/>`; an explicit cancel
  reason becomes its body. Do not add run IDs to either tag.
- Recall uses `<rules workspace="..." path="..." revision="...">`,
  `<skill ref="..." revision="...">`, or
  `<service ref="..." revision="...">`. Escape attributes.
- Run/execute provide runnable input; retry resumes execution; reload changes
  state. Create/fork/rewind select history. None needs an additional lifecycle
  message. Pending/revoked/unapplied controls produce no message.
- Complete a canceled tool exchange with durable cancellation results before
  the cancel/steer message. Do not imply rollback of completed side effects.
- Remove default run/thread identifiers from model-facing instructions/context.
  Keep the live message prefix and append context only when changed; a fresh
  sequence supplies context again when history does not carry it. Never
  deduplicate user input or infer runtime metadata from user-authored text.
- Cancel the shell process group and reap it on cancellation or timeout.
  Preserve the explicit distinction between immediate and next-step controls.

No new tools, recall triggers, compaction algorithm, or history storage format.

## Touchpoints and acceptance

Executor message assembly, history projection, bundled prompts, shell plugin,
and their execution/plugin tests:

- Cancel partial text or a running tool; the next run sees the partial output,
  complete tool exchange, and cancel fact in that order.
- Immediate steer interrupts a shell command; next-step steer does not.
- Cancel before any output, tool batches, repeated interruptions, and child
  execution preserve terminal facts and control order.
- Cancellation after a committed final Step remains a terminal Run fact; do
  not rewrite the completed Step or revive cancellations from earlier retries.
- Recall target attributes and multimodal steer content survive exact replay.
- Unchanged context is not repeated while visible; changed/omitted history
  restores context. Identical successive user inputs remain separate.
- Stored deltas reconstruct the exact submitted calls after restart.

Prior-call history assembly and cross-run context reuse remain separate work.
The principal risks are splitting tool exchanges and losing process ownership
during interruption cleanup.
