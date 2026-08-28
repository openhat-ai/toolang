# Define Agic Runtime Calls

## Status

Proposed.

## Goal

Let an agic explicitly reload the latest valid Agent State and call a public
runnable from the State captured at that call boundary. Expose both actions
through the Toolang-owned `_too` toolset while preserving provider tool-call
compatibility, durable execution history, and existing State isolation.

This plan builds on [Flow module discovery](flow-modules-phase-1.md),
[Agent State reload controls](root-run-agent-state-application.md), and
[internal toolsets](internal-toolsets.md).

## Success Criteria

- The built-in `_too` toolset exposes `reload` and `run`.
- `_too` provider calls are executor-owned runtime actions, not ordinary tool
  invocations: reload is consumed inside the executor and run projects a Run
  Step.
- `_too/reload` checks the process-owned State watcher and, when valid State
  changed, waits until the existing reload control is applied.
- `_too/reload` does not create a Step; the model Step that requested it keeps
  its captured State and the next Step in the root tree observes the reloaded
  State.
- `_too/run` resolves only a public agic or flow from its Run Step State and
  executes it as a normal child run.
- Each model call that can use `_too/run` receives a deterministic bounded
  catalog of callable public runnable signatures from that model step's State,
  with any omitted entries reported explicitly.
- Each following model request contains a correlated tool call and result for
  every completed runtime action without any `_too` Tool Step.
- Progress and UI presentation derive exclusively from typed execution events;
  no presenter observes executor callbacks or reparses provider tool names.
- Existing authored Flow `run` behavior, automatic next-root refresh, and runs
  that do not use `_too` remain unchanged.
- The default offline verification suite passes.

## Current Behavior

Flow modules are already published as immutable Agent State revisions and are
visible to the next root run. The executor also supports durable `reload`
controls: applying one replaces the root execution's current State at a
serialized step boundary, while a started step keeps its captured State.

Reload is currently process-local API behavior only. An agic cannot request a
watcher check or submit the resulting State. Its model loop treats every
provider-emitted call as a Tool Step, and only authored Flow statements can
create child runs. Runtime instructions do not list the public runnable
catalog.

## Scope

Included:

- the built-in `_too` toolset with `reload` and `run`;
- watcher refresh injection into resident, local, hosted, sandboxed, and
  one-shot execution composition;
- synchronous completion of model-requested State reload;
- executor-internal reload actions and agic-produced dynamic Run Steps and
  child runs;
- durable run-call facts and provider-history replay;
- active public runnable signatures in runtime instructions; and
- focused State, executor, record, adapter-neutral history, CLI, and
  integration tests.

Excluded:

- tools dedicated to creating or editing Flow source;
- automatic reload after any filesystem mutation;
- public reload HTTP, CLI, Chat-control, or scheduler endpoints;
- setup, plugin, environment, model-catalog, or policy reload;
- private cross-module calls or cross-module static types;
- authored `run "name"` syntax or other Toolang grammar changes;
- tree-sitter changes; and
- multi-State retry reconstruction beyond the existing applied-reload guard.

## Runtime Toolset

The canonical identities are:

| Purpose | Selector | Provider-facing name |
| --- | --- | --- |
| Reload State | `_too/reload` | `_too__reload` |
| Run a runnable | `_too/run` | `_too__run` |

The double underscore remains the model-adapter separator. `_too_reload` and
`_too_run` are informal descriptions, not protocol identities.

Both definitions load through the normal built-in toolset entry point and
participate in existing tool directives, agent ceilings, resource snapshots,
and `agic_tool_calls` limits. No new run-call limit is introduced. A policy
that excludes `_too/*` or one leaf removes that action from the model request.

The executor recognizes these actions from the trusted structured `ToolRef`,
not from an untrusted raw call name. External plugins cannot register `_too`.
The action definitions provide provider schemas, but the agic executor converts
their calls into executor-owned runtime actions; generic direct tool invocation
rejects them. No `_too` action enters `invoke_tool_call()` or produces
`ToolStepGiven`. This keeps executor and watcher authority out of `ToolContext`
and prevents external tools from acquiring runtime mutation callbacks.

### Event And Presentation Boundary

Provider tool calling is only the model protocol used to request `_too`
actions. The executor selects the event projection from the action's actual
execution semantics:

