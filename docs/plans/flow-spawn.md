# Root spawning from flow and agic

Status: Approved in #687; implemented in [#692](https://github.com/openhat-ai/toolang/pull/692).

## Goal and Scope

Flow and agic can start an independent root in a new empty thread under the same
agent and executor, then continue with its identity without waiting for its result.
The contract covers input capture, authorization, ownership, durable admission,
inspection, and consistent execution progress.

Syntax follows the [grammar definition](https://github.com/openhat-ai/tree-sitter-toolang/pull/48)
and `tree-sitter-toolang==0.4.0a2`. [Async run/await](https://github.com/openhat-ai/toolang/pull/685)
and [await blocks](https://github.com/openhat-ai/toolang/pull/686) are separate work.
This PR adds no thread selector, cross-agent dispatch, detached process, automatic
restart, or CLI/API flag.

## Operations and Completion

| Operation | Execution | Successful source Step completion | Returned value |
| --- | --- | --- | --- |
| `run R` / `_toolang/run` | Child Run, owned by the caller | After the child finishes | Child result `T`; tool output is `{type, value}` |
| `spawn R` / `_toolang/spawn` | Independent root in a new thread | When admission commits | Run handle; tool output is `{id, thread, status: "pending"}` |
| `exec R` / `_toolang/exec` | Replace the runnable in the same Run | When the handoff commits, before target Steps | Flow has no value; tool output is `{controls: [ControlRef]}` |

Here `T = Return<R>`, the target's complete result type. Flow uses `run`, `spawn`,
and `exec` Step kinds. Agic uses ordinary Tool Steps for the corresponding runtime
tools. The operation has the same execution semantics in both forms. A successful
run with no result returns an empty tool output.

A child failure fails the source Step. Child-only cancellation produces a tool
error for agic, which may continue; flow propagates cancellation. Canceling
or immediately steering the caller unwinds its child and source Step. Caller
cancellation takes precedence over child-only cancellation. Exec preserves the
entry Run's result contract and never resumes the outgoing body.

## Invocation and Handles

```too
flow main(_: Text) -> Text:
  let job = spawn investigate
  let run_id = {{job.id}}
  run: Run {{job.id}} in thread {{job.thread}} is {{job.status}}.

agic investigate(_: Text) -> Text:
  user: Research {{_}} and save the findings.
```

Flow uses ordinary run input binding: declared `_` and named parameters come from
matching locals. Inline `spawn [-> T]: BODY` captures referenced locals; `-> T`
describes the target's result. Inputs are captured before writing the destination.
Inline captures include outer locals referenced inside template sections.

`let job = spawn R` binds only `job`. Bare `spawn R` and `let spawn R` leave every
local, including `_` and the flow's current output, unchanged; both format as bare
spawn. Discarding or overwriting a handle does not cancel its Run.

Agic calls `_toolang/spawn` (wire name `_toolang__spawn`) with the run tool's schema:

```json
{"runnable": "agic:investigate", "input": {"_": "Compare the proposed designs"}}
```

`runnable` is required; omitted `input` means `{}`. Named targets may be flow or
agic. Only `runnable` and `input` are accepted. Each tool call in a batch commits
independently; interruption skips calls that have not started. Exec and chdir
retain their single-call requirement.

`Run<T>` is design notation for a runtime handle with result type `T`, not an
authored type, generic syntax, or constructor. Its public fields are illustrated
using Toolang struct notation:

```too
struct Run:
  id: Text
  thread: Text
  status: Text
```

- `id` and `thread` are immutable canonical RunRef and ThreadRef strings.
- `status` reads the referenced Run's persisted state: pending, running, succeeded,
  failed, or canceled. Execution takes one snapshot per referenced Run per statement.
- Flow reads fields through templates. Rendering the whole handle produces its
  public view. Capture fields or rendered data when passing ordinary runnable inputs;
  native handles cannot be parameters, container elements, or runnable results.
- Agic receives the committed admission snapshot. Its stored tool reply and model
  history remain unchanged as the Run progresses. Existing history tools inspect
  status and results by ID; completion does not send a message to the source.

Flow stores the ordinary Output envelope and `output/value` reference path:

```json
{"type": "_Run<Text>", "value": {"id": "run_abc", "thread": "spawn_xyz"}, "binding": "job"}
```

The runtime tag is `_Run<T>` when the result type is known, otherwise `_Run`.
The value stores identity; the root entry stores the complete result contract,
including struct definitions. Execution validates identity and contract when
reading the handle. User struct names cannot start with `_`; an authored `Run`
struct and lookalike Json remain ordinary data. Status alone does not establish
live ownership or cause work to restart.

Future async run produces the same conceptual `Run<T>` for a parent-owned child.
Future `await h` reads `T` and binds `_`; `let v = await h` creates `v` on success.
Neither implicitly replaces `h`; only an explicit destination of `h` does so.
These await forms are not implemented by this PR.

## Authorization and Captured Context

Flow and `ToolRuntime.spawn(runnable, input)` use one execution-owned admission
path. The tool is registered through the existing factory and receives only the
per-call runtime interface. Agic spawn uses the same hands policy as run;
handoffs governs exec. Module visibility, requested-only routing, resource
restrictions, and generated-inline/output-repair tool restrictions still apply.

Resolve named targets against the latest compatible State and inline targets
against their pinned code. Validate inputs and authorization before admission.
Reject the current or an ancestor runnable on the active execution path; causal
links to other roots do not extend that path.

The root captures Setup, resolved State, model request, cwd, workspace bindings,
limits, inherited settings, and target settings. Effective resource restrictions
become root ceilings. Each root has fresh usage counters within the copied limits;
files and workspaces remain shared.

Available iteration history is captured as ordinary data. Unavailable outer-frame
dependencies reject admission. A local repeat/reduce scope fully shadows captured
history, including indexes outside its window; leaving that scope restores the
captured history. Flow rendering, agic prompts, and nested spawn use the same
selection rule. Accepted inputs and entry context are unaffected by later source
assignments, chdir, compaction, or State publication.

## Thread and Ownership

Each spawn creates a `spawn`-prefixed thread with chat origin, an empty history,
and `horizon=None`. Its peer is
`ThreadPeer(type="agent", name=current_agent_name, thread=source_thread_id)`.
The new root has `parent=None`; thread peer and control `triggered_by` identify
its source. This separates history and execution ownership while preserving
causality. Inherited recall reads the new thread.

| Event | Effect on the spawned root |
| --- | --- |
| Source returns, fails, is canceled, executes a handoff, or loses the handle | Continue under the same executor |
| Spawned root completes, fails, or reaches a limit | Record its own outcome |
| Spawned root is explicitly canceled | Cancel its execution subtree |
| Executor stops | Reject admissions and cancel/drain all owned roots |
| Host crashes | Execution stops; records may remain pending/running |

Admission and task registration are serialized with executor shutdown. Script
stops its executor when the invocation exits; local Chat keeps it across turns
until session close; AgentCore keeps it until host shutdown. A client disconnect
changes neither ownership nor lifetime. Opening a store does not adopt its Runs.

## Commit Boundaries and Records

Step records describe invocations; controls describe committed decisions; Run
records describe execution outcomes. Runtime controls are persisted as applied
with their effects and equal creation/finish times. Only external steer/cancel
requests may remain pending until a checkpoint or become `wontapply` at Run end.

Spawn commits the following in one transaction:

1. The new thread and its applied `create` control.
2. The pending independent root and its applied `run` control, with accepted input
   and context. Both controls point `triggered_by` to the source physical Step.
3. The succeeded source Step, output, and finish time.

Before commit, rejection or failure leaves no admitted root or partial controls.
After commit, interrupted delivery cannot change the successful source Step;
dispatch or execution failure belongs to the new Run. The executor owns and drains
accepted work even when the caller never receives the handle.

Exec similarly commits its applied `exec` control and succeeded source Step before
starting target Steps in the same Run. Flow exec also closes open repeat ancestors
in that transaction. Chdir commits its applied `chdir` control with its successful
Tool Step and retains `cwd` as the location field.

The record vocabulary and schema are specified in [execution records](../run-step-records.md).
Schema 52 rejects older stores before mutation; migration guidance belongs to
[CHANGELOG.md](../../CHANGELOG.md).

## Events, History, and Presentation

Synchronous run emits `StepBegin`, child `RunBegin`, child Steps, child `RunEnd`,
and source `StepEnd` in that order. Agic emits its paired ToolResult before
`StepEnd`. Child admission and `RunBegin` share the same event-lock boundary.
Spawn's source `StepEnd` reports committed admission; its independent root has
its own event stream. Exec ends the source Step before target Steps begin.

Terminal events use the persisted status, output, error, and finish time, including
when cancellation interrupts delivery. Background roots route through their own
root/thread tracer and never adopt the source's foreground progress or interrupt
target. Reopening the store reconstructs exact model calls from recorded messages
and paired tool replies, without requiring a following model call or live tasks.

[Execution presentation](../execution-presentation.md) normalizes flow statements
and runtime Tool Steps to a presentation-only `StepOperation`. Projectors, rows,
lanes, and timers consume that operation while preserving source styling and
physical Step metrics. Displayed outcomes follow execution events.

## Deduplication and Retry

A source physical Step identifies one admission. Reprocessing it returns the same
handle and tool reply; conflicting input or context fails. New repeat occurrences
and whole-run reruns may create new roots. Restoring a source prefix restores its
handles without launching their targets again.

Retry rejects any cut that would delete a surviving root's origin or retained
input references, before mutation, with guidance to use rerun. Rewind may hide the
source while retaining its records and references. Independent roots are never
pruned or canceled as source children. Explicit retry of a spawned root retains
its accepted context and authority ceiling.

## Acceptance and Implementation Map

| Acceptance criterion | Implementation and tests |
| --- | --- |
| Grammar, bindings, field validation, formatting, runtime type round trips | `lang/`, `execution/{types,records,schemas}.py`; `test_spawn.py`, `test_output_bindings.py` |
| Immediate admission, input/authority checks, empty thread, captured context, lifecycle independence | `execution/executor/{spawn,tool_runtime}.py`, `execution/executor/stmts/spawn.py`; `test_spawn_scenarios.py` |
| Section captures, nearest iteration scope, restoration, model-call replay | `execution/executor/{executor,iteration,frame}.py`; `test_spawn_captures.py`, `test_iteration_frames.py` |
| Atomic controls/source outcomes, commit rollback, dispatch failure, interrupted delivery | `execution/store.py`, `execution/executor/_persist.py`; `test_runtime_control_commit.py`, `test_store_atomicity_scenarios.py`, `test_spawn_scenarios.py` |
| Synchronous child lifetime and paired results/errors, cancellation precedence, storage reopen | `execution/executor/tool_runtime.py`, `execution/assembly/run_results.py`; `test_sync_run_lifecycle.py`, `test_sync_runs.py` |
| Foreground isolation and shared Script/Chat presentation | API relay and `cli/common/execution_progress/`; `test_runtime_progress.py`, `test_runtime_tool_progress.py`, `test_agic_run_progress.py` |
| Admission deduplication, retained handles, retry/prune protection | `execution/store.py`; `test_spawn_scenarios.py` |

Implementation paths are relative to `src/toolang/`; tests live under `tests/`.
Use offline providers and gates, then run the repository's default verification.
The main risks are authority widening, duplicate admission, dangling references,
and confusing an admission or status snapshot with execution completion.
Independent budgets can multiply work, and shared files permit write races.
