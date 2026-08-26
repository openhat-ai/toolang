# Define Flow Authoring And Same-Run Activation

## Status

Proposed.

## Goal

Let an agent save a home flow module, explicitly activate the latest valid
prepared state at the next step boundary, and call a public runnable from that
state within the same root run.

Saving source and applying state are separate actions. Phase 1 next-root
refresh, last-valid watcher behavior, and static Flow calls remain unchanged.

## Success Criteria

- Core actions can read and atomically save only
  `agents/<agent>/flows/<name>.too`.
- Saving never prepares or applies state.
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
| `_me` | current agent authored state | flow read and save |
| `_too` | Toolang executor and run tree | state apply and dynamic run |
| `_hat` | Human Agent Teaming | reserved only |

`_hat` covers future human-agent, agent-agent, and team coordination independent
of transport. User-facing built-ins and external plugins cannot register a core
toolset name. Tool names are verb-first.

This phase adds:

```text
_me__get_flow
_me__save_flow
_too__apply_state
_too__run
```

These are execution-core actions, not `toolang.toolset` plugins. They use the
normal model tool-call protocol, remain durable tool steps, count toward agic
tool-call limits, and participate in existing tool directives and ceilings.

## Flow Source Actions

`_me__get_flow(name)` returns the home-relative path, exact UTF-8 source, and
SHA-256 digest.

`_me__save_flow` accepts:

```text
name
source
if_digest?      required when replacing different existing bytes
```

The destination is always derived as `flows/<name>.too`. The action applies the
Phase 1 filename rules, rejects traversal, symlinks, and non-regular targets,
creates `flows/` if needed, and atomically replaces through a temporary sibling.

Save is optimistic and idempotent:

- a missing target is created without `if_digest`;
- identical bytes return `changed = false`;
- different existing bytes require the exact current digest; and
- a mismatch leaves the file unchanged.

The result includes path, digest, byte count, and `changed`. Save does not parse
or prepare source. Invalid authored source remains on disk for repair; the
watcher retains its last valid state.

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

`_too__apply_state()` requests one atomic result from the process-owned watcher:
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

It targets the root run, uses `timing = next_step`, and follows the existing
control lifecycle. The root start control retains the entry fingerprint; each
dynamic child start control records its bound fingerprint. No public API or CLI
can submit `state_apply` in this phase.

Retry rejects a root containing an applied state transition because current
retry reconstruction accepts one state. Rerun starts from the caller's current
state. Prepared state blobs are not copied into execution records.

## Dynamic Runnable Calls

`_too__run` accepts:

```text
runnable        name, agic:name, or flow:name
input?          primary JSON-compatible input
arguments?      JSON object of named inputs
```

It resolves only the state head's public catalog; private helpers remain
inaccessible and kind-qualified refs enforce kind. Inputs use the target
module's signature and structs. This runtime JSON boundary does not create a
cross-module static type namespace.

The selected runnable is accepted below the internal tool step as a normal
child run. It captures the state head and the root's setup, limits, and ceilings;
module resources are recomputed without expanding policy. Its normal events,
accounting, controls, and failures remain visible. The typed output returns as a
protocol-encoded tool result with child run ID, canonical runnable, type, and
value.

When `_too__run` is effective, the execution core appends an
`<available-runnables>` block to model instructions. It lists only public refs,
documentation, input and parameter types, optionality, and output types. The
block is runtime-owned and rebuilt from the state head at every model boundary,
including after apply.

## Progress And Failures

Progress classifies core actions by `_me`, `_too`, and future `_hat`, not Python
package or leaf-name heuristics:

- flow reads are quiet;
- changed saves show `Saved flow <name>`;
- apply shows the new state or validation diagnostic; and
- `_too__run` wrappers are hidden while the child run remains visible.

Raw inspection and logs retain the calls and controls. Read/save conflicts are
normal tool errors. Invalid state is a successful apply result with
`applied = false`. Dynamic resolution fails before child acceptance; failures
after acceptance use existing child-run error pointers. The state head advances
only after the prepared state and durable transition are both available.

## Scope

Included:

- the four core actions and reserved toolset-name enforcement;
- public toolset renames;
- root state head and durable `next_step` apply;
- dynamic agic/flow children and runnable instructions;
- progress, inspection, and all process compositions; and
- unit, integration, API, CLI, and progress coverage.

Excluded:

- Toolang grammar or tree-sitter changes, including `run "name"`;
- dynamic expressions in authored Flow statements;
- automatic apply after save;
- root flows, shared-flow wiring, `with` attachment, or installation;
- flow delete/rename actions;
- same-run setup, plugin, environment, model-catalog, or policy changes;
- public state-apply API/CLI;
- legacy tool-name aliases or historical-run migration; and
- multi-state retry.

## Implementation Touchpoints

- `execution/tools`, tool registry/loading, and setup: core definitions,
  reserved names, file safety, public renames, and policy selection;
- `state/watcher.py`: atomic refresh result with diagnostics;
- execution types, records, store, and schemas: `state_apply` persistence;
- executor binding and agic model/tool paths: state head, action effects, dynamic
  children, and runnable instructions;
- Chat, script, hosted core, and sandbox composition: state controller; and
- progress renderers plus tool, state, execution, API, and CLI tests.

## Acceptance Tests

1. Flow get/save is confined to direct home flow files and safely handles
   create, identical save, digest-guarded replace, traversal, and symlinks.
2. Saving valid or invalid source never changes the root state head.
3. Invalid apply returns ordered Phase 1 diagnostics; repaired apply records
   exact old/new fingerprints and takes effect at the next step.
4. Active static execution keeps its bound state after apply.
5. A later dynamic call executes a newly applied public flow with its module
   structs, helpers, caps, input coercion, and typed output.
6. Dynamic refs enforce public visibility and optional kind qualification.
7. The child is parented below the internal tool step and records the applied
   state fingerprint.
8. Every eligible model request lists the active public signatures, including
   the request following apply.
9. Tool directives and ceilings can exclude core actions; defaults include them.
10. Toolset and tool names enforce the portable component grammar; core toolset
    names are reserved and `_hat` means Human Agent Teaming.
11. Progress hides raw wrappers and preserves useful save, apply, child, and
    diagnostic output.
12. Concurrent applies install whole serialized state snapshots.
13. Retry rejects an applied transition; rerun uses current state.
14. Built-in identities migrate to `fs`, `web`, `shell`, and `service` without
    exposing legacy duplicates.
15. The default offline verification suite passes.

## Risks

- Public tool renames intentionally break legacy selectors, configuration, and
  historical resource snapshots.
- Rebinding an active run would violate module isolation; only the root state
  head may change.
- A dynamic child streams below a tool step, so event order and error pointers
  must remain valid.
- Flow source can be large; progress and logs must keep bounded previews.
- State must not advance without both a valid prepared snapshot and durable
  transition record.

## Open Questions

None.
