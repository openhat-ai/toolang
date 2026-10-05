# Define root spawning from flow and agic

Status: Proposed runtime definition; full human approval is still required.
No implementation changes in this PR.

## Goal and Scope

Let flow and agic start an independent root in a new empty thread under the same
agent and executor, receive a durable admission receipt, and continue without
waiting. Both surfaces share admission, ownership, and persistence rules.

The merged [grammar definition](https://github.com/openhat-ai/tree-sitter-toolang/pull/48)
owns syntax, CST, keywords, and highlighting. Implementation requires its
published grammar and the [call/array simplification](flow-array-semantics.md).
[Async run/await](https://github.com/openhat-ai/toolang/pull/685) and
[await blocks](https://github.com/openhat-ai/toolang/pull/686) are later work, not
dependencies. Exclude Future types, completion messages, thread selectors,
implicit forks, cross-agent dispatch, detached processes, restart/resume, and new
CLI/API flags.

## Verified Baseline

- Flow `run` waits and binds complete output. Agic `_toolang/run` returns
  `{run_id, controls: [entry_control_ref]}`, then waits and injects a separate
  `toolang:run-result` message. The receipt contains no result or output type.
- `RunExecutor.run` starts roots with `parent=None`; its Python handle is not a
  language value. `stop()` cancels and drains all roots owned by that executor.
- Thread history includes active predecessors; compaction and cwd defaults are
  thread-scoped. Fork requires a terminal anchor; rewind changes visible history
  without deleting physical records.
- Store admission derives control `triggered_by` from the execution parent.
  Atomic child receipts support model Tool Steps only; thread creation has a
  separate transaction. Spawn must extend these boundaries.

## Invocation and Receipt

```too
let job = spawn investigate
spawn record_audit
spawn -> Text: Research {{_}} and save the findings.
```

Flow reuses ordinary `run` input binding and validation: declared `_` and named
parameters come from matching locals; inline agic captures referenced locals.
Capture inputs before updating any binding. Unused locals are not arguments.
Inline `-> T` describes the target's eventual output, not the receipt.

Agic gains `_toolang/spawn` (wire name `_toolang__spawn`), with the same schema
and decoder as `_toolang/run`:

```json
{"runnable": "agic:investigate", "input": {"_": "Compare the proposed designs"}}
```

Require a runnable reference; default omitted input to `{}`. Named targets may
be flow or agic. Do not infer arguments from conversation history or accept
additional fields for thread, identity, source code, or execution configuration.

Both surfaces produce this exact ordinary Json receipt:

```json
{
  "run_id": "<RunRef>",
  "thread_id": "<ThreadRef>",
  "controls": ["<thread CreateControlRef>", "<entry RunControlRef>"]
}
```

References are canonical; controls identify index-0 thread creation and index-0
root admission, in that order. The receipt acknowledges committed admission
under executor ownership, regardless of whether the root has already finished.
It contains no live task, mutable status, result, or authority grant.

| Invocation | Immediate binding and continuation |
| --- | --- |
| `spawn R` | Preserve every local, including `_` |
| `let job = spawn R` | Bind Json receipt to job only |
| `let spawn R` | Preserve every local; format canonically as `spawn R` |
| Agic `_toolang/spawn` | Return receipt in ToolResultPart; continue without waiting |

Persist receipts even when unbound. Bare and nameless-let forms lower to
`SpawnStmt.binding=None`; restoration preserves that rule. A final unbound
spawn leaves the flow output unchanged; ordinary output validation still applies.
Admission rejection preserves locals. Losing a handle does not cancel its root.
Only named let retains a local handle for direct use or later await support.

Existing inspection supplies input, runnable, Steps, error, usage, and controls.
`history/read_output` supplies a nonblocking status/output snapshot, possibly
partial or null. Existing authorized host controls address the root by run ID;
spawn grants no history access and adds no model control or flow polling operator.
Thread ID enables navigation; child-run receipts remain unchanged because they
create no thread. Later async/await must reconcile #685's Future typing with
these durable references, validate them, and await without relaunching work.

## Authorization and Integration

- Add `ToolRuntime.spawn(runnable, input)` and the stateless runtime-tool leaf
  through the existing factory. Flow and the tool call one execution-owned
  admission operation; plugins receive no executor/store access.
- Reuse hands for agic spawn: `none`, explicit targets, `*`, and default
  `requested_only` mean the same as for run. Extend route metadata and guidance;
  handoffs still govern exec. Preserve module visibility, generated-inline and
  repair restrictions, runtime-tool availability, and call-time authorization.
  Authored flow uses ordinary flow call authorization.
- Resolve once using ordinary named live-State compatibility checks or pinned
  inline code. Validate/capture input values and provenance before admission.
  Reject the current or an ancestor runnable on the active execution path, as
  run does; causal spawn links do not extend that path into other roots.
- Spawn may share an ordinary tool-call batch. Each call commits independently;
  interruption skips unstarted calls without undoing accepted roots. Preserve
  exec/chdir singleton rules. Never set the scheduled-child slot or inject a
  child completion message, including during history reconstruction.

## Thread and Context

Always create a new empty thread, including for nested spawn. Use a `spawn`
ThreadPrefix, ordinary chat origin, and
`ThreadPeer(type="agent", name=current_agent_name, thread=source_thread_id)`.
The peer records provenance only. Keep normal inspection and explicit fork
behavior; do not switch the foreground thread or send messages to the source.

| Alternative | Reason to defer |
| --- | --- |
| Current thread | Shares active history, compaction, and later cwd defaults; a horizon does not freeze active predecessors |
| Implicit fork | Requires a chosen terminal anchor and frozen history prefix |
| Arbitrary existing thread | Requires destination authority and concurrent-history rules |

An empty thread gives independent history and an explicit input boundary. The
caller supplies context through arguments/captures; run and later async run
compose work within the current task.

The root has a new identity and `parent=None`, in the same agent/host. Capture
Setup, compatible resolved State, concrete model request, cwd/workspace bindings,
effective limits, and inherited settings; apply target settings within those
limits. Materialize effective resource restrictions as root ceilings so removing
ancestry cannot broaden authority.

Start with no history prefix and `horizon=None`. Do not copy messages, summaries,
or repeat frames; inherited recall operates on the new thread. Capture available
iteration values as data; reject unavailable outer-frame dependencies before
admission. Later local assignments, chdir, compaction, and State publications
cannot change accepted inputs or entry context. Files/workspaces remain shared.
Each root gets fresh usage counters under copied limit values; no aggregate
budget or concurrency pool is introduced.

## Lifetimes

Thread stores history; run owns execution state; executor owns live tasks.
Spawn creates a causal relationship with its source, not lifecycle ownership.

| Event | Effect on spawned root |
| --- | --- |
| Source returns, fails, is canceled, executes a handoff, or loses its handle | Continue under the same executor |
| Root completes, fails, or hits its limit | Record its own outcome; do not fail or resume the source |
| Root is explicitly canceled | Drain its execution children; leave other roots alone |
| Executor stops | Reject admissions and cancel/drain all owned roots |
| Host crashes | Work stops; records may remain pending/running, with no automatic resumption |

Serialize admission/registration with shutdown: no committed root may be missed
by the executor's drain. After commit, ownership and cleanup survive interrupted
receipt delivery; a dispatch failure records a failed root. Route events by the
new root/thread through host observation, without adopting the source foreground
tracer or changing its interrupt target.

The Script CLI stops its executor when the invocation exits, canceling unfinished
spawned roots. Local Chat keeps its executor across turns until session close;
AgentCore keeps it until host shutdown. A client/view disconnect does not transfer
ownership. No implicit join keeps a short-lived host alive. Records survive;
opening their store in another executor does not adopt or replay them.

## Persistence and Recovery

Atomically commit the thread/create control, root/run control, self-contained
entry State/context, and source Step receipt. Both controls use `triggered_by`
pointing to the originating Step in the same agent store, even across threads;
the root parent stays null. Separate parent from origin in shared admission
helpers. Add no new ControlKind, synthetic caller control, or scheduler.

Flow records a run-kind Step with SpawnStmt, Json receipt output, and its optional
named binding; agic records a normal Tool Step/ToolResultPart. Preserve typed
inputs with the ordinary codec and keep retained references resolvable.
Do not split thread creation and root admission into separate commits.

Use the source run/physical Step/admission occurrence as the stable request
identity. Reprocessing a committed occurrence returns its existing receipt;
conflicting specifications fail. New loop occurrences and whole-run reruns may
launch new roots. Cancellation/rejection before commit creates nothing. Recovery
never relaunches an accepted root without a live owner; this is admission
deduplication, not exactly-once external effects or restart recovery.

Retry/prune must reject, before mutation, deletion of an origin or retained input
reference needed by a surviving independent root, with guidance to use rerun.
Never prune that root as a child. Rewind may hide the source while retaining its
physical records and references; it must neither cancel nor relaunch the root.
Keep historical records and ordinary child retry behavior compatible.

## Implementation Touchpoints

Paths below are relative to `src/toolang/`.

| Area | Likely files and changes |
| --- | --- |
| Language | `lang/{ast,lower,contracts,flow_validation,format,description}.py`: CST, binding defaults, Json inference, diagnostics, prepared-cache compatibility |
| Execution | `execution/executor/{executor,common,frame,tool_runtime}.py`, new `execution/executor/stmts/spawn.py`: shared admission, context, ownership, binding |
| Runtime tools | `execution/tools/_toolang.py`, `base/protocols/tool.py`, `execution/runnables.py`, assembly guidance/result matching: registration, hands, receipt-only continuation |
| Persistence/hosts | `execution/{store,threads,records,events,types,schemas}.py`, inspection/history and host observers: atomic admission, provenance, retention, event routing |

Pin the published grammar, update examples/generated references, and generate
the implementation changelog through the repository runnable. This definition
changes no dependencies, product code, or changelog.

## Acceptance Tests

1. Cover the upstream grammar contract, all bindings, named/inline targets,
   canonical formatting, Json inference independent of target output, and cache
   compatibility. Malformed `let text = spawn a process` must error, never become
   text or launch work.
2. Match run input validation, inline captures, State resolution, hands policies,
   visibility, and ancestry checks; reject authority/configuration arguments.
3. Gates prove receipt delivery without awaiting completion on both surfaces.
   Preserve unbound locals in final position, immediate completion, and replay;
   named let changes only its destination. Preserve batches and child receipts
   and waits; inject no spawn completion online or during history reconstruction.
4. Verify thread/root IDs, null parent, control/peer provenance, empty history,
   copied context/limits, fresh accounting, and no authority widening. Cover
   nested callers, iteration captures, and later source changes.
5. Exercise the lifetime table, Script/Chat/AgentCore ownership, admission racing
   stop, and foreground event/interrupt isolation. Inspection must expose durable
   outcomes without waiting or granting authority.
6. Fault-inject creation, commit, registration, and delivery for both Step forms:
   no orphan thread, one receipt per committed occurrence, explicit dispatch
   failure, no ownerless relaunch. Verify conflicting requests, new loop/rerun
   launches, and old-record compatibility.
7. Reject destructive cuts that invalidate surviving roots; permit nondestructive
   rewind with references intact. Preserve receipts, bindings, and child retries.

Use offline fake providers/gates and all default checks for implementation.
This definition requires implementation review, link checks, and `git diff --check`.

## Risks and Approval

Main risks: authority widening, duplicate admission, accidental child completion
delivery, dangling references, and confusion between run and host lifetimes.
Independent budgets can multiply work; shared files still permit write races.
Approval covers Json receipts, hands authorization, new empty threads, inherited
authority without history, and executor ownership. Later handle typing/waiting
remains the integration question for #685; it does not block this launch contract.
