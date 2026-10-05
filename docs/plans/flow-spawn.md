# Define root spawning from flow and agic

Status: Proposed runtime definition; full human approval is still required.
No implementation changes in this PR.

## Goal and Scope

Let flow and agic start an independent root in a new empty thread under the same
agent and executor and continue without waiting. Both expose Run: a handle with
readable identity/status and a typed eventual result.

The merged [grammar definition](https://github.com/openhat-ai/tree-sitter-toolang/pull/48)
owns syntax, CST, keywords, and highlighting. Implementation requires its
published grammar and the [call/array simplification](flow-array-semantics.md).
This runtime contract supersedes #48's provisional Json handle typing.
[Async run/await](https://github.com/openhat-ai/toolang/pull/685) and
[await blocks](https://github.com/openhat-ai/toolang/pull/686) remain separate work.
The launch features share the runtime handle contract below; whichever lands first
supplies common support without depending on the other's execution behavior.
Exclude completion messages, thread selectors, implicit forks, cross-agent
dispatch, detached processes, restart/resume, and new CLI/API flags.

## Verified Baseline

- Flow `run` waits and binds complete output. Agic `_toolang/run` acknowledges
  admission, then waits and injects a separate `toolang:run-result` message.
- `RunExecutor.run` starts roots with `parent=None`; its Python handle is not a
  language value. `stop()` cancels and drains all roots owned by that executor.
- Thread history includes active predecessors; compaction and cwd defaults are
  thread-scoped. Fork requires a terminal anchor; rewind changes visible history
  without deleting physical records.
- Store admission derives control `triggered_by` from the execution parent.
  Atomic child launch replies support model Tool Steps only; thread creation has a
  separate transaction. Spawn must extend these boundaries.

## Invocation and Inputs

```too
let job = spawn investigate
spawn record_audit
spawn -> Text: Research {{_}} and save the findings.
```

Flow reuses ordinary `run` input binding and validation: declared `_` and named
parameters come from matching locals; inline agic captures referenced locals.
Capture inputs before updating any binding. Unused locals are not arguments.
Inline `-> T` describes the target's eventual output, giving a `Run<T>` handle.

Agic gains `_toolang/spawn` (wire name `_toolang__spawn`), with the same input
schema and decoder as `_toolang/run`:

```json
{"runnable": "agic:investigate", "input": {"_": "Compare the proposed designs"}}
```

Require a runnable reference; default omitted input to `{}`. Named targets may
be flow or agic. Do not infer arguments from conversation history or accept
additional fields for thread, identity, source code, or execution configuration.

## Run Handle and Result Types

`Run<T>` and `Return<R>` are design notation for a runtime handle and its eventual
result contract. They introduce no language type, annotation, generic syntax,
constructor, or reserved type name. Describe the handle's public fields using
Toolang struct notation; this is a schema illustration, not a built-in declaration:

```too
struct Run:
  id: Text
  thread: Text
  status: Text
```

`id` and `thread` are immutable canonical RunRef/ThreadRef strings. `status` is a
read-only snapshot of the referenced run's persisted lifecycle: pending, running,
succeeded, failed, or canceled. It is not the launching Step's status.
The result contract is internal runtime metadata, not a public data field.
Scripts obtain handles from launch statements without declaring a struct.
An authored struct named Run remains ordinary data and is not awaitable.

Let `T = Return<R>`, the complete output type of target runnable R:

| Caller | Operation | Value type | Available when | Destination |
| --- | --- | --- | --- | --- |
| flow | `run R` | `T` | Child succeeds | `_` by default |
| flow | Later `async run R` | `Run<T>` | Child is admitted | Named let only |
| flow | `spawn R` | `Run<T>` | Independent root is admitted | Named let only |
| flow | Later `await job`, with job: `Run<T>` | `T` | Referenced run succeeds | `_` by default |
| flow | Later `await:` block | `T[]` for homogeneous children, otherwise `Json[]` | All children succeed | `_` by default |
| agic | `_toolang/spawn` targeting R | Serialized Run view | Independent root is admitted | ToolResultPart.output |

Await reads that run's result without launching work. For `Run<Text[]>`, await returns
`Text[]`; an await block of such children returns `Text[][]`, without flattening.
Run's meaning is independent of who owns its lifetime: async children remain
parent-owned, while spawned roots remain executor-owned.

Returning a Run handle confirms admission. Agic receives `{id, thread, status}`
and can cite id or pass it as the run argument to history/read_output.
The reply is a snapshot; it does not update
inside model history. Keep `_toolang/run`'s existing `{run_id, controls}` reply
and completion message unchanged. Creation/entry controls remain persisted and
inspectable by run ID; they need no separate field on the new handle.

### Field Access and Binding

Flow reads fields through existing template paths. For example:

```too
let job = spawn investigate
let run_id = {{job.id}}
run inspect_started_run
run: Run {{job.id}} in thread {{job.thread}} is {{job.status}}.
```

Here inspect_started_run consumes the ordinary named run_id input. Existing
let-template rendering rules apply; no general member-expression syntax is added.
Field projections are ordinary data. Capture those values, not a live handle,
when crossing a runnable boundary. Rendering the whole handle explicitly yields
its public view, never its result. Rendered/serialized data is not awaitable.

Resolve public views in execution, with one status snapshot per referenced run
per statement evaluation. Later evaluations can observe a new status; neither
background progress nor a status read changes locals or waits for completion.
Agic can refresh through existing inspection tools. Missing or mismatched run
references fail explicitly. Pending/running records without a live owner remain
inspectable; status alone does not promise execution or restart work.

Await uses ordinary value-statement binding: `await job` writes the result to `_`,
`let result = await job` writes result only, and `let await job` discards it.
The Run handle stays available unless its local is the destination: explicit
`let job = await job` replaces job with `T`, as does `await _` for `_`.
Only success writes a binding. Retained handles can be awaited repeatedly and
keep their readable fields; a replaced `T` cannot be awaited. Stopping a wait
does not cancel an independent spawned root.

Bare and nameless-let spawn preserve every local, including a final statement's
flow output. Both lower to `SpawnStmt.binding=None` and format as bare spawn;
restoration preserves those rules. Admission rejection preserves locals;
discarding or overwriting a handle does not cancel its run.

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
- Handle fields grant no authority. Reuse authorized history and host controls
  for inspection/control; add no polling operator or model control tool.

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
handle delivery; a dispatch failure records a failed root. Route events by the
new root/thread without adopting the source foreground tracer or interrupt target.

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

Flow records a run-kind Step with SpawnStmt, runtime handle output, and its optional
named binding; agic records a normal Tool Step with the admission-time Run view.
Do not split thread creation and root admission into separate commits.

Persist handles through an execution-owned record variant, distinct from ordinary
data outputs. Store identity and result contract, not status, results, tasks, or
executors. Execution resolves status/output and validates the thread and accepted
contract. Restore handle locals with that metadata; do not add Run to the language
value-type registry or struct codec. Lookalike Json, authored structs, and rendered
views do not become handles. General handle parameters/containers remain out of scope.

Use the source run/physical Step/admission occurrence as the stable request
identity. Reprocessing restores the original handle and recorded agic reply;
conflicting specifications fail. New loop occurrences and whole-run reruns may
launch new roots. Cancellation/rejection before commit creates nothing. Recovery
never relaunches an accepted root without a live owner. This is admission
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
| Language | `lang/{ast,lower,contracts,flow_validation,format,description}.py`: spawn CST, handle/result contract tracking separate from authored value types, field validation, diagnostics, prepared-cache compatibility |
| Execution | `execution/executor/{executor,common,content,frame,tool_runtime}.py`, new `execution/executor/stmts/spawn.py`: admission, Run views/projections, context, ownership, binding |
| Runtime tools | `execution/tools/_toolang.py`, `base/protocols/tool.py`, `execution/runnables.py`, assembly guidance/result matching: registration, hands, handle-only continuation |
| Persistence/hosts | `execution/{store,threads,records,events,types,schemas}.py`, inspection/history and host observers: Run codec/view, atomic admission, provenance, retention, event routing |

Pin the published grammar, update examples/generated references, and generate
the implementation changelog through the repository runnable. This definition
changes no dependencies, product code, or changelog.

## Acceptance Tests

1. Cover the upstream grammar contract, all bindings, named/inline targets,
   canonical formatting, handle/result contract tracking, and cache compatibility.
   Run remains an available authored struct name, with no handle semantics. Malformed
   `let text = spawn a process` must error, never become text or launch work.
2. Match run input validation, captures, State resolution, hands policies,
   visibility, and ancestry checks; reject authority/configuration arguments.
3. Gates prove immediate handle/field availability. Check id/thread types,
   every status, consistent snapshots within an evaluation, later status changes,
   metadata captures, and ordinary runnable inputs. Model history retains the
   recorded snapshot. Reject unknown fields and lookalike handles; metadata reads
   never wait or publish the result. Preserve all binding and child-run behavior.
4. Verify thread/root IDs, null parent, control/peer provenance, empty history,
   copied context/limits, fresh accounting, and no authority widening. Cover
   nested callers, iteration captures, and later source changes.
5. Exercise the lifetime table, host ownership, admission racing stop, and
   foreground event/interrupt isolation. Inspect ownerless records without replay.
6. Fault-inject creation, commit, registration, and delivery for both Step forms:
   no orphan thread, one admission per occurrence, explicit dispatch failure.
   Round-trip Run identities/result contracts; verify conflicting requests,
   intentional loop/rerun launches, and historical records.
7. Reject destructive cuts that invalidate surviving roots; permit nondestructive
   rewind with references intact. Preserve handles, bindings, and child retries.

Use offline fake providers/gates and all default checks for implementation.
This definition requires implementation review, link checks, and `git diff --check`.

## Risks and Approval

Main risks: authority widening, duplicate admission, stale status interpreted as
live ownership, accidental child completion delivery, and dangling references.
Independent budgets can multiply work; shared files still permit write races.
Approval covers runtime handles/views, hands authorization, new empty threads,
inherited authority without history, and executor ownership. Waiting remains
in #685/#686 and consumes the same Run handle contract.
