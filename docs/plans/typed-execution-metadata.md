# Normalize Run Preparation Vocabulary

## Goal

Replace overlapping input and resource-ceiling terminology with five explicit
execution concepts while preserving current behavior:

- `RunBindings`: the model and runnable selected for a run;
- `RunLimits`: the limits adopted by a run tree;
- `RunnableInput`: resolved primary and named inputs adopted by a run;
- `AgentResources`: the concrete resources available at an execution point;
- `AgentCeiling`: selector lists that can only narrow `AgentResources`.

Raw chat input remains a `str`; there is no separate raw-text type alias. Chat
parsing distinguishes a standalone `ChatCommand` from one run submission.
`CallInput[str]` holds unresolved input and arguments; `RunOverride` holds the
settings and root-runnable selection for that submission.

## Success Criteria

- `parse_chat_input(str)` produces `ChatCommand | tuple[RunOverride, CallInput[str]]`;
  execution resolution produces `RunnableInput`.
- `RunSpec` and bound runs carry one `RunnableInput` instead of separate primary
  and named fields.
- `AgentCeiling` remains selector-based, while `_ResolvedAgentCeiling` and
  `_RunCeiling` are replaced by `AgentResources`; no `RunResources` or
  `AvailableResources` type exists.
- Resource preparation follows `AgentResources + AgentCeiling* ->
  AgentResources` through shared selector-list operations.
- Preparation controls store explicit `bindings`, `limits`, `input`, and final
  `resources` snapshots.
- A run is a root exactly when `parent is None`; durable records do not store a
  redundant root field or root-run identifier.
- Current execution, retry, rerun, child-run, CLI, API, and inspection behavior
  remains unchanged.

## Scope

This refactor covers caller parsing, policy resolution, executor preparation,
child-run derivation, execution records/events, SQLite persistence, projections,
documentation, and tests.

It does not change statuses, errors, request identity, control timing, retry
cuts, command syntax, resource-selection semantics, or `given`/`noted` values.
It does not persist state fingerprints, agent ceilings, environment variables,
plugin instances, provider credentials, or other new setup provenance.

## Input Model

The parsed alternatives are:

```text
ChatInput = ChatCommand | tuple[RunOverride, CallInput[str]]
RunnableInput = CallInput[Value]
```

Run-only surfaces produce the same aggregate override and source-input pair.
Commands occupy one Chat submission and never submit a run. A leading override
prefix requires supplied Call Input; direct root runnable calls may omit it when
the signature permits. See [Input Syntax](../input-syntax.md) for the grammar.

Resolution expands Content and coerces arguments into `CallInput[Value]`.
Child runs receive typed values directly. Empty input is accepted only when the
signature permits it; absence and explicit empty values remain distinct.

## Overrides And Resources

`RunOverride` contains parsed submission settings and root-runnable selection.
`SessionSetting` owns Chat defaults; `RunCommand` retains low-level restart
provenance. These execution values are distinct from authored command and
run-directive syntax.

`AgentCeiling` contains the current model, tool, and cap selector lists.
`AgentResources` contains the resolved model selectors and concrete tool/cap
bindings used by execution. Its record codec emits only stable model, tool, and
cap identities; it never emits runtime objects or secrets.

Resource construction has two operations:

1. build the initial `AgentResources` from immutable setup and state snapshots;
2. apply zero or more `AgentCeiling` values without expanding the base set.

Selector matching, ordering, deduplication, and directive `=`, `+=`, and `-=`
behavior use shared selector-list helpers. Flow and agic execution select their
base resource set and use the same filtering operation; workflow-specific
ceiling types and resolver functions are removed.

## Records And Controls

Preparation controls (`run`, `rerun`, `retry`, and child `run`) store these
top-level typed fields:

```text
bindings: RunBindings
limits: RunLimits
input: RunnableInput
resources: AgentResources
```

Each is the effective snapshot for that accepted preparation. Retry records a
self-contained snapshot even when its input is unchanged. Steer uses a message
and cancel uses a reason rather than overloading `RunnableInput`.

`AgentCeiling` is not durable run truth and is not stored. Historical ceiling
data may be decoded only for migration and is not projected as final resources.
Historical controls that cannot reconstruct final resources use an explicit
legacy absence; all newly accepted preparation controls require resources.

`RunRecord.context` remains for current facts outside this scope, but no longer
owns preparation input, bindings, limits, resources, or `root`. A root run has
`parent is None`. Consumers that require `root_run_id` derive it by following
`parent.run`; caller-facing schemas may continue exposing that derived value.
The executor may retain an ephemeral root ID for active-tree ownership and
shared limit accounting.

## Implementation Touchpoints

- `lang/input.py`, `execution/input.py` submission parsing, CLI chat/script, and scheduler;
- `base/types/policy.py`, resource selector helpers, setup configuration, and
  executor resource preparation;
- `execution/executor`, `records.py`, `events.py`, `store.py`, and `schemas.py`;
- execution, input, CLI, API, store, retry/rerun, and child-run tests;
- executor and run-record documentation.

## Acceptance Tests

- `ChatInput` alternatives and `CallInput[str]` validation follow the current
  prefix plan, including required input after overrides and signature-based
  empty root calls.
- Resolved primary/named values round-trip through `RunnableInput`, controls,
  events, and SQLite without losing names or declared types.
- Setup, session, run, runnable, flow, and agic resource restrictions select the
  same ordered resources as before and never widen a base set.
- Root run, rerun, retry, direct child, and parallel child controls store the
  four preparation snapshots; steer and cancel reject preparation-only fields.
- Root identity is derived correctly for roots, nested children, parallel
  children, and reruns.
- Historical database migration remains readable without inventing resolved
  resources that were never stored.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest` pass.

## Risks

- Resource values contain runtime bindings while durable records require stable
  identities; codecs must make that boundary explicit.
- Child controls are currently accepted before all local resources are resolved;
  acceptance must move after preparation without changing event order.
- Removing stored root identity makes ancestry integrity important; store writes
  must reject missing or cyclic parent ownership.
