# Define Flow Authoring And Same-Run Activation

## Status

Proposed.

## Goal

Let an agent manage home flow modules, explicitly activate the latest valid
prepared state at the next step boundary, and call a public runnable from that
state within the same root run.

Changing source and applying state are separate actions. Phase 1 next-root
refresh, last-valid watcher behavior, and static Flow calls remain unchanged.

## Success Criteria

- Core actions can list, read, create, update, and delete only
  `agents/<agent>/flows/<name>.too`.
- Source mutations never prepare or apply state.
- Explicit apply returns Phase 1 diagnostics or advances the root state head at
  `next_step` without rebinding accepted runs.
- A model can call a public agic or flow from the active state head as a normal
  child run.
- Each eligible model call receives the active public runnable signatures.
- State transitions are durable controls with old and new fingerprints.
- Core toolset names are reserved, and public built-in toolsets use the short
  canonical names `fs`, `web`, `shell`, and `service`.

## Toolset And Tool Names

Public toolsets that interact with the external world use ordinary names:

| Canonical | Replaces | Domain |
| --- | --- | --- |
| `fs` | `filesystem` | agent-home filesystem |
| `web` | `web_search` | web search and retrieval |
| `shell` | unchanged | command execution |
| `service` | `service_use` | service-cap interaction |

The built-in toolset names change. Repository configuration, selectors,
examples, docs, and tests migrate together. Legacy and canonical names are not
coexposed, and no compatibility aliases are added.

Canonical model-facing and persisted names use `toolset__tool`. Frontends may
render the same reference as `toolset.tool`, but must translate it back before
dispatch; the dotted form is not an identity or protocol value.

Names use conservative, provider-portable grammar:

- user-facing toolset names and tool names match
  `[A-Za-z]+(?:_[A-Za-z]+)*`; and
- core toolset names match `_[A-Za-z]+(?:_[A-Za-z]+)*`.

This excludes leading or trailing underscores and `__` inside either component.
Only core toolsets have the single leading underscore:

| Toolset | Meaning | This phase |
| --- | --- | --- |
| `_me` | current agent authored state | existing authoring tools plus flows |
| `_rt` | runtime and run tree | state apply and dynamic run |
| `_hat` | Human Agent Teaming | reserved only |

`_hat` covers future human-agent, agent-agent, and team coordination independent
of transport. User-facing built-ins and external plugins cannot register a core
toolset name. Tool names are verb-first.

The existing `agent_state` toolset becomes `_me`; its tools become verb-first:

```text
_me__list_tasks
_me__get_task
_me__create_task
_me__update_task
_me__list_chores
_me__get_chore
_me__create_chore
_me__update_chore
_me__list_psyches
_me__get_psyche
_me__create_psyche
_me__update_psyche
_me__delete_psyche
_me__list_skills
_me__get_skill
_me__create_skill
_me__update_skill
_me__delete_skill
_me__list_services
_me__get_service
_me__create_service
_me__update_service
_me__delete_service
_me__list_prompts
_me__get_prompt
_me__create_prompt
_me__update_prompt
_me__delete_prompt
_me__list_flows
_me__get_flow
_me__create_flow
_me__update_flow
_me__delete_flow
```

This phase also adds the executor-owned `_rt` tools:

```text
_rt__apply_state
_rt__run
```

Both use the normal model tool-call protocol, count toward agic tool-call
limits, and participate in existing tool directives and ceilings.
`_rt__apply_state` remains a durable tool step; `_rt__run` is an executor
intrinsic recorded as a run step. No legacy aliases are exposed.

## Flow Source Actions

`_me__list_flows()` lists direct flow modules by name with home-relative path,
SHA-256 digest, and byte count. It reads source files, not the active runnable
catalog, so unapplied modules remain visible.

`_me__get_flow(name)` adds the exact UTF-8 source to the same metadata.

The mutation actions are:

```text
create_flow(name, source)
update_flow(name, source, if_digest)
delete_flow(name, if_digest)
```

The destination is always derived as `flows/<name>.too`. All actions apply the
Phase 1 filename rules and reject traversal, symlinks, and non-regular targets.
Create makes `flows/` when needed and fails if the target exists. Update and
delete require the exact current digest; a mismatch leaves the file unchanged.
Create and update publish atomically through a temporary sibling. Identical
updates return `changed = false`.

