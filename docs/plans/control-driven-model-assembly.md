# Control-driven ModelCall assembly

## Goal and scope

Make the executor own live ModelCall assembly and record enough facts for crash
recovery and exact replay, without using database reconstruction as the online
execution path. Extend
[message recording](model-message-recording.md) and
[control messages](control-messages-and-context.md) with a compact-result
reference and complete far/near/now assembly.

This PR defines the consumer side: records, control adoption, assembly, replay,
and acceptance tests. Runtime tools, resource-loading triggers, compaction
execution/scheduling, and runspace remain out of scope. Tests supply controls
and compact outputs directly.

## Terms

- **binding:** the effective state, runnable, and input for execution.
- **horizon:** a reference to the adopted compact Run output, or `None`.
- **far:** the summary of the compacted historical prefix.
- **near:** role-preserving history after that prefix, before the current root Run.
- **now:** messages in the current Run's active execution sequence.
- **delta:** new message templates recorded by one Model Step.

## Durable facts

Add horizon to run payloads and introduce a run-scoped compact control:

```python
RunControlPayload.horizon: FieldRef | None = None
CompactControlPayload.horizon: FieldRef
```

Horizon references a compact Run's `output`, whose resolved value is:

```yaml
thread: ThreadRef
begin: RunRef | StepRef | None
end: RunRef | StepRef
summary: Text
```

The summary covers `[begin, end)`; `thread` must equal the target Thread.
This PR supports complete-prefix summaries and root Run boundaries only.
`begin` is `None` or the first logical root; `end` must be a historical root
strictly before the active root, retaining at least one historical root in near.
Partial-summary merging and Step-level boundaries are deferred.

A run control establishes the initial horizon. A compact control replaces its
effective value; neither changes earlier payloads nor copies summary content.
ThreadView determines logical history membership independently. Its `head`
is not part of horizon.

Existing Step/control relations determine when changes take effect:
`preceded_by` records adoption, `aborted_by` records interruption, and
`triggered_by` identifies the originating Tool Step when one exists. Control
creation alone does not imply adoption.

State publications are temporary, watcher-managed data, not replay inputs.
Persist the resolved values used by a call before dispatch:

- Store instructions and tool definitions in `contents`, referenced by hash.
- Capture expanded messages/context in deltas and recalled rules/guidance in
  recall controls, retaining their actual content.
- Record the effective `recall` directive values in the Model Step's call
  references, alongside the already-captured output schema and continuation.

For replay, State revisions are provenance only. Replay never reloads current or
historical State, resolves authored source again, or requires State directories.

## Executor and control-driven execution

The executor holds the effective binding, horizon, resolved far/near/now, and
pending delta in memory. Shared deterministic helpers apply control effects and
render templates; they do not depend on the Store. Persistence records the
executor's decisions and outputs rather than deciding the next ModelCall.

At each Step boundary, the executor applies adopted controls in durable
control-index order. Replay feeds the recorded facts through the same rules in
numeric Step order within each Run. A control affects its target Run: a child's
run/execute/retry must not reset its parent's now. Live State propagation continues
to use each Step's recorded `state` reference, including root reloads first
adopted at a child Step.

Initialize a child's horizon from its parent's effective value when creating
the child run control. Later compact controls affect only their target Run;
an unrelated child's Step must not consume them. Active children keep their
own horizon until they adopt a compact control.

| Control | Binding and history | Message accumulation |
| --- | --- | --- |
| run | Establish binding and initial horizon. | Start now. |
| retry | Preserve the existing retry contract. | Start a new now sequence. |
| execute | Replace binding; retain effective horizon. | Start a new now sequence. |
| reload | Adopt state; refresh affected instructions, resources, context, and recall settings. | Preserve now. |
| compact | Replace horizon. | Replace far/near; preserve now. |
| steer / recall | Adopt input or recalled content. | Append the corresponding messages. |
| cancel | Preserve existing interruption semantics. | Preserve output and its cancellation message. |

Thus controls already determine resets and continuation. Add no bind control,
`previous` chain, reset/append flags, or separate template-selection encoding.
Control changes do not retroactively alter earlier ModelCalls.

## Messages and assembly

Retain the existing versioned message format:

```javascript
delta = {
  version: 1,
  messages: [{role, segments}]
}
```

Segments remain literal strings, Parts, or typed field references under the
existing rendering contract. Templates preserve roles, message boundaries,
Part boundaries, and duplicate occurrences.

Historical messages already have persisted templates and are replayed from
those templates. New messages come from the previous Model Step's output and
subsequent Steps and controls, plus the initiating input for a new sequence.
The Model Step that first assembles them records them in its own delta.
Persisted deltas are never amended or backfilled.

Ownership follows recording, not the referenced fact. If R2's first ModelCall
first assembles R1's final output and cancel, those messages belong to R2's delta
alongside R2's new input. R1's deltas remain unchanged. When a compact boundary
is at R2, these messages stay with R2; do not move them into R1's range, infer
their ownership from their content references, or add origin metadata.

Child internals remain outside the Thread conversation; a parent Tool Step
contributes its result once. Preserve complete tool exchanges when consuming
new outputs and controls across a Run boundary.

Before each ModelCall, determine far and near from the effective horizon:

- With no horizon, far is empty and near starts at the Thread's logical beginning.
- With a horizon, its summary supplies far and its exclusive `end` starts near.
- Near ends before the current root Run; now contains the active sequence's
  accumulated deltas. There must be no uncovered historical range.