| Model request | Internal handling | Action-specific event projection |
| --- | --- | --- |
| `_too/reload` | State refresh and reload control application | no action-specific Step or event |
| `_too/run` | public runnable resolution and child acceptance | one Run Step and optional child Run |

`_too/reload` is consumed between model Steps. It does not reserve a step index,
change `last_step`, create a `StepRecord`, or emit `StepBegin`, `Part*`, or
`StepEnd`. The executor appends its correlated `ToolResultPart` to the live agic
messages, and the next Model Step durably captures that complete request in
`ModelStepGiven.call`. The requesting Model Step remains the next Model Step's
dependency. The reload control is authoritative for the State transition, and
the changed `StepBegin.state` on a later Step exposes the revision actually used
at that boundary.

There is no reload-specific progress item. Progress, TUI, API streaming, and
other presenters consume execution events only; they do not receive a
runtime-action callback, inspect executor internals, or parse `_too__*` names.
Dynamic Run Steps, child Runs, ordinary Tool Steps, and their normal events are
the complete action-specific presentation surface in this phase.

`_me` has a different ownership boundary. It changes authored Agent State
sources through the normal built-in tool invocation path and therefore remains
a visible Tool Step with `ToolStepGiven` and `ToolStepNoted`. A presenter may
render `_me` differently from an external or world-facing tool by using the
built-in `ToolStepGiven.plugin == "_me"` identity and summaries already carried
by the Tool Step events. The `_me` plugin and toolset intentionally have the
same canonical name. A presenter must not consult the live tool registry or
split the provider-facing `__` name. A successful `_me` mutation publishes no
executor State transition by itself; `_too/reload` is the separate explicit
action that can adopt the watcher's State in the active root.

## `_too/reload`

`_too/reload` takes no input. The requesting Model Step has already captured the
old State when the executor consumes the action. It reserves one
`agic_tool_calls` unit because the model requested it through provider tool
calling, but it does not create or increment any Step.

The executor receives one narrow asynchronous State-refresh function from its
process composition. That function requests `StateWatcher.refresh()` and
returns the resulting last-valid State together with diagnostics from the same
completed check. The executor does not scan or parse source itself.

The action behaves as follows:

1. Request and await one serialized watcher check using the metadata fast path.
2. If the candidate has diagnostics, return them without creating a reload
   control or changing execution State.
3. If the valid revision equals the root execution's current revision, return
   an idempotent no-op.
4. Submit the concrete durable State through a model-facing wrapper around the
   existing `RunExecutor.reload()` path. The retained process-local request is
   marked as requiring active-tree compatibility; the durable payload and
   public process-local API do not change.
5. At the control's existing `event_lock` application boundary, preflight the
   candidate against every accepted run that is still active in the root tree.
   Each active module and frozen resource identity must remain resolvable, and
   each active agic must be able to rebuild its next frame with its existing
   declaration, variables, bound model, setup, and ceilings.
6. If preflight succeeds, claim and apply the control under that same boundary.
   If it fails, finish the control as `wontapply` with the compatibility error
   and retain the root's current State.
7. Await the control reaching a terminal status before appending the correlated
   protocol result to the agic messages. Only `applied` is success; `revoked`
   and `wontapply` become correlated protocol errors that the next model call
   can handle.

Model-requested reload actions are serialized per root tree by a dedicated
runtime-action lock across watcher refresh, current-revision comparison,
control submission, and terminal completion. This lock is distinct from
`_ActiveRun.event_lock` and is never acquired by reload application or
`StepBegin`; awaiting application therefore cannot block the boundary that must
apply it. Compatibility preflight runs inside the short application boundary,
not under the runtime-action lock alone, so child acceptance cannot race between
the compatibility decision and the State swap. Two concurrent requests for the
same revision produce one transition, because the later request observes the
applied revision and returns a no-op. External reload controls retain their
existing control-index ordering and join the existing `event_lock` application
boundary, but do not acquire the model-facing runtime-action lock or request the
additional compatibility guard.

Control completion uses an executor-owned, race-safe notification: registering
a waiter and reading an already-terminal control cannot miss each other. If the
tool action is canceled while its control is pending, it attempts the existing
revocation race and then follows the winning terminal status. It never waits
indefinitely for a revoked control or a root that has marked it `wontapply`.

