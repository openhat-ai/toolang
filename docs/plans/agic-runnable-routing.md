# Define Agic Runnable Routing

## Status

Approved for implementation.

This definition supersedes the runtime-tool availability and execute-transition
decisions in the earlier version of this plan. It retains the existing
`hands`, `handoffs`, State-boundary, Run Step, and child Run decisions.

## Goal

Let an Agic call or transfer to explicitly authorized public runnables while
keeping executor-owned runtime tools separate from user-facing tools.

- `hands` authorizes a child Run whose result returns to the caller.
- `handoffs` authorizes replacing the caller for the remainder of the same Run.
- `_too` is the always-available inner runtime toolset.

## Success Criteria

- `_too__run`, `_too__execute`, and `_too__reload` are offered on every normal,
  tool-capable Agic Model Call, independently of route lists and refresh
  capability.
- Runtime instructions make the tools conservative by default. A bounded
  catalog is present only when the current Agic declares `hands` or `handoffs`.
- `_too__run` creates the existing ordinary Run Step and child Run, waits, and
  returns a correlated result to the caller.
- `_too__execute` creates no Step and no child Run. One applied execute control
  durably records the same-Run replacement before target execution begins.
- Progress shows an execute marker while the target has not started, a failed
  marker when preparation is rejected, and a handoff divider on the target's
  first natural Step without synthesizing a transition Step.
- `_too__reload` creates no Step and retains its existing reload-control
  behavior, including an applied no-op control for an unchanged State.
- Flow Run Statements remain source-authorized and unchanged.

## Scope

In scope:

- Agic `hands` and `handoffs` authorization;
- inner runtime tool definitions, Model Call injection, dispatch, and
  instructions;
- bounded authorized-route catalog rendering;
- child-Run calls and same-Run execute transfer;
- execute control records, lineage, retry/rerun behavior, and State timing; and
- removal of the obsolete Handoff Step from types, records, events, progress,
  and tests.

Out of scope:

- new Flow syntax, dynamic Flow Run Statements, or Flow control transfer;
- asynchronous spawn/join behavior;
- thread, agent, or remote handoff;
- user-configured runtime-tool availability; and
- special provider-continuation target checks.

## Vocabulary

| Term | Meaning |
| --- | --- |
| public runnable | An Agic or Flow exported by the current Agent State |
| hand | A public runnable authorized for `_too__run` |
| handoff | A public runnable authorized for `_too__execute` |
| Run Call | A model `_too__run` call that creates an ordinary Run Step |
| execute request | A model `_too__execute` call in a Model Step output |
| execute control | The durable commit that replaces the active runnable |
| inner runtime toolset | Executor-owned `_too` definitions and dispatch |
| agent state toolset | Agent-State-owned `_me` tools |
| user-facing toolset | Public tools such as `fs`, `web`, and `shell` |

The process analogy is intentional but not literal:

```text
hand       ~= fork + exec + wait + structured result
handoff    ~= exec
```

## Language Surface

`hands` and `handoffs` are Agic-only directives:

```too
agic coordinate(_: Text) -> Report:
  hands = agic:research, flow:verify
  handoffs = flow:deliver

  Coordinate the requested work.
```

Each directive may occur once, must use `=`, and contains an ordered list of
exact `name`, `agic:name`, or `flow:name` refs. Empty lists, duplicate authored
refs, filters, globs, additive operators, and use on a Flow are invalid. An
absent directive means an empty authorization list.

Missing well-formed targets do not invalidate State. They remain authored
authorization intent but are omitted from the current route catalog. An
unqualified ref authorizes either kind with that public name; a qualified ref
authorizes only that kind. Model spelling may narrow but never widen authored
authorization.

Private module helpers cannot resolve through these directives.

## Flow Boundary

A Flow is statement-driven. Its `run name` statement remains the authority and
continues to create the existing ordinary Run Step and child Run. It does not
consult `hands`, `handoffs`, or `_too`.

Future dynamic or transfer Flow syntax requires a separate language design and
must not implicitly reuse Agic route directives.

## Inner Runtime Toolset

The executor owns exactly these model-protocol tools:

| Model name | Meaning |
| --- | --- |
| `_too__run` | Run an authorized hand and resume with its result |
| `_too__execute` | Transfer the remainder of this Run to an authorized handoff |
| `_too__reload` | Refresh and apply the newest valid Agent State now |