Results include path, digest, byte count, and the mutation outcome. Mutations do
not parse or prepare source. Invalid authored source remains on disk for repair
or deletion; the watcher retains its last valid state.

## State Semantics

| Value | Meaning |
| --- | --- |
| entry state | state captured when the root run is accepted |
| state head | state used by later dynamic calls in that root tree |
| bound state | immutable state captured by one accepted run |

The state head starts at the entry state. Applying state changes only the head:

- active runs and their static calls continue with their bound state;
- later dynamic calls resolve against the head and capture it as their bound
  state; and
- static descendants then use that child's bound state normally.

This prevents mixing module source, structs, helpers, or caps across versions.

`_rt__apply_state()` requests one atomic result from the process-owned watcher:
the latest last-valid `AgentState` plus diagnostics for the newest candidate.
The executor receives this through a narrow callback; it does not own file
watching. Local Chat, hosted execution, sandboxes, and one-shot execution supply
the controller for their materialized layout.

Apply behavior:

1. Refresh through the existing metadata fast path.
2. If diagnostics exist, return `applied = false` and leave the head unchanged.
3. If the valid fingerprint equals the head, return an idempotent no-op.
4. Otherwise, record and install that exact state before the next model, tool,
   Flow statement, or dynamic call.

Background watcher publication never changes an active root's head. Apply is
serialized per root tree, so parallel branches cannot combine prepared states.

`ControlKind` gains `state_apply` with:

```text
from_state
to_state
source          requesting internal tool StepPath
```

It targets the root run, fixes `timing = next_step`, and follows the existing
control lifecycle. The action exposes no timing input, and the store rejects
other timings for this control kind. `immediate` would imply rebinding or
interrupting the active apply step, while `next_call` could let intervening
Flow or tool steps keep using stale state. `next_step` is therefore the first
safe, deterministic boundary after the action is durably recorded. The root
start control retains the entry fingerprint; each dynamic child start control
records its bound fingerprint. No public API or CLI can submit `state_apply`
in this phase.

Retry rejects a root containing an applied state transition because current
retry reconstruction accepts one state. Rerun starts from the caller's current
state. Prepared state blobs are not copied into execution records.

## Dynamic Runnable Calls

`_rt__run` accepts:

```text
runnable        name, agic:name, or flow:name
input?          JSON object containing the complete runnable input
```

Within `input`, `_` is the primary input and every other key is a named input
using the target parameter name. Missing, extra, and mistyped fields are
validated against the target signature. For example:

```json
{
  "runnable": "flow:research",
  "input": {"_": "Toolang", "depth": 3}
}
```

It resolves only the state head's public catalog; private helpers remain
inaccessible and kind-qualified refs enforce kind. Inputs use the target
module's signature and structs. This runtime JSON boundary does not create a
cross-module static type namespace.

The agic executor recognizes `_rt__run` before generic tool dispatch. It records
one direct `kind = run` step containing the model tool-call identity and dynamic
request, then accepts the selected runnable as that step's normal child run.
This matches authored Flow `run` behavior: child steps remain in the child run
and are not flattened into the agic parent. The original call is still present
in the model step, and its call ID correlates the run step and returned tool
result; no duplicate `kind = tool` wrapper is recorded.

`StepGiven` and `StoredStepGiven` gain `DynamicRunStepGiven(call, runnable)`,
where `call` preserves the model-emitted `_rt__run` call and `runnable` is the
resolved canonical qualified ref. It is valid only for `kind = run`; authored
run steps continue to store their `FlowStmt` unchanged.

This is an in-band runtime step, not a run control. Unlike steer or stop, it
does not asynchronously alter already planned execution: it selects a child,
waits for its value, and resumes the requesting model loop. The child captures
the state head and the root's setup, limits, and ceilings; module resources are
recomputed without expanding policy. Its normal events, accounting, controls,
and failures remain visible. The typed output returns as a protocol-encoded
tool result with child run ID, canonical runnable, type, and value.