The compatibility preflight is an additional process-local flag on
model-requested `_too/reload`, not a durable payload field or a change to the
public `RunExecutor.reload()` contract. An incompatible candidate remains the
watcher's newest valid State and is available to a new root run, but the current
root does not adopt it. Its control durably records `wontapply` and the first
active run, module, or resource that is no longer usable; the action returns the
same reason as its correlated error. Additive Flow modules and capabilities
that leave active bindings intact pass this check.

The existing reload control remains root-targeted with `immediate` timing. No
new control kind, payload, or State-head abstraction is added. Calling from a
descendant still reloads its whole root execution. The requesting Model Step
retains its old `StepRecord.state`; the following serialized step boundary uses
the reload control reference and new State. Already-started parallel steps keep
their captured State exactly as defined by the existing reload plan.

The result has one canonical shape:

```json
{
  "applied": true,
  "from_state": "<revision>",
  "state": "<revision>",
  "control": {"target": "<root-run-id>", "index": 1},
  "diagnostics": []
}
```

For an unchanged valid State, `applied` is false and `control` is null. For an
invalid candidate, `applied` is false, `state` remains the active revision,
`control` is null, and `diagnostics` contains the ordered watcher diagnostics.
Invalid source is therefore an inspectable action result, not a change to the
last-valid State. Active-tree incompatibility, operational refresh failure, and
the terminal `revoked` or `wontapply` statuses remain ordinary correlated tool
errors and create no successful result payload.

An execution surface that cannot supply the process-owned watcher returns a
clear unavailable error and never falls back to constructing another watcher
inside the executor. All first-party execution compositions supply it.

## `_too/run`

The tool input is:

```text
runnable  required public ref: name, agic:name, or flow:name
input     optional JSON object; `_` is primary input and other keys are params
```

For example:

```json
{
  "runnable": "flow:research",
  "input": {"_": "Toolang", "depth": 3}
}
```

An omitted `input` is an empty object. The executor parses an optional kind
prefix, resolves only the public catalog in the State captured by the dynamic
Run Step, and validates the values against the target declaration. Input
coercion uses the target module's own structs. Arbitrary Toolang signature
types are supported through this JSON protocol boundary, but no type or struct
becomes visible to another module's authored source.

The child binding uses the target module, captured State and State reference,
root thread, immutable setup, limits, caller ceilings, and the exact model
binding inherited by existing static child runs. Its resources are resolved
from that State under those fixed ceilings, then the inherited model is
validated against the target runnable's effective model resources. State
additions can therefore supply the target module and its caps, while reload
cannot change the bound model or add setup tools, models, plugins, environment
values, or broader policy authority.

The child is accepted and executed through the existing recursive executor.
It emits normal Run events, retains normal accounting and controls, and parents
itself to the dynamic Run Step. Its private helpers remain available only
inside its module through existing authored static calls.

The correlated provider result is:

```json
{
  "run_id": "<child-run-id>",
  "runnable": "flow:research",
  "output_type": "Result",
  "output": {}
}
```

`output` uses the existing canonical protocol encoding for scalars, arrays,
structs, and message parts. The child Run record remains authoritative for its
typed result.

Resolution and input failures occur before child acceptance. A child failure
retains the existing pointer to that failed child. In both cases the dynamic
Run Step is failed and the correlated tool result contains an error, but the
agic model loop continues so the model can correct the call. Authored Flow
`run` failures continue to fail their enclosing Flow path as before.

## Agic Run-Call Execution

The provider still emits `_too__run` through its ordinary tool-call mechanism;
no provider API gains a Toolang-specific call type. The agic loop recognizes
the trusted runtime action before generic tool dispatch and creates:

```text
model Step
  dynamic run Step
    child Run
```

It does not create an additional Tool Step. The model Step retains the original
`ToolCallPart`. The dynamic Run Step stores:

```text
DynamicRunStepGiven
  call        original model-emitted ToolCall
  requested   text from `call.input.runnable`, or null when it is not text
  runnable    canonical resolved kind-qualified ref, or null on failure
```

This given variant is valid only for `kind = run`; authored Run Steps keep
their existing Flow statements. The step-boundary builder never lets request
parsing, runnable resolution, or input coercion escape before `StepBegin`.
Instead it captures the boundary State, stores the original request and any
resolved canonical ref, and retains a validation failure for step evaluation.
Evaluation then emits the normal failed `StepEnd` and correlated error result.
Consequently malformed and missing run calls remain inspectable even when no
child Run can be accepted.

