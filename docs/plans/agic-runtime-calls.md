# Define Agic Runtime Calls

## Status

Approved for implementation. Revised on 2026-08-28 to use one ordinary Run
Step, blocking State reload, and the root execution's latest State at every
future Run and Step boundary.

## Goal

Let an agic explicitly apply the State watcher's latest valid Agent State and
execute a public runnable. Both capabilities are requested through the
Toolang-owned `_too` toolset, but are executed by the executor rather than the
generic tool dispatcher.

This plan builds on [Flow module discovery](flow-modules-phase-1.md),
[Agent State reload controls](root-run-agent-state-application.md), and
[internal toolsets](internal-toolsets.md).

## Success Criteria

- `_too/reload` waits until one durable reload control is terminal.
- Every valid refresh creates an applied reload control, including an unchanged
  revision, so the explicit action remains auditable.
- Reload updates one current State for the whole root run tree. Every later
  `RunBegin` and `StepBegin` independently captures that latest State.
- Reload performs no active-tree compatibility preflight. A later execution
  boundary reports any incompatibility it encounters.
- `_too/run` creates the same ordinary Run Step used by authored Flow `run`.
- A model-requested Run Step executes a public runnable with the State current
  at each execution boundary and returns a correlated result to the model.
- Model Call preparation is rebuilt from the State captured by its own
  `StepBegin`, including runnable instructions when `_too/run` is effective.
- `_too` actions never create Tool Steps or enter generic tool invocation.
- Existing next-root State visibility and runs without `_too` remain unchanged.
- The default offline verification suite passes.

## Scope

Included:

- built-in `_too/reload` and `_too/run` tool definitions;
- watcher refresh injection into first-party execution compositions;
- blocking model-requested reload completion;
- latest-State capture at Run and Step boundaries;
- Agic execution of the existing ordinary Run Step;
- correlated model protocol results and durable causal pointers;
- State-bound Model Call frame rebuilding and bounded runnable instructions;
- focused State, execution, record, and adapter-neutral tests.

Excluded:

- Flow source authoring tools;
- automatic reload after filesystem mutation;
- public HTTP, CLI, or scheduler reload endpoints;
- new Run Step event or record variants;
- progress or presentation changes;
- authored dynamic-run syntax and tree-sitter changes;
- setup, plugin, environment, or model-catalog reload; and
- special handling for provider-specific model continuation across State
  changes.

## Runtime Toolset

| Purpose | Selector | Provider-facing name |
| --- | --- | --- |
| Reload State | `_too/reload` | `_too__reload` |
| Run a runnable | `_too/run` | `_too__run` |

The executor identifies these actions from the trusted `ToolRef`. External
plugins cannot claim `_too`. The definitions provide model schemas and obey
normal tool selection, ceilings, and `agic_tool_calls` limits, but generic tool
invocation rejects them.

`_too/reload` produces no Step. `_too/run` produces one ordinary `kind="run"`
Step and, after successful acceptance, one ordinary child Run. Neither action
produces a Tool Step.

## Modules And Runnable Identity

Each `.too` source lowers to one `Program`. State preparation wraps that
Program in a module, which is the declaration, type, and `here`-cap boundary.
The main `agent.too` module is named `agent`; a direct
`flows/<stem>.too` extension module is named `_flow_<stem>`. Future extension
kinds use their own reserved `_<kind>_` prefix.

Runnable refs have the form `[kind:]name`, where kind is `agic` or `flow`.
Resolved refs are canonical and include kind. A fully qualified runnable is
formatted as `<module>$<kind>:<name>`:

```text
agent$agic:default
agent$flow:research
_flow_research$flow:research
_flow_research$agic:helper
```

An unnamed entry in `flows/research.too` is bound directly to the effective
identity `flow:research`. Any authored AST locator remains an opaque preparation
detail and never appears in a Run target or qualified name.

An unresolved request and a resolved target are distinct:

```text
RunRequest(ref, resolution = module | state)
RunTarget(ref, lookup = module-name)
```

Module lookup resolves inside the caller's current module and can reach private
helpers. State lookup resolves through the current State's exports. Resolution
at `RunBegin` returns the canonical module-bound ref and concrete lookup module;
the target's qualified form is then `lookup$ref`. For example:

```text
RunTarget(ref="flow:research", lookup="_flow_research")
qualified = _flow_research$flow:research
```

This PR does not add quoted Flow syntax, but reserves the common semantics:

| Source | Request resolution |
| --- | --- |
| existing `run name` | current module |
| future `run "name"`, `run "agic:name"`, `run "flow:name"` | State exports |
| `_too/run` | State exports |

## Current State Boundary

