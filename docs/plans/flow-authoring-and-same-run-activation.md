# Define Flow Authoring And Same-Run Activation

## Status

Proposed.

## Goal

Let an agent save a complete home flow module, explicitly activate the latest
valid prepared state at the next execution-step boundary, and call a public
runnable from that state within the same root run.

The feature builds on home flow discovery from Phase 1. Saving source and
activating state remain separate actions. Existing next-root-run refresh and
last-valid watcher behavior remain unchanged.

## Success Criteria

- A model can read and atomically save `flows/<name>.too` through reserved core
  actions without receiving a general root-filesystem mutation capability.
- Core actions can create only agent-home flow modules. They cannot create root
  flows or modify `agent.too`.
- Saving source does not prepare or activate state.
- An explicit state action refreshes through the process-owned `StateWatcher`
  and returns the existing layered diagnostics when the authored candidate is
  invalid.
- A valid state activation takes effect before the next step in the same root
  run, while already accepted runs keep their immutable program and resource
  snapshots.
- A model can call an agic or flow from the active public runnable catalog. The
  call is recorded as a normal child run and its result returns to the model.
- Every model call receives a runtime-owned description of the public
  runnables it may call, including their signatures.
- Existing public core toolsets use the canonical short names `fs`, `web`,
  `shell`, and `service` without duplicate legacy model names.
- State transitions are durable run controls with the old and new state
  fingerprints.
- `_me`, `_too`, and `_hat` are core-owned namespaces that cannot be registered
  by user-facing built-ins or external toolset plugins, and progress renderers
  can recognize them without plugin-specific heuristics.
- Existing static Flow calls, next-root refresh, and watcher publication remain
  unchanged.

## Scope

This phase includes:

- reserved model-facing core actions for reading and saving home flow source,
  applying state, and calling public runnables;
- canonical public toolset renames from `filesystem`, `web_search`, and
  `service_use` to `fs`, `web`, and `service`, with `shell` unchanged;
- safe, optimistic full-file flow updates;
- one mutable state head per active root run tree;
- `next_step` state-activation controls and durable state-transition facts;
- dynamic agic and flow child calls produced by an agic model call;
- runtime-owned public-runnable instructions refreshed at model boundaries;
- internal-action progress projection; and
- local Chat, hosted API, sandbox, and one-shot execution composition.

This phase does not include:

- `run "name"` or any other Toolang grammar change;
- dynamic runnable expressions in authored Flow statements;
- automatic activation after a save;
- root flow discovery or agent-authored root files;
- shared-flow wiring, `with` attachments, or marketplace installation;
- flow deletion or rename actions;
- same-run `AgentSetup`, plugin, environment, model-catalog, or policy changes;
- external API or CLI endpoints for state activation;
- compatibility aliases that expose old and new public tool names together;
- retry across a root run that applied more than one state version; or
- compatibility for retrying historical runs that captured legacy public tool
  identities.

The quoted dynamic Flow syntax remains the final, tree-sitter-dependent phase.

## Vocabulary And Invariants

This phase distinguishes three state values:

| Value | Meaning |
| --- | --- |
| entry state | The `AgentState` captured when the root run is accepted |
| state head | The state available to future dynamic runnable calls in that root tree |
| bound state | The immutable state captured by one accepted root or child run |

The state head initially equals the entry state. It may advance only through an
explicit successful state-apply action. A `BoundRun` never changes state after
acceptance.

Consequently:

- the currently executing agic or flow continues against its bound state;
- its existing static Flow statements resolve against that same bound state;
- a later dynamic public call resolves against the current state head;
- that child captures the selected head as its bound state; and
- static descendants of the new child use the child's bound state normally.

This preserves coherent module-local source, structs, caps, and private helper
resolution while allowing new public modules to be used in the same root tree.

## Tool Namespace Model

Tool namespaces identify the domain an action affects. Public capabilities that
interact with the external world have ordinary names:

| Namespace | Replaces | Domain |
| --- | --- | --- |
| `fs` | `filesystem` | agent-home filesystem |
| `web` | `web_search` | web search and retrieval |
| `shell` | unchanged | command execution |
| `service` | `service_use` | service-cap interaction |