`_too` is a toolset, but its definitions are not public `AgentToolResource`
values. They are absent from `AgentSetup.tools`, authored `tools` selection,
resource ceilings, plugin invocation, and public tool inspection. External
plugins cannot claim the reserved `_too` toolset. Generic tool dispatch always
rejects `_too__...`; Agic execution dispatches trusted runtime definitions.

All three definitions are present on every ordinary tool-capable Agic Model
Call. Route lists authorize targets but do not add or remove definitions.
Refresh capability does not remove `_too__reload`; an executor without a State
refresh source returns a correlated error when it is called. Statement-generated
Flow evaluators, output-repair calls, and providers or models without
tool-calling support receive no runtime definitions because of their execution
or protocol boundary, not route semantics.

Runtime calls continue to consume the existing `agic_tool_calls` limit.

## Model Instructions And Route Catalog

Every Model Call that receives runtime tools also receives runtime
instructions. The instructions state:

- Do not call a runtime tool merely because it is available or a route resembles
  the request.
- Call `reload` only when the current Run must observe newly authored State now.
  A future root Run naturally starts with the latest valid State.
- Call `run` only when an authorized target must run now and its result is needed
  before the caller continues.
- Call `execute` only when an authorized target must take over the remainder of
  the current Run. The caller never resumes, and execute must be the only tool
  call in that Model Call.
- Prefer `run` when either `run` or `execute` would satisfy the intent.

When neither directive is authored, the instructions state that no target is
authorized and omit the runnable catalog entirely. Otherwise, the bounded
`<available-runnable-routes>` document includes only the authored, nonempty
route lists. Each resolved target appears once with its canonical ref,
signature, reachable module-local structs, bounded untrusted documentation,
and an `actions` array.

The catalog remains deterministic and is limited to 64 complete entries,
32,768 UTF-8 bytes, and 512 documentation code points per entry. It reports
omitted entries. Catalog membership is a hint; runtime authorization uses the
authored routes captured by the originating Model Step.

Both route tools accept:

```text
runnable  required authorized ref: name, agic:name, or flow:name
input     optional object; `_` is primary input and other keys are parameters
```

## Run Call

`_too__run` retains the ordinary Run Step path:

1. The originating Model Step captures the Agic frame and authored `hands`.
2. The model ToolCall remains the request.
3. A normal Run Step begins and captures the then-current State.
4. Resolution uses that State; authorization uses the captured `hands`.
5. The executor prepares, accepts, executes, and awaits one child Run.
6. Success or a recoverable failure becomes one correlated ToolResult and the
   caller continues.

The Run Step and child Run retain their existing events, records, ownership,
limits, and progress behavior. The executor rejects a stable runnable identity
already present in the current or any active ancestor lineage.

## Execute Transfer

`_too__execute` is a runtime call contained in a successful Model Step output.
It creates no Handoff Step, Run Step, Tool Step, child Run, or second
`RunBegin`.

The observable Step sequence is:

```text
RunBegin(caller)
Model Step(output contains _too__execute)
target's natural Model, Tool, Run, or Flow Steps
RunEnd(target result)
```

Before commit, the Agic executor:

1. requires execute to be the Model Call's only tool call;
2. validates its input shape and requested ref;
3. uses the originating Model Step's captured State and captured `handoffs`;
4. resolves and authorizes the target;
5. prepares target input, module, resources, model binding, and executable; and
6. rejects any stable target identity already present in the current or an
   active ancestor lineage.

A pre-commit request, authorization, resolution, input, resource, or lineage
failure creates no execute control. One correlated ToolResult is appended and
the caller continues at its next Model Step. Infrastructure and store failures
remain Run failures.

After successful preparation, the executor atomically persists one applied
execute control, then replaces the in-memory active binding and lineage. The
caller cannot resume after that commit. The target starts directly with its
natural next Step in the same Run.

The replacement retains the Run ID, root Run ID, thread, parent Step, setup,
ceilings, tree-wide accounting, Step sequence, and entry output contract. It
replaces the public runnable, module, explicit input locals, runnable resources,
and executable. Caller messages, continuation, provisional output, and
per-Agic counters are discarded.

The target resolves user tools through the normal public runnable resource
rules. With no authored `tools` directive it receives every tool in the agent
resource base; an explicit directive narrows that base. The caller's runnable
selection does not constrain the target, and `_too` remains independent of both.

Target success supplies the existing Run's result. Target failure or
cancellation ends that Run and never restores the caller. Chained execute
transfers are allowed when lineage remains acyclic. The final result is coerced
against the entry runnable's output contract.