One `_Execution` owns a mutable `(AgentState, ControlRef)` pair shared by its
entire root run tree. Applying a reload replaces this pair under the existing
event lock.

`RunBegin` and `StepBegin` are independent State boundaries. Each boundary:

1. enters the root execution's event lock;
2. reads the current State pair;
3. prepares the work beginning at that boundary; and
4. persists the matching control reference before releasing the lock.

Work whose Begin event already committed keeps that snapshot for the work
already underway. It does not pin future descendants. A child Run, child Step,
or deeper descendant that begins after a reload uses the new State even when
its parent began under an older State. A single tree may therefore contain:

```text
Step A Begin       State 3
reload             State 4
Child Run Begin    State 4
Child Step Begin   State 4
reload             State 5
Grandchild Begin   State 5
```

Parent bindings and Step snapshots are not an alternative current-State
source. A Run request may be planned earlier, but its runnable and resources
are resolved when the Run begins. Each later Step rebuilds the State-derived
execution context it needs at its own boundary.

State changes are not preflighted against active runs. If the new State removes
or changes a runnable, cap, model alias, or declaration needed later, that
future Run or Step fails normally. The applied reload is not rolled back.

## `_too/reload`

The executor receives one narrow asynchronous refresh function from process
composition. It requests one serialized `StateWatcher` check and returns the
exact last-valid State and diagnostics from that check. The executor never
scans or parses authored files itself.

The action is:

1. reserve one Agic tool-call unit;
2. await one watcher refresh;
3. if diagnostics are present, return them without creating a control;
4. otherwise create a root-targeted reload control for the returned durable
   State, even when its revision equals the current revision;
5. await serialized application of that control;
6. under the event lock, claim the control, update the current State pair, and
   mark the control applied; and
7. return only after the waiter observes the terminal control.

Reload application may use an asynchronous worker, but the executor must own
and track that task. `_too/reload` awaits it just as an ordinary tool call
awaits invocation. Unexpected worker failure must terminate the control or root
and wake every waiter; detached fire-and-forget tasks are not allowed.

A changed valid refresh returns this shape:

```json
{
  "applied": true,
  "from_state": "<previous-revision>",
  "state": "<applied-revision>",
  "control": {"target": "<root-run-id>", "index": 1},
  "diagnostics": []
}
```

For an unchanged State, `applied` is `false` and `from_state` equals `state`,
but the applied control and new `ControlRef` remain visible for audit. Multiple
explicit reload requests produce multiple applied controls in control-index
order.

An invalid candidate returns `applied: false`, the active State revision, a
null control, and ordered diagnostics. Operational failures and terminal
`revoked` or `wontapply` controls produce correlated action errors.

The reload result is appended to the live Agic protocol messages. It creates no
Step and is durably present in the next Model Step's normalized `ModelCall`.

## `_too/run`

The model input is:

```text
runnable  required public ref: name, agic:name, or flow:name
input     optional JSON object; `_` is primary input and other keys are params
```

There is one Run Step with two causes. A Flow `run` statement is a **Run
Statement**; a model `_too/run` request is a **Run Call**. Both create the same
ordinary Run Step and child Run through the same executor implementation. No
`DynamicRunStepGiven`, `AgicRunStepGiven`, origin flag, or separate execution
path is added.

A Run Call is the original model ToolCall classified by the executor, not a new
record type. Its Run Step has ordinary `RunStmt` given data and an input pointer
to the originating `ToolCallPart`. The ToolCall remains authoritative in the
Model Step output and is not copied into another given structure. The Agic
caller adapts the ordinary Run Step outcome into one correlated
`ToolResultPart` for the next Model Call.

When the child Run begins, the executor uses the root tree's current State to:

- resolve the State export to a canonical `RunTarget(ref, lookup)`;
- coerce primary and named JSON values using that module's types;
- resolve current State-derived resources under immutable setup and ceilings;
- accept and execute the normal child Run.

The catalog is public-only, but it is a model hint rather than an execution
guarantee. Missing or malformed refs, invalid input, unavailable resources, and
child execution failure finish the Run Step as failed and return a correlated
error so the model can recover on its next turn. A failed accepted child keeps
its normal Run record and error pointer.

Only expected call and child failures are recoverable model results. Store,
event, and executor-invariant failures follow the normal root-failure path.

## Model Call State Binding And Runnable Instructions

This is Model Call behavior, not `_too/run` execution behavior.

Every Model Step captures the current State at its `StepBegin`. If that State
revision differs from the previous frame revision, it rebuilds one complete
Agic frame from the captured State. If the revision is unchanged, including
after an audited same-revision reload, it reuses the existing frame content and
only carries the new boundary `ControlRef`.