`StepBegin.input` points to the originating `ToolCallPart` in the model Step,
using the same source-pointer fallback as a Tool Step if a malformed provider
call has no indexed part. After every terminal dynamic Run Step, the agic loop
appends its correlated `ToolResultPart` as one tool-role message, advances
`last_step` to the Run Step, and lets the next model Step depend on that Step.
Steer and cancellation complete or discard outstanding calls through the same
rules as existing Tool Steps. These updates occur for success and recoverable
failure, so the in-memory transcript and durable dependency graph agree.

The dynamic Run Step output is the correlated `ToolResultPart`, including the
original tool-call and call IDs. Transcript projection treats only a Run Step
with `DynamicRunStepGiven` as a tool-role message. Authored Run Steps produce no
provider transcript message. This closes every provider tool-call/result pair
and makes stateless message replay provider-portable. Same-provider stateful
continuation remains unchanged. This feature does not change the agic's bound
model or provider; any future feature that does so must clear provider-specific
`cont` before the first call to the new target.

Dynamic calls execute sequentially in the order returned by the model, like
current tool calls. They count against the requesting agic's tool-call limit.
The child also consumes the root tree's normal time, token, cost, model-call,
and tool-call budgets.

## Runnable Instructions

When `_too/run` is present in the effective tools for a model step, runtime
instructions include an `<available-runnables>` block built from that step's
captured State. It contains only public runnables whose resources can be
resolved under the immutable setup and caller ceilings and whose effective
model resources accept the exact inherited parent model binding. Catalog
generation and dynamic child acceptance use the same eligibility function, so
an advertised runnable cannot later fail only because model selection was
evaluated differently.

Each entry includes:

- canonical kind-qualified ref and documentation;
- primary input type and optionality;
- named parameter types and optionality;
- output type; and
- recursively referenced struct fields, scoped inside that runnable entry.

Entries are ordered by canonical kind-qualified ref. Struct rendering strips
array suffixes when following references, emits each reachable struct at most
once per entry, and leaves later or cyclic occurrences as type-name references.
The renderer therefore terminates for recursive module-local types. Free-form
documentation is JSON encoded, described as untrusted data rather than runtime
instructions, and truncated to 512 Unicode code points per runnable.

The complete block is canonical JSON inside the XML element and is limited to
64 entries and 32,768 UTF-8 bytes, including its framing. These are fixed
implementation constants in this phase. The renderer reserves the exact final
framing and `omitted` object before admitting entries, then adds complete entries
in canonical order until the next entry would exceed either limit. The result is
therefore the longest canonical prefix that fits; partial entries are never
emitted. The `omitted` object contains the exact number of remaining public
callable entries. Omitted runnables remain valid `_too/run` targets when the
model already knows their names, but they are not advertised by that model step.
An entry whose encoding cannot fit stops the prefix rather than failing model
preparation.

The block explains the `_` primary-input convention, the catalog limits, and
instructs the model to call `_too/run` only when delegating to one of the shown
entries materially helps. It is appended as runtime-owned protocol after the
selected default, custom, or empty authored instruct, so authored templates
cannot accidentally hide the catalog while the action remains available. It is
omitted when `_too/run` is not selected; resource selection is the way to
suppress both the action and its instructions.

Instructions are rebuilt at every model step boundary. Therefore the model
step after a successful `_too/reload` sees runnable signatures from the new
State, including a newly published `flows/<name>.too` module.

## Persistence And Inspection

Only `DynamicRunStepGiven` is added to the execution Step vocabulary and durable
codecs. No database table or schema migration is required because Run Step given
facts are already stored as canonical JSON. Inspection exposes the original
model call, canonical runnable when resolved, source `ToolCallPart` dependency,
Run Step, child Run when present, captured State reference, and correlated
result.

Reload adds no Step record. Its existing reload control and State references
remain authoritative for the transition. The correlated `ToolResultPart` is
live agic protocol state until the next Model Step begins, when it is included
in that Step's durable normalized `ModelCall`. It is not projected as a
standalone message when reconstructing later cross-run conversation history;
the reload exchange is internal to the agic invocation. The existing rule that
retry rejects a root with an applied reload stays unchanged; rerun starts a new
root from the caller's current State.

## Implementation Touchpoints

- `pyproject.toml` and `src/toolang/execution/tools/`: `_too` entry point and
  provider-facing action definitions;
- `src/toolang/state/watcher.py` and process composition: one refresh result and
  its injected executor callback;