The implementation changes both the built-in toolset identity and its public
namespace. Repository-owned selectors, configuration, examples, documentation,
and tests migrate to the canonical names. Old and new model tools are not
coexposed, and this phase adds no legacy selector or configuration aliases.

A single leading underscore marks a core-owned model-facing namespace:

| Namespace | Meaning | This phase |
| --- | --- | --- |
| `_me` | the current agent's authored and persistent state | flow read and save |
| `_too` | the Toolang language, executor, and current run tree | state apply and dynamic run |
| `_hat` | Human Agent Teaming | reserved; no communication actions yet |

`_hat` expands to **Human Agent Teaming**. It owns future human-agent,
agent-agent, and team communication or coordination actions rather than a
specific transport.

All toolset and public namespaces beginning with `_` are reserved for the
Toolang/OpenHat core. User-facing built-ins and external toolset plugins must
use ordinary namespaces and cannot return a model tool name beginning with
`_`. The existing `namespace__leaf` encoding remains unchanged.

Phase 2 defines four core actions:

```text
_me__flow_get
_me__flow_save
_too__state_apply
_too__run
```

They use the model provider's existing tool-definition and tool-call protocol,
and their calls remain durable tool steps. They are owned and invoked by the
execution core, not loaded through the `toolang.toolset` entry-point family.

Core action definitions participate in the existing effective tool selection.
`tools = none`, an explicit Toolang tool selector, and caller or agent tool
ceilings can therefore remove them. With the normal default tool selection they
are available. They count toward the existing agic tool-call limit.

Internal classification is derived from the core namespace identity. It is not
inferred from a Python package path or plugin name. Progress may distinguish
`_me`, `_too`, and future `_hat` actions without matching individual leaf names.

## Flow Source Actions

`_me__flow_get` accepts a canonical public flow name and returns:

```text
name
path            home-relative flows/<name>.too
source          exact UTF-8 source text
digest          SHA-256 of the source bytes
```

`_me__flow_save` accepts:

```text
name
source
if_digest?      expected current SHA-256 when replacing different bytes
```

The action derives the destination from the current `AgentLayout`; callers do
not supply a directory. It validates the same canonical filename rules used by
Phase 1, rejects symlinked or non-regular destinations, creates `flows/` when
needed, writes a temporary file in that directory, and atomically replaces the
target.

Save behavior is optimistic and idempotent:

- a missing target is created when `if_digest` is absent;
- identical existing bytes succeed with `changed = false`;
- replacing different existing bytes requires an exact `if_digest`; and
- a digest mismatch fails without changing the file.

The result contains `path`, `digest`, byte count, and `changed`. A later update
can use the returned digest. `flow_get` supplies the digest for an existing
user-authored file, preventing an agent from silently overwriting a concurrent
user edit.

The save action deliberately does not parse, prepare, or apply the source. An
invalid file remains authored source, the watcher keeps its last valid state,
and a later state-apply action returns the normal structured diagnostics. This
keeps file mutation and state publication independent.

## Explicit State Activation

`_too__state_apply` has no source or path arguments. It asks the
process-composition layer for one atomic watcher refresh result containing:

```text
state           latest last-valid AgentState
diagnostics     diagnostics for the latest rejected candidate, if any
```

The executor depends on this narrow callback and does not import or construct a
watcher. Local Chat and hosted `AgentCore` pass their existing watcher. One-shot
execution constructs the same controller for its materialized agent layout.

The action behaves as follows:

1. Refresh the watcher through its existing metadata fast path.
2. If diagnostics are present, do not change the root state head and return
   `applied = false` with the complete ordered diagnostics.
3. If the valid state fingerprint already equals the state head, return an
   idempotent no-op.
4. Otherwise, durably accept a `state_apply` control for the root run and
   activate the exact refreshed `AgentState` at the current tool step's
   `next_step` boundary.
5. Return the old fingerprint, new fingerprint, and new public runnable
   catalog.

Activation completes after the state-apply action succeeds and before another
model, tool, Flow statement, or dynamic child call begins. This makes an
ordered model tool-call batch useful: `flow_save`, `state_apply`, and `run` can
execute sequentially, with the run action observing the new state.

