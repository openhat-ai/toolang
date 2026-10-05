# Define Root Spawning from Flow and Agic

Status: Proposed; feature definition only. The human requested separate grammar
and runtime definitions, launch support in both Flow and Agic, and deferred
async/await support. The complete contract below still requires human approval.

## Goal and Scope

Let either runnable start an independent root in a new empty thread, using the
same agent and executor, receive an admission receipt, and continue working.
Success means both callers use the same admission operation and receipt, with
explicit inputs, authority, ownership, persistence, and inspection behavior.

The [grammar definition](https://github.com/openhat-ai/tree-sitter-toolang/pull/48)
owns source syntax, CST, keyword boundaries, and highlighting. This definition
owns its Toolang integration and the model-facing runtime tool. Implement after
the grammar is published and the approved call/array simplification is available.
Neither [async run/await](https://github.com/openhat-ai/toolang/pull/685) nor
[await blocks](https://github.com/openhat-ai/toolang/pull/686) is a dependency.

Exclude waiting, Future types, automatic completion messages, new control kinds,
cross-agent dispatch, existing-thread selection, implicit forks, process
detachment, automatic restart, new CLI/API flags, and a new scheduler.

## Verified Baseline

- Flow `run` binds declared inputs from locals, waits for its child, and binds
  the complete output. Named calls resolve against the latest compatible State;
  inline code remains pinned to the containing definition.
- Agic `_toolang/run` accepts `{runnable, input?}` and atomically records a child
  plus `{run_id, controls: [entry_control_ref]}`. Its model loop then waits and
  supplies a separate `toolang:run-result` message before continuing. The receipt
  contains neither output nor output_type; older design prose is superseded.
- `RunExecutor.run` accepts roots with `parent=None` and returns a process-local
  handle. These Python handles are not authored-language values.
- Root admission already creates a `run` control. `RunStore.accept_run` currently
  derives `triggered_by` from `parent`; its atomic receipt path supports only a
  model `_toolang__run` Tool Step. Spawn needs a separate origin parameter.
- Runtime tools use the `_toolang` toolset factory and narrow `ToolRuntime`
  protocol. Agic routes currently expose run through hands and exec through
  handoffs. History already reads root records, Steps, status, and typed output.
- Executor shutdown cancels/drains all owned roots. The Script CLI stops its
  executor at invocation exit; independent roots do not imply detached processes.
- Thread history is a logical root prefix; its `before(root)` selection does not
  filter active predecessors. Compaction horizons are thread-scoped, and normal
  root cwd defaults can come from the latest finished root in that thread.
  ThreadManager already supports durable create/fork; fork requires a terminal
  visible anchor. None of these relationships imply executor ownership.

## Invocation and Inputs

Flow consumes the grammar's named and inline forms:

```too
let job = spawn investigate
let spawn record_audit
spawn -> Text: Research {{_}} and save the findings.
```

Named targets may be either Agic or Flow. Bind `_` to the declared primary input
and matching locals to declared named parameters, using exactly the ordinary
Flow run rules for missing/optional values, types, structs, arrays, and Parts.
Unused locals are not arguments. Inline Agic templates capture referenced
locals as ordinary inline run does; an output annotation describes the target's
eventual output, never the receipt. Capture before changing the result binding.
There is no spawn-specific input syntax, argument list, or implicit flattening.

Agic receives one new model-callable runtime tool (wire name `_toolang__spawn`):

```json
{"runnable": "agic:investigate", "input": {"_": "Compare the proposed designs"}}
```

Use the same parameter schema and input decoder as `_toolang/run`: required
nonempty runnable reference, optional input object defaulting to `{}`, `_` for
primary input, and other keys for named parameters. Reject unknown top-level
fields and invalid/missing target arguments with the existing error/diagnostic
convention. Do not infer missing arguments from the model conversation or Flow
locals. Model arguments cannot supply source code, agent, thread, run ID,
control ID, setup, limits, resources, model, workdir, or parent.

Both surfaces resolve/authorize once before admission, using ordinary named
live-resolution, baseline signature checks, pinned inline code, and target
input validation. Snapshot accepted input values and their provenance; subsequent
local assignments, publications, or model messages cannot change them. Admit
neither a root nor a success receipt when validation fails.

### Routing and Runtime Tool Availability

- Add `ToolRuntime.spawn(runnable, input)` and a stateless `_toolang/spawn` leaf
  through the existing factory/registration/dispatch path. Both it and the Flow
  handler call one execution-owned root-admission operation. Do not invoke the
  model tool from Flow or expose the executor/store to plugins.
- Spawn uses hands authorization, not handoffs. `hands = none` denies it;
  explicit hands and `*` select the same targets as child run. With no hands
  directive, the existing requested_only policy applies to both actions.
  Extend route action metadata and hands guidance to describe run and spawn;
  introduce no `spawns` directive or second target catalog.
- Preserve runtime-tool availability, generated-inline restrictions, repair
  restrictions, module visibility, and call-time route checks. A Flow statement
  uses ordinary authored Flow call authorization, not model hands routing.
- At launch reject the current or an ancestor runnable on the caller's active
  execution path, as child run does. Causal spawn links do not become execution
  ancestry; this does not introduce a global prohibition on repeated targets.
- The model should choose spawn for independent work and run when it needs the
  result before continuing. Spawn may share an ordinary ToolCall batch; existing
  exec/chdir singleton rules still apply. Each spawn call has its own admission.
  An interruption skips unstarted calls without rolling back accepted roots.

## Receipt, Binding, and Available Information

Both surfaces return the same spawn receipt, extending the child receipt's
run_id/controls convention with the newly allocated thread identity:

```json
{"run_id": "<RunRef>", "thread_id": "<ThreadRef>", "controls": ["<thread CreateControlRef>", "<entry RunControlRef>"]}
```

These are canonical serialized references; controls contains the new thread's
index-0 `create` control followed by the root's index-0 `run` control. The receipt
acknowledges committed creation/admission and registration for execution, even
if the root finishes before delivery. It is
immutable ordinary Json, with no new built-in type, handle methods, or live task
object. Copying, storing, returning, or rendering it does not launch, wait, or
grant authority. Control references are provenance, not caller control adoption.

| Surface | Immediate result | Subsequent behavior |
| --- | --- | --- |
| Flow `run R` | Complete child output after completion | Bind by normal run rules |
| Flow `spawn R` | Json receipt in `_` | Continue without waiting |
| Flow `let job = spawn R` | Json receipt in job only | Preserve `_` |
| Flow `let spawn R` | No local binding | Preserve every local; persist receipt |
| Agic `_toolang/run` | Receipt in ToolResultPart.output | Existing wait and run-result message |
| Agic `_toolang/spawn` | Spawn receipt in ToolResultPart.output | Continue; no wait or completion injection |

Flow statement result inference is Json regardless of the target's output type.
A Flow returning this receipt must satisfy its ordinary output contract, for
example `-> Json`; `spawn -> Text:` still yields Json to the caller. Failure
before admission leaves existing locals unchanged. Dropping/overwriting a receipt
does not cancel its root. The launcher Step's success never means root success.

| Information | Access and reason |
| --- | --- |
| Run ID | Receipt `run_id`; needed to identify, inspect, and control the root |
| Thread ID | Receipt `thread_id`; directly open/inspect the newly created conversation |
| Creation and entry controls | Receipt `controls`; identify both committed effects in order |
| Status and available typed output | Existing `history/read_output({run: id})`; a nonblocking snapshot, possibly partial or null |
| Error, accepted input, runnable, Steps, controls, usage | Existing Run/Step inspection and history records; do not duplicate mutable facts in the receipt |
| Cancel/steer/retry/rerun | Existing authorized host/API/CLI controls addressed by run ID; no new model control tool |

History remains an ordinary selectable toolset; spawn does not grant it. Pure
Flow code can pass/store the receipt or use an Agic with authorized history tools
to inspect it. This scope adds no Flow polling operator. Root output_type is
already part of its accepted contract and output records; status, output, error,
cost, and runnable echoes add no necessary admission information. Child run's
receipt remains unchanged: it creates no thread, so it has no new thread/control
to report. Shared fields retain the same meanings; receipts need not pretend
that child execution and creation of another conversation have identical effects.

Future async/await work must accept these durable references without relaunching
and validate targets through existing authorization. It owns waiting syntax,
typing/wrappers, cancellation of waits, and result binding. This supersedes this
PR's former dependency on a Future codec; #685 must reconcile that integration
before implementation. No await examples or behavior are implemented here.

## Thread Selection and Context

Always allocate a new empty thread at acceptance, including when the caller is
itself spawned. Accept no thread option in Flow syntax or the model tool. Do not
silently create a fork, switch the caller's selected thread, or reuse a thread
after a whole-run rerun. Restore the same thread only when recovering the same
committed spawn occurrence.

| Choice | Decision and rationale |
| --- | --- |
| New empty thread | Selected: independent history/compaction and an explicit input boundary; no unfinished caller exchange or sibling outputs become implicit input |
| Current thread | Deferred: independent roots would still share conversation history and later cwd defaults; a captured horizon alone does not freeze active predecessors |
| Fork caller history | Deferred: a fork needs a defined terminal anchor and frozen prefix; the current in-progress conversation is not a valid implicit anchor |
| Arbitrary existing thread | Deferred: requires destination authority, concurrent history policy, and a use case beyond starting an independent task |

This makes spawn's isolation useful for independent research/audit work. The
cost is that the caller must supply enough context through inputs or inline
captures; there is no implicit access to its conversation. Ordinary child run
and later async run remain the composition mechanisms within the current task.
An explicit current-thread mode can be defined later without changing this
default, but must define concurrent history snapshots first.

Use a new `spawn` ThreadPrefix with ordinary chat origin and
`ThreadPeer(type="agent", name=current_agent_name, thread=source_thread_id)`.
The peer records provenance; it creates no message subscription or cross-agent
dispatch. Keep the existing Thread record schema and `create` control kind;
allow normal chat inspection/fork behavior after the relevant terminal boundary.
No automatic UI switch, title generation, or message to the source thread.

- Create a new root identity, `parent=None`, and independent executor ownership
  in the same agent/host. No execution-child registration or caller task
  ownership. Starting inside a nested child still creates a separate root.
- Capture the caller's Setup, compatible resolved State, concrete model request,
  current cwd/workspace bindings, resource ceilings, and inherited settings.
  Apply ordinary target settings within the captured
  effective authority. Materialize the caller's effective restrictions as root
  ceilings: losing execution ancestry must not restore broader setup defaults.
- Start with no history prefix and horizon=None in the new thread. Do not copy
  far/near messages, compaction summaries, or repeat frames from the caller.
  Inherited recall settings operate on the new thread. Inline captures of
  available iteration values are passed as ordinary data before launch; a
  target requiring an unavailable outer frame fails preflight.
- Copy the caller's concrete cwd explicitly rather than looking up a new-thread
  default. Later chdir/compaction in either run cannot alter the other's context.
  Workspaces/files remain shared under their existing permissions; a new thread
  is not a filesystem sandbox or a new agent configuration.
- Copy effective limit values with fresh root accounting. Usage is charged to
  the new root, not the launcher's remaining counters. There is no new aggregate
  budget or task pool; multiple roots can increase total concurrent work.

## Run and Executor Lifetimes

Thread is durable conversation storage; Run is one execution; executor owns live
tasks. A new thread does not allocate a new executor or a detached process.
The source Run has only a causal link to the spawned root, not lifecycle ownership.

| Event | Spawned root behavior |
| --- | --- |
| Source Run succeeds, fails, is canceled, or executes a handoff | Continue under the same live executor; do not inherit source cancellation or finalization |
| Source loses/discards its receipt or stops inspecting | Continue; receipt reachability is not task ownership |
| Spawned root succeeds/fails/hits its own limit | Record its own result; do not fail, resume, or inject completion into the source |
| Explicit cancel of spawned root | Cancel/drain that root and its execution children; leave source and other independent roots alone |
| Executor.stop begins | Reject new admissions; cancel/drain every root it owns and persist terminal outcomes |
| Host crashes or is killed | Execution stops; durable records can remain pending/running; do not promise a final cancellation record |
| Another executor opens the same store | Records remain inspectable; no implicit transfer, resumption, or replay of accepted roots |

Serialize spawn admission/registration against executor shutdown. A stopped or
closing executor accepts nothing; a committed root must enter its owned task
registry before shutdown takes its drain snapshot. New-thread creation must not
permit an unowned acceptance between commit and stop.

Host examples follow existing executor ownership: Script CLI stops its executor
when the top-level invocation exits, so unfinished spawned roots are canceled.
Local terminal Chat retains its executor across turns but stops it on session
close. A long-lived AgentCore retains its executor until host shutdown; closing
a view/client does not itself transfer ownership or extend host lifetime.
No implicit join keeps a short-lived host alive. The event loop and providers
remain available until the host drains the executor.

Route execution events by the new Run/thread through existing host observation.
Do not reuse the source's foreground response tracer or redirect its interrupt
target to background work. Thread/Run records survive completion and shutdown;
missing live ownership is not success and must never trigger automatic replay.

## Controls, Persistence, and Recovery

Use existing `create` and `run` controls for the new thread and its first root,
with both `triggered_by` references pointing to the originating Flow spawn Step
or Agic Tool Step. Validate that origin in the same agent store; it is deliberately
in a different thread. Keep Run.parent null. Add no spawn control or synthetic
caller control. Inspection must show causation without execution-child ownership.

Flow uses a run-kind Step with SpawnStmt given, a Json receipt output, and its
actual default/named/discard binding. Agic uses the ordinary model Tool Step
with its original ToolCall and a ToolResultPart containing the identical receipt.
Never set the Agic scheduled-child slot or feed spawn receipts into the child
completion matcher, either online or during history reconstruction.

Atomically commit the thread/create control, root/run control, self-contained
entry State/context, origin, and source Step receipt before delivery. Generalize
the existing transaction helpers; separate create_thread then accept_run commits
would leave orphan threads on failure and are not sufficient. Extend child scheduling
transaction to distinguish execution parent from launch origin and the two
receipt encodings. Preserve existing child behavior. Persisted references must
remain resolvable independently of caller memory; use the ordinary value/content
codec, preserve typed inputs, and protect any retained source references.

Use the source run/physical Step/admission occurrence as a stable identity.
Recovery of a committed acceptance returns the same receipt before considering
another insert; duplicate-request errors alone are insufficient. Conflicting
specifications fail explicitly. A later intentional loop occurrence or whole-run
rerun has a new identity and may launch a new root. This guarantees admission
deduplication, not exactly-once external effects inside the root.

Cancellation before admission creates no root. After commit the executor owns
registration/launch and cleanup even if receipt delivery is interrupted. A local
dispatch failure records a failed root instead of leaving an unowned pending
acceptance. Process-loss recovery may inspect a persisted receipt/terminal root;
it must not silently relaunch an accepted root with no live owner. Recovery of
unfinished work across host restarts remains outside scope.

Restore completed Flow bindings from their receipt output. Retry/prune/rewind
must reject, before mutation, a cut deleting an origin or retained input reference
still needed by a surviving independent root; give rerun guidance. Never delete
that root as if it were a child, leave a dangling trigger, or erase a committed
receipt because delivery failed. Follow the existing store schema compatibility
policy for changed encodings; historical records remain inspectable.

## Implementation Touchpoints

- `src/toolang/lang/{ast,lower,contracts,flow_validation,format,description}.py`:
  consume the published spawn CST, infer Json, bind/format SpawnStmt, invalidate
  incompatible prepared caches, and validate target inputs/output annotations.
- `src/toolang/execution/executor/{executor,common,frame,tool_runtime}.py` and
  `stmts/spawn.py`: shared prepared-root admission, context ceilings, independent
  tasks, Flow binding, and Agic dispatch without scheduling a child wait.
- `src/toolang/execution/tools/_toolang.py`,
  `src/toolang/base/protocols/tool.py`, `src/toolang/execution/runnables.py`, and
  assembly protocol/guidance: tool registration, schema, hands routes, and
  receipt-only continuation. Preserve assembly/run_results child matching.
- `src/toolang/execution/{store,threads,records,events,types,schemas}.py`, inspection
  and history: atomic thread/root/origin/receipt, spawn thread prefix, existing
  create/run event routing, schema compatibility, root presentation,
  restoration, and retention checks. No new ControlKind or language value type.
- Pin the grammar dependency; update examples and generated references, then
  generate the implementation changelog through the repository runnable. This
  definition PR changes neither dependencies nor changelog nor product code.

## Acceptance Tests

1. Parse/check/format named and inline spawn with default/named/discard binding;
   verify Json inference and independent target output validation. Reject malformed
   syntax and invalid arguments before root admission. Use the upstream corpus
   contract for keyword/let boundaries; test prepared-cache invalidation.
2. Match Flow child input behavior for declared `_`, named/optional parameters,
   structs, arrays, Parts, and inline captures. Match Agic child input decoding,
   diagnostics, hands scopes/requested_only, module visibility, State publication
   checks, and active-ancestry rejection. Reject caller-supplied authority fields.
3. Deterministic gates prove both surfaces return the exact same three-field
   receipt before root completion. Verify all Flow bindings, tool batch behavior,
   no scheduled-child wait, and no injected result online or in recovered history.
   Existing child run receipts, waits, and completion messages remain unchanged.
4. Check distinct empty thread/root identities, null parent, peer/causal origins,
   explicit input captures, cwd, model, settings, and effective ceilings. No
   history prefix/horizon crosses threads; inherited recall does not copy history.
   Verify independent counters and no
   authority widening, including nested callers and changed caller locals/State.
5. Launcher return/error/cancel/exec and dropped receipts leave accepted roots
   running. Root failures do not fail launchers; root cancellation is independent.
   Host shutdown drains roots; short-lived CLI and long-lived host cases differ
   only by their existing lifetime. Test nested spawn, local Chat across turns
   and session close, admission racing stop, crash records without live owners,
   and event routing without changing the foreground thread/interrupt target.
6. Existing inspection finds status, error, input, provenance, typed output, and
   usage. History output snapshots do not wait or grant controls; malformed or
   unavailable refs fail normally. References never imply resource authority.
7. Fault-inject around thread creation, root commit, registration, and receipt
   delivery for both Step forms. Restore one thread/root/receipt, leave no orphan
   thread on rejection, reject mismatches, and fail dispatch
   explicitly; never relaunch ownerless acceptances. New loop/rerun occurrences
   remain intentional launches. No Future or await machinery is required.
8. Reject every retry/prune/rewind cut that would invalidate a surviving root's
   origin/input references before mutation; retain roots and receipts. Verify old
   records and ordinary child retry behavior. Use offline fake providers and
   gates; run all default checks for implementation, docs checks for this PR.

## Risks and Approval

Primary risks are resource escalation at the root boundary, duplicated admission,
accidental child-result delivery, dangling provenance, and confusing host lifetime
with run lifetime. Receipt reuse reduces interface surface but requires matching
completion behavior by operation, never merely by JSON shape. Independent budgets
and shared workspaces permit more concurrent work and ordinary filesystem races.

No blocking design question remains within this proposed scope. Approval must
cover Json receipts, shared hands policy, new empty threads, inherited authority
without caller history, and host lifetime. Async/await's later handle typing is
deliberately outside this scope;
the stable run/control references are its integration boundary.