- `src/toolang/execution/executor/executor.py`: awaitable reload completion,
  per-root runtime-action serialization, active-tree compatibility, public
  dynamic child binding, and captured-State resource resolution;
- `src/toolang/execution/executor/runs/agic.py` and `executor/steps/`: intrinsic
  dispatch, internal reload consumption, direct Run Step execution, and
  correlated results;
- `src/toolang/execution/types.py`, `records.py`, `store.py`, and `schemas.py`:
  dynamic Run Step facts, codecs, replay, and inspection;
- `src/toolang/execution/executor/prepare.py` and default runtime instruction:
  callable runnable signatures; and
- execution, State, plugin, CLI, API, event-only progress projection, and
  provider-adapter-neutral tests.

## Acceptance Tests

1. Built-in loading exposes `_too__reload` and `_too__run` with canonical
   selectors, and external plugins cannot claim `_too`.
2. `_too` calls never enter generic tool invocation or emit Tool Steps. Reload
   is consumed without allocating a Step or emitting action-specific events;
   run emits one dynamic Run Step. `_me` mutations remain ordinary Tool Steps,
   and a presenter can distinguish them using only `ToolStepGiven.plugin` and
   other event facts without parsing a provider name or observing an executor
   callback.
3. Tool directives and ceilings can exclude either action; excluded actions and
   runnable instructions are not sent to the model.
4. Reload with invalid source returns ordered diagnostics and preserves State;
   unchanged valid State is an idempotent no-op with no control.
5. Reload with a new valid revision records one existing reload control, waits
   for `applied`, allocates no Step, keeps the requesting Model Step on the old
   State, and puts the next Step on the new State.
6. A valid candidate that removes an active module or frozen resource, or that
   cannot rebuild an active agic frame, creates a model-requested control that
   becomes `wontapply` at the atomic application preflight and preserves the
   current State; public process-local reload behavior remains unchanged.
7. Reload from a descendant targets the root, and concurrent started steps keep
   their captured revisions; concurrent model reloads to one revision produce
   only one transition without holding `event_lock` while awaiting completion.
8. Reload completion observes an already-terminal control without a missed
   wakeup; `revoked` and `wontapply` terminate with correlated errors, and
   cancellation resolves the existing revocation race.
9. The next model call after reload lists a newly published Flow signature and
   can call it during the same root run.
10. Dynamic refs accept unqualified, `agic:`, and `flow:` forms; reject private,
   missing, malformed, and kind-mismatched refs; use the target module; and
   record a failed Run Step even when resolution or input validation fails
   before child acceptance.
11. Dynamic input supports primary and named values, optional fields, arrays,
   module-local custom structs, and all existing output types without exposing
   a cross-module static type namespace.
12. A dynamic call records its source `ToolCallPart` pointer, one Run Step and,
    on acceptance, one normal child Run with no Tool Step wrapper; its terminal
    result updates both the durable dependency graph and the live agic transcript.
13. Pre-acceptance and child failures produce correlated tool errors, preserve
    child error pointers when present, and allow the agic model loop to recover.
14. The next Model Step durably captures the internal reload result in its
    normalized call, completed runtime actions provide exactly one valid live
    tool-call/result pair, stateless cross-run history does not project the
    reload exchange, and no claim is made that provider-specific `cont` can
    cross providers.
15. Dynamic child resources reflect the captured State without expanding the
    fixed setup, ceilings, or policy authority; the child inherits the parent
    model binding, and instruction eligibility uses that same binding check.
16. Runnable instructions render cyclic structs finitely, truncate documentation,
    obey the 64-entry and 32,768-byte limits deterministically, and report the
    exact number of omitted callable entries without emitting partial entries.
17. Existing static Flow calls, next-root State visibility, reload API behavior,
    and runs without `_too` remain unchanged.
18. Ruff, formatting, type checking, and the default pytest suite pass.

## Risks

- A runtime Run Step must retain provider correlation while looking like an
  authored Run Step in execution progress; the dedicated given variant is the
  discriminator.
- Reload must not return before application or the immediately following model
  step could race and retain the old State.
- The runtime-action lock must never substitute for or nest around the
  `event_lock` application wait, or reload can deadlock itself.
- Dynamic child binding must switch to the target module for structs, caps, and
  helpers without widening immutable setup or policy authority.
- Catalog limits keep runtime instructions bounded, but omitted entries are less
  discoverable; a future discovery action would require separate approval.

## Open Questions

None.