## Execute Control

The execute control is the durable commit boundary:

```text
ControlKind     execute
scope           run
target          current Run
timing          immediate
status          applied
```

Its typed payload records:

- the captured State revision;
- the canonical runnable ref and owner module;
- a source pointer to the originating Model ToolCall part; and
- typed raw-`Json` input locals that point directly into that ToolCall's
  `input` fields. The captured target signature and State define coercion.

The control is created and finished synchronously after preparation. It is
never pending and has no worker. Persistence completes before the in-memory
replacement. The applied control remains historical truth if target execution
later fails or is canceled.

`RunBegin.runnable` remains the entry runnable. Ordered applied execute controls
provide the transition lineage. Retry rejects a Run tree containing an applied
execute control because replay would cross a replacement boundary. Rerun starts
a normal new root Run from the original request.

There is intentionally no compatibility or migration for records containing
the removed `kind="handoff"` Step. Such unpublished records are not guaranteed
to reopen.

## State Timing

- A Model Step captures State and rebuilds its complete Agic frame from it.
- Execute resolution, authorization, and preparation use that same captured
  State because execute has no later Step boundary.
- A Run Call's ordinary Run Step captures the current State when it begins.
- After reload, every later natural Step and child Run boundary uses the latest
  applied State under the existing executor rules.
- Execute must be the only tool call in its Model Call, so reload and execute
  cannot share one result batch.

## Implementation Touchpoints

- `src/toolang/execution/tools/runtime.py`: inner runtime tool vocabulary and
  always-present definitions.
- `src/toolang/execution/executor/prepare.py`, `steps/model.py`, and
  `runs/agic.py`: frame-bound instructions, adapter assembly, dispatch, and
  correlated failures.
- `src/toolang/execution/runnables.py`: route authorization and bounded catalog.
- `src/toolang/execution/executor/executor.py`: target preparation, applied
  execute commit, in-place transfer, lineage, and entry output coercion.
- `src/toolang/execution/{types,records,store}.py`: execute control vocabulary,
  payload serialization, atomic persistence, and retry rejection.
- `src/toolang/execution/{types,records,events}.py` and CLI progress: delete the
  Handoff Step vocabulary and derive execute presentation from ordinary Model
  and Step events.
- execution unit and integration tests: cover definitions, instructions,
  controls, no-Step transfer, failures, chaining, and retry/rerun.

## Acceptance Tests

1. Every normal tool-capable Agic Model Call sees all three `_too` definitions,
   including when routes are empty and refresh is unsupported.
2. Statement-generated Flow evaluators, output-repair calls, and tool-disabled
   calls see none; public selection, ceilings, listing, plugins, and generic
   dispatch cannot control or invoke `_too`.
3. Runtime instructions are always paired with effective runtime definitions.
   They omit the catalog when both directives are absent, omit each absent list
   from a nonempty catalog, and obey catalog bounds.
4. Unsupported reload returns a correlated error; supported reload retains its
   existing applied-control behavior, including unchanged-State no-op records.
5. `_too__run` uses captured authorization plus Run Step State, produces one
   ordinary Run Step and child Run, and returns to the caller.
6. A failed execute produces no Step and no execute control, returns a
   correlated error, and lets the caller continue.
7. A successful execute produces one applied execute control, no transition
   Step or child Run, one `RunBegin`/`RunEnd`, and only target natural Steps.
8. The execute payload round-trips the captured State, canonical ref, module,
   ToolCall source, and raw-`Json` source-pointing locals.
9. Successful execute discards caller frame state; target success, failure, and
   cancellation terminate the same Run without caller recovery.
10. Chained execute transfers preserve one Run and reject repeated or active
    ancestor identities.
11. The entry output contract survives transfer.
12. Retry rejects applied execute history; rerun remains valid.
13. Handoff Step types and codecs are absent. Progress projects an active
    execute marker, correlated prestart failure, and first-target-Step handoff
    divider from ordinary events.
14. Default offline verification passes.

## Risks

- Always-present high-impact tools require concise, consistent conservative
  instructions across Model Calls.
- The execute control, not a synthetic Step or mutation of `RunBegin`, must be
  the sole durable transition truth.
- Source-pointing input locals depend on the originating Model ToolCall part
  remaining the canonical model-output representation.
- Same-Run replacement discards provider continuation by design; the target
  must always begin with a fresh Agic frame.

## Open Questions

None.