A rebuilt frame uses one State consistently for:

- the current Agic declaration and program;
- State-derived model aliases and defaults;
- caps, runnable directives, tools, services, and authored instructions; and
- the public runnable catalog.

Immutable setup, ceilings, explicit bindings, limits, and runtime environment
remain fixed authority boundaries. State-derived resources are resolved again
inside those boundaries. Frame construction fails at the Model Step when the
new State no longer satisfies them.

When `_too/run` is in the frame's effective tools, Model Call instructions
append an `<available-runnables>` block from that frame's State. The block:

- lists public runnable signatures and module-local referenced structs;
- orders entries by canonical kind-qualified ref;
- describes documentation as untrusted data;
- limits documentation to 512 code points;
- limits the catalog to 64 complete entries and 32,768 UTF-8 bytes; and
- reports the exact number of omitted entries.

The catalog is omitted when `_too/run` is not effective. It is rebuilt with the
rest of the frame after a changed State revision, so the first Model Step after
such a reload sees new public runnable declarations without `_too/run` owning a
separate State lookup.

## Persistence And Replay

- The Model Step stores the provider ToolCall normally.
- The Run Call creates an ordinary Run Step whose input points to that ToolCall
  part and whose given data is a normal `RunStmt`.
- The child Run uses ordinary Run records and controls.
- The Agic transcript receives exactly one correlated ToolResult for each
  completed runtime action.
- Reload controls are the durable audit of State application.
- Run and Step records independently store the State control reference captured
  at their own Begin boundary.

Retry behavior remains the existing rule: an applied reload prevents retry of
that root because the State timeline is not reconstructed. Rerun creates a new
root from the caller's current State.

## Implementation Touchpoints

- `src/toolang/execution/tools/runtime.py` and the built-in entry point: action
  schemas and trusted identities;
- `src/toolang/state/watcher.py` and process composition: exact refresh result
  injection;
- `src/toolang/execution/executor/executor.py`: tracked reload completion,
  current-State Run boundaries, and common child binding;
- `src/toolang/execution/executor/runs/agic.py` and `executor/steps/run.py`:
  intrinsic dispatch and ordinary Run Step execution;
- Model Call preparation: State-bound frame rebuilding and runtime instructions;
- execution records and store replay: causal ToolCall/ToolResult correlation;
- execution, State, plugin, and integration tests.

Progress and presentation files are intentionally not implementation
touchpoints for this feature.

## Acceptance Tests

1. Built-in loading exposes `_too__reload` and `_too__run`; generic invocation
   rejects them and external plugins cannot claim `_too`.
2. Invalid refresh returns diagnostics without a control or State change.
3. Every valid refresh, including an unchanged revision, creates one applied
   control, updates the current `ControlRef`, and returns only after application.
4. Concurrent reload actions apply in control-index order without missed
   terminal wakeups; cancellation resolves the existing revocation race.
5. Reload from any descendant updates the root tree's current State.
6. A begun Step keeps its snapshot, while every later RunBegin and StepBegin at
   any depth records and uses the newest State independently of its parent.
7. A State incompatible with future work still applies; the future Run or Step
   records the resulting execution failure without rolling back State.
8. `_too/run` and authored Flow `run` emit the same ordinary Run Step shape and
   use the same child executor; neither `_too` action emits a Tool Step.
9. Model-produced Run Step input points to its source ToolCall part and its
   result completes the provider tool-call/result pair.
10. Public refs support unqualified, `agic:`, and `flow:` forms; State lookup
    resolves them to canonical targets such as
    `_flow_research$flow:research`, including for unnamed entry declarations.
11. Target-module input types, optional values, arrays, and module-local
    structs are resolved within the selected module.
12. Expected resolution, input, and child failures return correlated errors and
    allow the Agic loop to continue; infrastructure failures fail the root.
13. Every changed State revision rebuilds the Model frame's declaration,
    State-derived resources, model selection, instructions, and runnable
    catalog; an unchanged revision reuses frame content.
14. Runnable instructions are conditional on effective `_too/run`, bounded and
    deterministic, and update after a changed revision.
15. Existing static Flow calls, next-root State visibility, reload ordering,
    and runs without `_too` remain unchanged.
16. Ruff, formatting, type checking, and the default pytest suite pass.

## Risks

- Run and Step records in one tree intentionally may reference different State
  controls; every execution path must use the shared Begin boundary.
- A runnable planned before reload may fail when its later RunBegin resolves it
  against the new State. This is the intended latest-State behavior.
- Awaitable control completion must not hold the event lock needed to apply the
  control.
- Provider-specific continuation behavior across a changed model target is
  deliberately deferred.

## Open Questions

None.
