# Define Agic Runnable Routing

## Status

Approved for implementation.

This definition supersedes the tool-selection and all-public-catalog decisions
for `_too/run` in [Agic runtime calls](agic-runtime-calls.md). It retains that
plan's State-boundary, ordinary Run Step, and child Run semantics.

## Goal

Restore `hands` and `handoffs` as explicit Agic routing directives. A hand is a
public runnable that an Agic may call, await, and continue after. A handoff is a
public runnable that may replace the current runnable and take over the
remainder of the same Run.

Keep executor-owned `_too` actions separate from user-facing tools. The model
protocol uses `_too/run`, `_too/execute`, and `_too/reload`, while language
source and normal presentation expose only hands, handoffs, child runs, State
reloads, and runnable transitions.

## Success Criteria

- `hands` is the sole authored allow list for model-requested child Run calls.
- `handoffs` is the sole authored allow list for model-requested same-Run
  runnable replacement.
- `_too` actions are selected by executor capability and routing directives,
  never by `tools` directives, tool ceilings, or public tool selectors.
- `_too/run` creates an ordinary Run Step and child Run, waits for its result,
  and then resumes the calling Agic.
- `_too/execute` replaces the current runnable after one durable handoff
  boundary; the target continues the same Run and owns its final output.
- Model instructions explain when to run a hand and when to execute a handoff,
  and expose only routes authorized for that Model Step.
- State reload, target resolution, persistence, replay, limits, and failure
  boundaries remain deterministic and auditable.
- Flow Run Statements, including future runtime-resolved variants, remain
  source-authorized and are never governed by Agic routing directives.

## Scope

In scope:

- `hands` and `handoffs` syntax, validation, State behavior, and effective
  route resolution for Agics;
- executor-owned `_too/run`, `_too/execute`, and `_too/reload` definitions and
  Model Call injection;
- bounded route instructions;
- Run Call authorization and existing child Run execution;
- same-Run handoff execution and durable handoff events and records;
- reload interaction, routing-cycle rejection, retry/rerun behavior, limits,
  inspection, and semantic progress inputs; and
- removal of `_too` from public tool resources and selectors.

Out of scope:

- implementing new Flow Run or control-transfer statement syntax;
- asynchronous spawn/join behavior;
- remote agent routing;
- implementing `handoffs` as thread or agent transfer;
- user-configured or CLI-configured routing defaults; and
- changing existing Flow Run Statement resolution.

## Vocabulary

| Term | Meaning |
| --- | --- |
| public runnable | An Agic or Flow exported by the current Agent State |
| hand | A public runnable the current Agic may call as a child Run |
| handoff | A public runnable that may replace the current runnable in the same Run |
| Run Call | A model `_too/run` request authorized by `hands` |
| Run Statement | A source-authored Flow statement that creates a child Run |
| handoff request | A model `_too/execute` request authorized by `handoffs` |
| handoff commit | The durable boundary after which the caller cannot resume |

The process analogy is intentional but not literal:

```text
hand       ~= fork + exec + wait + structured result
handoff    ~= exec
```

A child Run does not copy the caller's messages, locals, provider continuation,
or mutable memory. It receives only explicit input and normal tree-owned runtime
authority.

## Language Surface

`hands` and `handoffs` are Agic-only routing directives:

```too
agic coordinate(_: Text) -> Report:
  hands = agic:research, flow:verify
  handoffs = flow:deliver

  Coordinate the requested work.
```

Each directive:

- may occur at most once in an Agic;
- must use `=`;
- accepts an ordered comma-separated list of exact public runnable refs;
- accepts `name`, `agic:name`, and `flow:name` forms;
- rejects an empty list, duplicate authored refs, malformed refs, filters, and
  glob patterns; and
- is rejected on a Flow declaration.

An absent directive defines an empty route list. A target may appear in both
lists because the caller may either use its result or transfer the remaining
work to it. Authored order is retained in the AST, but has no priority meaning;
runtime instructions order resolved entries by canonical kind-qualified ref.

The directive values are cross-module public references, not static imports.
Module parsing therefore validates only their syntax. A complete State may
contain a well-formed route whose target is currently absent. Such a route is
retained as authorization intent but omitted from Model Call instructions until
one current public export resolves it. This permits independently shared
modules and lets an explicit reload make a newly added target available without
rewriting the calling Agic.

An unqualified route authorizes either kind with that public name. A qualified
route authorizes only its stated kind. Public-name uniqueness keeps current
resolution deterministic.

Authorization compares the authored route with the resolved target, not only
with the spelling emitted by the model. Names must match. An unqualified
authored ref accepts either resolved kind; a qualified authored ref accepts
only that kind. A qualifier on the model request may narrow resolution but can
never widen the authored authorization.