Compute far/near before applying recall, including when templates use them.
Omitted/auto recall selects far and near; `none` selects neither; `far`, `near`,
and their combination select the named parts. Now is always included. Selected
nonempty far is a user message; near is flattened in role order. The adapter
receives ordinary messages; far/near/now separators are not serialized.
Instructions, tools, output_schema, and continuation remain separate call fields.

Reuse unchanged visible context, append necessary updates, and keep tool
exchanges complete. Do not deduplicate inputs by text equality or proactively
reload guidance. Preserve unchanged message prefixes as now becomes near.

## Preparation and Model Step begin

Preparation computes a candidate request without allocating a Model Step,
consuming controls or pending delta, or invoking the adapter. It may be discarded
and repeated when controls arrive.

At actual Model Step begin, fix the adopted controls and request. Referenced
outputs and control content must already be durable; commit the Step's adoption
relations, call references, and delta together. Only after that commit succeeds
advance the live buffer and invoke the adapter. A failed write must not consume
pending delta or dispatch the call. Later calls consume model/tool outputs only
after those outputs are persisted, using their already-known in-memory values.

Creation/application of a runtime control and adoption by a Step are distinct
durable facts. Recovery must retain available, unadopted controls from the active
attempt, including controls already marked applied; it must not rely only on a
pending-status query or a lost in-memory preceding-control queue.

An actual user cancellation still follows the existing begin-to-canceled-end
boundary, including before the first call. Discarded preparation is not a
canceled Model Step.

Future compaction fits between preparation and Model Step begin: a real runtime
Tool Step performs the work, persists its output, and creates a compact control.
After the wait, prepare again using all applicable controls, including any
reload, steer, or cancel received meanwhile. This PR consumes such controls but
does not implement that tool or fabricate model-emitted ToolCalls/results.

## Persistence, recovery, and replay

Live execution renders additions once and reuses resolved prefixes. It must not
write records and immediately reread them to determine the next call, rescan the
Step/control history each time, or rerender unchanged messages. Reads for initial
history, recovery, external controls, or changed history/resources remain valid.
An exception that leaves commit success uncertain also permits a targeted read:
honor an existing committed begin rather than emitting or consuming it twice.
This exception path is not the normal assembly path.

Store/RunHistory supply durable facts and resolve references for recovery and
replay. The same control and rendering rules use in-memory values online and
persisted values offline. Replay stops at the requested Model Step and restores
its normalized adapter request, including tools and recorded tool exchanges;
it never invokes the model or tool handlers.

After a process crash, reconstruct committed assembly state from records for
inspection and existing recovery/retry behavior. Before a begin commit, a
candidate call does not exist; after it, the recorded request is reproducible
without volatile buffers. Do not infer that an in-flight external operation
completed or automatically repeat it as part of replay.

Use recorded templates, their format versions, and captured binding values, not
current runtime wording, State directories, or the latest compact output. Do not
copy historical bodies into every Run or ModelCall, or introduce a second
database-driven assembly policy.

Preserve existing retry behavior and its physical-deletion policy. Assume users
do not retry or alter referenced history in ways that invalidate replay; add no
retry restrictions, MVCC, reference snapshots, or repair mechanisms for that
case. Missing references fail explicitly. Increment the store schema for the
new record encoding and reject incompatible databases without modifying them.

## Implementation and acceptance

Touchpoints: executor state/preparation/message buffers, shared assembly helpers,
records/codecs, Store/RunHistory, and offline tests. The main risks are adoption
timing, commit boundaries, cross-Run terminal messages, and live/replay divergence.

- Compare captured adapter requests with replay after restart and changed runtime
  wording, including roles, Parts, ordering, duplicates, and unchanged prefixes.
- Remove State cache directories before replay; instructions, resources, recall
  selection, and messages must reconstruct entirely from execution records and
  `contents`, including calls made before and after reload.
- Cover empty horizon, initial compact output, later compact adoption, wrong
  target Thread, incomplete coverage, at least one retained historical root,
  every recall selection, and unchanged recall with changed far.
- Cover run/retry/execute resets, reload/compact continuation, mixed steer/recall,
  cancellation, complete tool exchanges, child isolation, and fork/rewind views.
- Cover root controls first observed by child Steps, inherited child horizons,
  and compaction while children are active without cross-Run resets/adoption.
- Cover terminal messages first recorded by the next Run: prior deltas remain
  unchanged, new messages belong to the recording delta, and a compact boundary
  at that Run retains them without splitting by the referenced fact's owner.
- Prepare repeatedly while controls arrive: no phantom Steps, premature
  consumption, lost input, duplicate messages, or stale committed requests.
- Inject failures before/after Model Step begin commit and between output
  persistence and the next begin; verify reconstruction from a fresh process
  without duplicate adoption or replay invoking model/tool handlers.
- Cover control persistence before Step adoption and commit-before-acknowledgment
  failures, retaining available controls without emitting another begin.
- Instrument a multi-call run: unchanged prefixes are not reread or rerendered,
  and freshly persisted outputs are consumed from memory. Compare every online
  request with independent reconstruction from persisted records.
- Verify earlier calls remain unchanged by later compact adoption; unresolved
  references and incompatible databases fail explicitly.
- Run Ruff check/format, ty, and the complete offline pytest suite.