There is no automatic fallback to the watcher's last-valid state when the most
recent authored candidate is invalid. Diagnostics must be repaired and the
action called again. Background watcher publication alone never changes an
active root tree's state head.

State-head mutation is serialized per root execution. Parallel branches may
save source concurrently, but each apply captures and installs one exact
watcher result under the root's state lock.

## Durable State Controls

`ControlKind` gains `state_apply`. Its payload contains:

```text
from_state      previous root state-head fingerprint
to_state        activated AgentState fingerprint
source          internal tool StepPath that requested the transition
```

The control targets the root run, uses `timing = next_step`, and follows the
existing `pending`, `applied`, `wontapply`, and `revoked` lifecycle. It is not a
public steer/stop endpoint and cannot be submitted by an API caller in this
phase.

The root start control continues to record the entry state. Every dynamically
accepted child start control records its own bound state, so inspection can
reconstruct which state selected each runnable. The state-apply controls form
the ordered root state timeline.

Prepared versions are already immutable and are not deleted on the preparation
hot path. This phase does not add state blobs to execution records.

Retry currently accepts one state for the whole reconstruction. A root run
containing an applied `state_apply` control is therefore rejected by retry with
an actionable message; rerun starts normally from the caller's current state.
The durable transition records leave exact multi-state retry as a future
extension rather than guessing a state.

## Dynamic Runnable Calls

`_too__run` accepts:

```text
runnable        name, agic:name, or flow:name
input?          primary JSON-compatible input
arguments?      JSON object of named inputs
```

Resolution uses only the current state head's public runnable catalog. Private
module helpers are never dynamically addressable. Kind-qualified references
enforce the requested kind, and unqualified references remain unambiguous by
the existing catalog rule.

Inputs are coerced against the target owner module's signature and structs.
This is a runtime JSON boundary, not a cross-module static type reference. The
new child is accepted below the internal tool step, captures the current state
head, and receives the root's immutable setup, limits, and ceilings. Resources
are recomputed for the selected module from that captured state without
expanding the root's policy.

The child remains a normal run with normal events, accounting, controls, and
failure propagation. Its typed output is projected into a JSON-compatible tool
result containing the child run ID, canonical runnable, Toolang output type,
and value. Text, scalar, JSON, struct, and Part values use the existing runtime
protocol codecs rather than stringification.

Returning the child result as a tool result naturally causes the parent agic's
next model call to evaluate or present it. No new model-provider protocol is
required.

## Runnable Instructions

The execution core appends a runtime-owned `<available-runnables>` block to
model instructions when `_too__run` is in the effective tool set. The block
is independent of authored `instruct` declarations and contains only public
catalog entries:

```text
state fingerprint
canonical ref and kind
primary input type, if present
named parameter names, types, and optionality
output type, if present
declaration documentation, if present
```

The block tells the model to call `_too__run` rather than inventing a normal
plugin tool call. It omits private helpers and module filesystem paths.

The annex is rebuilt at every model-call boundary from the root state head.
After `state_apply`, the next model call therefore sees the new catalog even
though its currently executing agic remains bound to its original program.

## Progress And Inspection

Internal calls remain durable, but user-facing progress never prints their raw
reserved names as ordinary plugin tools:

- flow reads are quiet;
- flow saves render a compact `Saved flow <name>` fact when changed;
- state activation renders the resulting state fact or its validation
  diagnostic; and
- dynamic run wrappers are suppressed while their normal child run remains
  visible.

Raw run inspection still exposes the tool call and state-apply control for
debugging. Logs retain the reserved identity.

## Failure Semantics

- A flow read, save, or optimistic-concurrency failure fails that tool step and
  returns a normal tool error to the model.
- Invalid authored state is an expected successful state-apply result with
  `applied = false` and structured diagnostics, allowing the model to repair
  the source.
- A valid state that cannot be durably recorded is not installed as the root
  state head.
- Dynamic input or runnable resolution failure fails the internal run tool
  step without accepting a child run.
- A failure after child acceptance follows existing child-run and enclosing
  step error-pointer behavior.