`hands +=`, `hands -=`, `handoffs +=`, and `handoffs -=` are rejected in this
version. Routing inheritance or configured routing bases must be designed
before additive operations acquire semantics; they must not silently reuse
resource-directive set math.

## Effective Agic Routes

Frame preparation derives one immutable value from the Agic declaration and
the Model Step's captured State:

```text
AgicRoutes(
  hands = authored hand refs,
  handoffs = authored handoff refs,
  resolved = descriptors for refs present in captured State,
)
```

The authored refs are the authorization boundary. Resolved descriptors are
instructions, not authority. This distinction lets a Run Call already
authorized by the captured Agic frame resolve a target added by a preceding
reload action when its Run Step begins.

Private module helpers never resolve through either route list.

## Flow Boundary

A Flow is statement-driven, so its source is the call authority. The existing
`run name` statement resolves in its owner module and may call a private helper.
A future runtime-resolved form such as `run "name"` would still be authorized by
that authored statement; changing when its target is resolved would not turn it
into a model-requested route.

All Flow Run Statement variants create the same ordinary Run Step and child Run
without `hands`, `handoffs`, or `_too`. A Flow may be the target of a hand or
handoff, but it does not acquire Agic routing directives or hidden model actions.
If a future Flow statement accepts a computed target or transfers the current
Run instead of creating a child, its authority and transfer semantics require a
separate language definition; they must not implicitly reuse `hands` or
`handoffs`.

## Hidden Runtime Actions

The executor owns three model actions:

| Semantic action | Source selector | Model name |
| --- | --- | --- |
| run a hand | `hands` | `_too__run` |
| execute a handoff | `handoffs` | `_too__execute` |
| reload State | executor refresh capability | `_too__reload` |

`_too` actions are not `AgentToolResource` values and are absent from:

- `AgentSetup.tools` and `AgentResources.tools`;
- `tools` directive matching;
- setup, session, and run tool ceilings;
- public tool listing and inspection; and
- plugin tool invocation.

External plugins remain unable to claim the reserved `_too` toolset. The Agic
frame keeps public tools and executor actions in separate fields. Model Call
assembly combines their schemas only at the adapter boundary, and Agic dispatch
classifies `_too` calls only through the frame's trusted action definitions.
Generic tool dispatch always rejects them.

For a tool-capable, non-repair Model Call:

- inject `_too/run` exactly when effective `hands` is nonempty;
- inject `_too/execute` exactly when effective `handoffs` is nonempty; and
- inject `_too/reload` exactly when the executor has a State refresh source.

Output-repair calls and models without tool-calling support receive no `_too`
actions. Runtime actions continue to consume `agic_tool_calls` in this version
because each is a model-produced action round; renaming or splitting that limit
is separate work.

Authored `tools = _too/run` or another internal ref is invalid. Existing source
must express the route through `hands` instead.

## Model Instructions

When at least one routing action is injected, the executor appends one bounded
`<available-runnable-routes>` document. Each unique resolved runnable appears
once with its signature, reachable module-local structs, bounded untrusted
documentation, and an `actions` array containing `run`, `execute`, or both.

The instruction states:

```text
Use run to call an allowed hand as a child Run, wait for its result, and then
continue. Use it when you must inspect, combine, validate, summarize, or
otherwise act on the result.

Use execute only to replace the current runnable with an allowed handoff target.
The target continues the same Run and owns its final output. The current Agic
does not resume.

If either action would work, prefer run. Execute must be the only action in the
Model Call.
```

The route catalog:

- contains only the union of currently resolved hands and handoffs;
- orders entries by canonical `agic:name` or `flow:name` ref;
- preserves the existing limits of 64 complete entries, 32,768 UTF-8 bytes,
  and 512 code points of documentation per entry;
- reports omitted unique entries and omitted action memberships exactly; and
- is rebuilt with the complete Agic frame after a changed State revision.

The `_too/run` and `_too/execute` schemas both accept:

```text
runnable  required authorized ref: name, agic:name, or flow:name
input     optional JSON object; `_` is primary input and other keys are params
```

Catalog membership is a hint. Runtime authorization uses the captured authored
route refs, and target resolution and input preparation use the State current at
the semantic action boundary.

## Run A Hand

`_too/run` retains the ordinary Run Call design:

1. The originating Model Step captures the Agic frame and effective hands.
2. The model ToolCall remains the authoritative request.
3. A normal Run Step begins and captures the root tree's current State.
4. The target is resolved from the Run Step State and checked against the
   captured hands.
