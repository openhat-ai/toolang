# Define root spawning from flow and agic

Status: Proposed runtime definition; full human approval is still required.
No implementation changes in this PR.

## Goal and Scope

Let flow and agic start an independent root in a new empty thread under the same
agent and executor and continue without waiting. Flow produces a typed future;
agic receives an admission receipt. Both share admission and ownership rules.

The merged [grammar definition](https://github.com/openhat-ai/tree-sitter-toolang/pull/48)
owns syntax, CST, keywords, and highlighting. Implementation requires its
published grammar and the [call/array simplification](flow-array-semantics.md).
This runtime contract supersedes #48's provisional Json handle typing.
[Async run/await](https://github.com/openhat-ai/toolang/pull/685) and
[await blocks](https://github.com/openhat-ai/toolang/pull/686) remain later work.
Include the shared Future value contract so spawn's return type stays stable;
whichever launch feature lands first supplies it, without depending on waiting
behavior. Exclude completion messages, thread selectors, implicit forks,
cross-agent dispatch, detached processes, restart/resume, and new CLI/API flags.

## Verified Baseline

- Flow `run` waits and binds complete output. Agic `_toolang/run` acknowledges
  admission, then waits and injects a separate `toolang:run-result` message.
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
Inline `-> T` describes the target's eventual output; the launch value is
`Future<T>`.

Agic gains `_toolang/spawn` (wire name `_toolang__spawn`), with the same schema
and decoder as `_toolang/run`:

```json
{"runnable": "agic:investigate", "input": {"_": "Compare the proposed designs"}}
```

Require a runnable reference; default omitted input to `{}`. Named targets may
be flow or agic. Do not infer arguments from conversation history or accept
additional fields for thread, identity, source code, or execution configuration.

The spawn tool returns this fixed admission schema, expressed as a Toolang struct:

```too
struct SpawnReceipt:
  run_id: Text
  thread_id: Text
  controls: Text[]
```

SpawnReceipt names a protocol schema, not a new built-in type for scripts.
Validate its fields using ordinary struct rules. Text fields hold canonical
references; controls contains the thread create control followed by the root
entry run control (both index 0). It confirms admission, not target success.

Agic `_toolang/spawn` returns these fields in ToolResultPart.output and continues.
The visible run_id lets agic identify newly started work in its summary and use
existing inspection while that work is still running. Preserve the same IDs in
stored replies and reconstructed model history.

Keep `_toolang/run`'s existing `{run_id, controls}` reply and completion message
unchanged; this feature needs no named RunReceipt or shared receipt type.
Flow `run`, `async run`, and `spawn` expose values below, not receipts.

Existing inspection supplies input, runnable, Steps, error, usage, and controls.
`history/read_output` supplies a nonblocking status/output snapshot, possibly
partial or null. Existing authorized host controls address the root by run ID;
spawn grants no history access and adds no model control or flow polling operator.
Thread ID enables navigation; child-run receipts remain unchanged because they
create no thread.

### Flow Values and Binding

Let `T = Return<R>`, the complete output type of target runnable R:

| Flow statement | Value type | Available when | Default binding |
| --- | --- | --- | --- |
| `run R` | `T` | Child succeeds | `_` |
| Later `async run R` | `Future<T>` | Child is admitted | None |
| `spawn R` | `Future<T>` | Independent root is admitted | None |
| Later `await job`, where job is `Future<T>` | `T` | Referenced run succeeds | Replace job |
| Later `await:` block | `T[]` for homogeneous children, otherwise `Json[]` | All children succeed | `_` |

Named let binds that statement's value to its destination only; nameless let
discards it. Thus `let result = await job` retains job when the names differ;
`let await job` also retains job. Only success changes bindings. A retained future
can be awaited again; after replacing job with `T`, another await is a type error.

Bare and nameless-let spawn preserve every local, including a final statement's
flow output. Both lower to `SpawnStmt.binding=None` and format as bare spawn;
restoration preserves those rules. Only named let retains a local future.
Admission rejection preserves locals; losing a future does not cancel its root.

`Future<T>` is explanatory/static notation; the language value is Future, with
`T` tracked as its result contract. Reuse #685's Future value/codec and ordinary
data-use restrictions. The first launch implementation supplies the shared type;
the other reuses it. Spawn implementation creates/restores futures without await.

Future references the eventual business result; receipt records admission
effects. Neither contains the result or grants authority. Await accepts Future,
never SpawnReceipt, and does not launch work. If R returns `Text[]`,
its future is `Future<Text[]>` and await returns `Text[]`; an await block of such
children returns `Text[][]`. Follow #686's ordered, non-flattening collection
rules. No implicit awaiting, tuple/union types, or authored generics are added.
Waiting does not transfer ownership: stopping a wait leaves spawned roots alive;
an async child's original parent lifetime still applies.

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
launch-value delivery; a dispatch failure records a failed root. Route events
by the new root/thread through host observation, without adopting the source
foreground tracer or changing its interrupt target.

The Script CLI stops its executor when the invocation exits, canceling unfinished
spawned roots. Local Chat keeps its executor across turns until session close;
AgentCore keeps it until host shutdown. A client/view disconnect does not transfer
ownership. No implicit join keeps a short-lived host alive. Records survive;
opening their store in another executor does not adopt or replay them.

## Persistence and Recovery

Atomically commit the thread/create control, root/run control, self-contained
entry State/context, and source Step output. Both controls use `triggered_by`
pointing to the originating Step in the same agent store, even across threads;
the root parent stays null. Separate parent from origin in shared admission
helpers. Add no new ControlKind, synthetic caller control, or scheduler.

Flow records a run-kind Step with SpawnStmt, `Future<T>` output, and its optional
named binding; agic records a normal Tool Step with the serialized SpawnReceipt.
The root/thread/create/run records reconstruct the same receipt for either
surface; no duplicate flow-local receipt is needed. Preserve typed inputs and
futures with the ordinary codecs and keep references resolvable. Do not split
thread creation and root admission into separate commits.

Use the source run/physical Step/admission occurrence as the stable request
identity. Reprocessing a committed occurrence restores its existing future or
receipt; conflicting specifications fail. New loop occurrences and whole-run
reruns may launch new roots. Cancellation/rejection before commit creates nothing.
Recovery never relaunches an accepted root without a live owner; this is admission
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
| Language | `lang/{ast,lower,types,contracts,flow_validation,format,description}.py`: shared Future vocabulary, CST, binding/type inference, diagnostics, prepared-cache compatibility |
| Execution | `execution/executor/{executor,common,frame,tool_runtime}.py`, new `execution/executor/stmts/spawn.py`: shared admission, context, ownership, binding |
| Runtime tools | `execution/tools/_toolang.py`, `base/protocols/tool.py`, `execution/runnables.py`, assembly guidance/result matching: registration, hands, receipt-only continuation |
| Persistence/hosts | `execution/{store,threads,records,events,types,schemas}.py`, inspection/history and host observers: Future codec, SpawnReceipt schema, atomic admission, provenance, retention, event routing |

Pin the published grammar, update examples/generated references, and generate
the implementation changelog through the repository runnable. This definition
changes no dependencies, product code, or changelog.

## Acceptance Tests

1. Cover the upstream grammar contract, all bindings, named/inline targets,
   canonical formatting, `Future<T>` inference from complete target output, and
   cache compatibility. Malformed `let text = spawn a process` must error,
   never become text or launch work.
2. Match run input validation, inline captures, State resolution, hands policies,
   visibility, and ancestry checks; reject authority/configuration arguments.
3. Gates prove future/receipt delivery without awaiting completion.
   Preserve unbound locals in final position, immediate completion, and replay;
   named let changes only its destination. Check struct fields/types, exact
   references, codec round trips, and rejection of missing/extra/wrongly typed
   fields. Round-trip futures with their complete result contracts; preserve
   historical records and reject ordinary data misuse. Preserve batches and
   child receipts/waits; inject no spawn completion online or on replay. Verify
   the exact spawned run_id is visible in live and reconstructed tool replies.
4. Verify thread/root IDs, null parent, control/peer provenance, empty history,
   copied context/limits, fresh accounting, and no authority widening. Cover
   nested callers, iteration captures, and later source changes.
5. Exercise the lifetime table, Script/Chat/AgentCore ownership, admission racing
   stop, and foreground event/interrupt isolation. Inspection must expose durable
   outcomes without waiting or granting authority.
6. Fault-inject creation, commit, registration, and delivery for both Step forms:
   no orphan thread, one admission per committed occurrence, explicit dispatch
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
Approval covers the shared Future contract, SpawnReceipt schema, hands
authorization, new empty threads, inherited authority without history, and
executor ownership. Waiting behavior remains in #685/#686.