When `_rt__run` is effective, the execution core appends an
`<available-runnables>` block to model instructions. It lists only public refs,
documentation, input and parameter types, optionality, and output types. The
block is runtime-owned and rebuilt from the state head at every model boundary,
including after apply.

## Progress And Failures

Progress classifies core actions by `_me`, `_rt`, and future `_hat`, not Python
package or leaf-name heuristics:

- flow lists and reads are quiet;
- mutations show `Created`, `Updated`, or `Deleted flow <name>`;
- apply shows the new state or validation diagnostic; and
- a dynamic `kind = run` step renders like an authored run and exposes its
  child without a synthetic tool wrapper.

Raw inspection and logs retain the model call, run step, child, and controls.
Source conflicts are normal tool errors. Invalid state is a successful apply
result with `applied = false`. Dynamic resolution fails before child acceptance;
failures
after acceptance use existing child-run error pointers. The state head advances
only after the prepared state and durable transition are both available.

## Scope

Included:

- the `agent_state` to `_me` migration, seven new core actions, and reserved
  toolset-name enforcement;
- public toolset renames;
- root state head and durable `next_step` apply;
- dynamic agic/flow children and runnable instructions;
- progress, inspection, and all process compositions; and
- unit, integration, API, CLI, and progress coverage.

Excluded:

- Toolang grammar or tree-sitter changes, including `run "name"`;
- dynamic expressions in authored Flow statements;
- automatic apply after source mutation;
- root flows, shared-flow wiring, `with` attachment, or installation;
- flow rename actions;
- same-run setup, plugin, environment, model-catalog, or policy changes;
- public state-apply API/CLI;
- legacy tool-name aliases or historical-run migration; and
- multi-state retry.

## Implementation Touchpoints

- `execution/tools`, tool registry/loading, and setup: core definitions,
  reserved names, file safety, public renames, and policy selection;
- `state/watcher.py`: atomic refresh result with diagnostics;
- execution types, records, store, and schemas: `state_apply` persistence;
- executor binding and agic model/tool paths: state head, action effects,
  dynamic run-step records and children, tool-result correlation, and runnable
  instructions;
- Chat, script, hosted core, and sandbox composition: state controller; and
- progress renderers plus tool, state, execution, API, and CLI tests.

## Acceptance Tests

1. Flow CRUD is confined to direct home flow files and safely handles listing,
   create conflicts, identical update, digest-guarded update/delete, traversal,
   and symlinks.
2. Creating, updating, or deleting source never changes the root state head.
3. Invalid apply returns ordered Phase 1 diagnostics; repaired apply records
   exact old/new fingerprints, fixes the control timing to `next_step`, and
   takes effect at that boundary; other state-apply timings are rejected.
4. Active static execution keeps its bound state after apply.
5. A later dynamic call executes a newly applied public flow with its module
   structs, helpers, caps, input coercion, and typed output.
6. Dynamic refs enforce public visibility and optional kind qualification;
   their single `input` object maps `_` to primary input and other keys to named
   inputs.
7. A dynamic call records one `kind = run` step below the agic run, parents its
   child below that step, records the applied state fingerprint, and adds no
   tool-step wrapper or flattened child steps.
8. Every eligible model request lists the active public signatures, including
   the request following apply.
9. Tool directives and ceilings can exclude core actions; defaults include them.
10. Toolset and tool names enforce the portable component grammar; core toolset
    names are reserved and `_hat` means Human Agent Teaming.
11. Progress renders dynamic and authored run steps consistently and preserves
    useful mutation, apply, child, and diagnostic output.
12. Concurrent applies install whole serialized state snapshots.
13. Retry rejects an applied transition; rerun uses current state.
14. Built-in identities migrate to `fs`, `web`, `shell`, and `service`, while
    `agent_state` becomes `_me` with verb-first tools and no legacy duplicates.
15. The default offline verification suite passes.

## Risks

- Toolset and tool renames intentionally break legacy selectors, configuration,
  and historical resource snapshots.
- Rebinding an active run would violate module isolation; only the root state
  head may change.
- A dynamic run step must correlate the model call ID, child events, output,
  and error pointer without creating a second tool step.
- Flow source can be large; progress and logs must keep bounded previews.
- State must not advance without both a valid prepared snapshot and durable
  transition record.

## Open Questions

None.