5. Its input and resources are prepared from that State under immutable setup
   and ceilings.
6. One ordinary child Run is accepted, executed, and awaited.
7. Its result or recoverable error becomes one correlated ToolResult for the
   calling Agic, which then continues.

The Run Step and child Run keep their existing events, records, ownership,
limits, and progress semantics. A Run Call may not target a stable runnable
identity in the entry-plus-handoffs lineage of its current Run or any active
ancestor Run. This rejection is an executor invariant and cannot be granted by
`hands`.

Store, event, and executor-invariant failures remain root failures rather than
recoverable model results.

## Execute A Handoff

`_too/execute` performs an in-place runnable replacement. It does not create a
child Run or a second `RunBegin`.

Before commit:

1. The Agic dispatcher inspects the complete Model Call action list.
2. One semantic Handoff Step begins, points to the originating ToolCall, and
   captures the root tree's current State.
3. The handoff must be the Model Call's only tool or runtime action.
4. The executor resolves the target from that State and checks it against the
   Model Step frame's captured handoffs.
5. It prepares the target input, module, resources, model binding, and
   executable from that State.
6. The target's stable `(module, kind, local-name)` identity must not already
   occur in the entry-plus-handoffs lineage of the current Run or any active
   ancestor Run.

Request, authorization, resolution, input, resource, or lineage failure ends
the Handoff Step as failed and appends a correlated ToolResult. The original
Agic has not been replaced and may continue on its next Model Call.

If execute appears with any other action, each execute request fails this
pre-commit check and the other actions retain normal ordered dispatch. This
includes multiple execute requests. No action executed earlier in that Model
Call is rolled back.

Successful preparation commits the Handoff Step. After commit:

- the Run ID, root Run ID, thread, parent Step, setup, ceilings, tree-wide
  accounting, and Step sequence remain unchanged;
- the target's public runnable, owner module, input locals, runnable resources,
  and executable replace the caller's execution binding;
- the caller's Agic frame, messages, provider continuation, provisional output,
  and per-Agic call counters are discarded;
- the entry runnable's effective output type remains the Run output contract;
- the target starts fresh execution at the next physical Step index;
- a target Agic receives its authored frame and explicit handoff input;
- a target Flow begins its authored statements with the explicit handoff input;
- subsequent Step boundaries continue to observe the root tree's latest applied
  State under the existing reload rules; and
- the target's result becomes the output of the existing Run.

The executor validates the final target result against the entry runnable's
output contract at Run completion. This preserves the contract already observed
by a parent Run Step without restricting the target's module-local signature.
An incompatible result fails the same Run after target execution; it does not
restore the caller.

There is no ToolResult after a successful commit because the calling Agic no
longer exists. Target failure fails the existing Run and never restores the
caller. A later target may execute another handoff, producing a transition
lineage such as:

```text
agic:route -> flow:research -> agic:deliver
```

Repeated stable runnable identity in the current or an active ancestor lineage
is rejected before commit. Retry and rerun remain the supported ways to execute
a prior runnable again.

## Events, Records, And Inspection

`_too` protocol names never determine presentation. `_too/run` continues to
produce the ordinary Run Step. `_too/reload` continues to produce only its
reload control. `_too/execute` produces one typed semantic Handoff Step.

The Handoff Step records:

- its input pointer to the originating Model ToolCall;
- requested and resolved canonical runnable refs;
- the State control captured at its Step boundary;
- the target owner module identity needed to replay that exact State; and
- succeeded or failed status and a bounded diagnostic.

`RunBegin.runnable` remains the Run's entry runnable and is never overwritten.
The latest succeeded Handoff Step determines the effective runnable for later
Steps. One Run therefore has one `RunBegin`, zero or more durable runnable
transitions, and one `RunEnd`.

Inspection shows both the entry runnable and ordered handoff lineage. Progress
renders the Handoff Step semantically as `handoff RUNNABLE`; it does not show a
generic Tool Step or `_too__execute`. A successful handoff has no child Run ID.

Retry cannot cross a succeeded handoff boundary because it would have to
reconstruct replaced frames and routing history. It follows the existing rule
for applied reload history and is rejected with a direct diagnostic. Rerun
starts a new Run from the caller's current State and original request.

## State And Reload Timing

Model Step authorization and semantic-action execution use separate, explicit
snapshots:

```text
Model Step State   -> Agic declaration, hands, handoffs, action schemas, catalog
Run/Handoff State  -> current target resolution, input coercion, target resources
```

This preserves model-output authority while allowing explicit reload to affect
later boundaries. A Run Call authorized by the producing Model Step may resolve
a newly available target after a preceding reload. If the current State still
does not resolve the requested ref, the action fails normally.