- Ending the root run marks any unclaimed state-apply control `wontapply` using
  the existing control cleanup path.

## Implementation Touchpoints

- `src/toolang/execution/tools/`: core action definitions, flow file safety,
  and internal invocation contracts;
- `src/toolang/plugin/toolsets/registry.py` and loading: reserved-name
  enforcement, canonical public toolset names, and normal resource selection
  for core definitions;
- `src/toolang/setup/`: include core action definitions in the immutable setup
  tool snapshot without plugin entry points;
- `src/toolang/state/watcher.py`: return one atomic refresh result with
  diagnostics;
- `src/toolang/execution/types.py`, `records.py`, `store.py`, and schemas:
  `state_apply` control vocabulary, codecs, persistence, and inspection;
- executor binding, agic model/tool steps, and resources: root state head,
  action effects, dynamic child acceptance, and per-call instruction annex;
- local Chat, script, hosted core, and sandbox composition: supply the state
  refresh controller;
- execution-progress projectors and renderers: internal action projection; and
- tool, watcher, record, execution, API, CLI, and progress tests.

## Acceptance Tests

1. Saving a new unnamed flow writes only
   `agents/<agent>/flows/<name>.too` atomically and returns its digest.
2. Saving identical bytes is idempotent; replacing changed bytes requires the
   exact previous digest; a concurrent user edit is not overwritten.
3. Symlink, traversal, non-canonical name, root-flow, and `agent.too` targets
   are impossible through the flow actions.
4. Saving valid or invalid source does not change the root state head.
5. Applying an invalid candidate returns ordered Phase 1 diagnostics and keeps
   the previous root state head.
6. Repairing the file and applying state records one applied `state_apply`
   control with exact old and new fingerprints.
7. A state apply takes effect before the next tool or model step, including
   within one ordered model tool-call batch.
8. An already accepted flow continues its old static statements after apply.
9. A dynamic call after apply resolves and executes a newly saved public flow
   using its own module structs, caps, and helpers.
10. Dynamic unqualified, `agic:`, and `flow:` references enforce the existing
    public catalog and reject private helpers or kind mismatches.
11. Dynamic input coercion and output projection cover scalar, JSON, struct,
    and Part values without cross-module static type lookup.
12. The dynamic child start record stores the applied state fingerprint and is
    parented below the internal tool step.
13. Tool directives and tool ceilings can exclude core actions; default tool
    selection includes them.
14. User-facing and external toolset plugins cannot register an underscore-led
    namespace or model name; `_hat` is reserved as Human Agent Teaming without
    adding communication actions in this phase.
15. Every eligible model request lists the exact public catalog and signatures;
    the request after state apply lists the new flow.
16. Progress hides raw internal names, shows meaningful save/apply facts, and
    renders the dynamic child normally.
17. Concurrent state applies serialize complete state objects and never combine
    modules or caps from different prepared versions.
18. Retry rejects a root containing an applied state transition; rerun uses the
    current normal root-run state.
19. Next-root-run refresh and roots with no state apply preserve Phase 1
    behavior and records.
20. Built-in tool identities and repository-owned selectors migrate to `fs`,
    `web`, `shell`, and `service`; no model request exposes both a legacy and a
    canonical name.
21. The default offline verification suite remains deterministic and passes.

## Risks

- Core actions must participate in policy without becoming externally
  registerable plugins; mixing the two registries would weaken reserved-name
  ownership.
- The public toolset rename is intentionally breaking. Authored selectors,
  configuration, and historical run resource snapshots that contain legacy
  identities do not silently resolve to the new tools.
- Updating only the root state head, rather than an accepted `BoundRun`, is the
  central isolation rule. Rebinding an active run would mix program, struct,
  and cap versions.
- Dynamic child execution occurs beneath a tool step. Event ordering and error
  pointers must remain valid when the child streams progress before the tool
  result is committed.
- A full flow source string can be large in a tool call. Existing model and run
  limits remain authoritative; progress and logs must keep using bounded
  previews rather than echoing source.
- State refresh and durable control insertion cross subsystem boundaries. The
  state head must not advance unless both the prepared state and transition
  record are available.

## Open Questions

None.