Non-execute actions are dispatched in provider output order. A reload before a
Run Call in the same Model Call therefore affects that Run Step; a reload after
it does not affect work whose boundary has already committed.

An execute request must be the only action in its Model Call, so reload and
execute cannot occur in one action batch. The Agic must reload, receive its
result, and produce execute from the next State-bound Model Step.

## Implementation Touchpoints

- `src/toolang/lang`: validate Agic-only exact `hands` and `handoffs` lists and
  preserve the existing formatter surface;
- `src/toolang/state`: retain module-local authored route refs while composing
  them against the public runnable catalog without making missing refs invalid;
- `src/toolang/execution/runnables.py`: route ref matching, resolved descriptor
  union, and bounded route catalog rendering;
- `src/toolang/execution/tools/runtime.py`: executor-owned `run`, `execute`, and
  `reload` schemas independent of plugin tool resources;
- `pyproject.toml` and setup/plugin loading: remove `_too` from registered and
  prepared toolsets while retaining its reserved namespace;
- `src/toolang/execution/executor/prepare.py`: separate public tools, runtime
  actions, effective routes, and State-bound instructions in the Agic frame;
- `src/toolang/execution/executor/runs/agic.py`: dispatch and correlated failure
  handling for run, execute, and reload;
- `src/toolang/execution/executor/steps/` and `executor.py`: Handoff Step commit,
  binding replacement, lineage checks, and same-Run dispatch;
- `src/toolang/execution/{types,events,records}.py` and persistence: typed
  handoff metadata and exact replay;
- inspection and execution progress: derive semantic handoff presentation only
  from events and records; and
- `docs/program.md`, `docs/executor.md`, `docs/selectors.md`, and `docs/tools.md`:
  current language and runtime behavior.

## Acceptance Tests

1. An Agic without `hands` receives no `_too/run`, even when every public tool
   is allowed; an Agic without `handoffs` receives no `_too/execute`.
2. `tools` directives and tool ceilings cannot add, remove, or name `_too`
   actions, and public tool listing never contains them.
3. Exact hand and handoff refs validate, duplicate or malformed refs fail, and
   routing directives on Flows or with non-`=` operators fail.
4. Existing Flow Run Statements remain source-authorized, create ordinary Run
   Steps, and never consult Agic routes.
5. Missing well-formed route targets do not invalidate State, are omitted from
   instructions, and become resolved after a State reload adds their export.
6. The route catalog includes only the resolved union, marks each entry with
   `run`, `execute`, or both, remains deterministic, and obeys all bounds.
7. `_too/run` accepts only captured hands, creates one ordinary Run Step and
   child Run, returns its result, and resumes the caller.
8. `_too/run` rejects non-hands and any target already present in the current or
   an active ancestor runnable lineage through a recoverable failed Run Step
   without hiding infrastructure failures.
9. A pre-commit execute failure returns one correlated error and the original
   Agic performs another Model Call.
10. A successful execute creates one Handoff Step, no child Run and no additional
   `RunBegin`; target Steps continue under the same Run ID and its result becomes
   the Run output.
11. Successful execute discards caller messages and continuation, resets
    per-Agic call counters for a target Agic, and retains tree-wide accounting.
12. Chained handoffs preserve one Run and ordered lineage; self, repeated
    lineage, and active-ancestor targets are rejected before commit.
13. A successful execute action is terminal for the caller, while target
    execution failure fails the same Run without caller recovery.
14. The entry runnable's output contract survives handoff; a target result that
    cannot satisfy it fails the same Run without caller recovery.
15. Reload-plus-run uses captured hand authorization and current target State;
    execute is rejected when accompanied by another action.
16. Records reopen with exact entry runnable and handoff lineage, inspection
    reproduces the transition, and progress never exposes `_too` names.
17. Retry rejects succeeded handoff history and rerun creates a normal new root
    Run.
18. Default offline verification passes.

## Risks

- `run` and `execute` are near-synonyms without the injected routing rule;
  action descriptions and the bounded instruction must remain concise and
  identical across adapters.
- Same-Run replacement introduces an executable timeline where records
  currently have one entry runnable; the Handoff Step must be the only source
  of transition truth rather than mutating `RunRecord.runnable`.
- Removing `_too` from public tool resources changes recently implemented
  source that used `tools = _too/run`; validation must fail directly and point
  authors to `hands`.
- Allowing unresolved route refs improves module sharing but can hide typos;
  inspection must distinguish authored unresolved routes from resolved routes.
- Successful handoff deliberately abandons the caller's provider continuation;
  adapter and transcript tests must ensure it is never sent to the target.

## Open Questions

None.
