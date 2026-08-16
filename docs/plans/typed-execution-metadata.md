# Normalize Run Preparation Vocabulary

## Goal

Replace overlapping input and resource-ceiling terminology with five explicit
execution concepts while preserving current behavior:

- `RunBindings`: the model and runnable selected for a run;
- `RunLimits`: the limits adopted by a run tree;
- `RunInput`: resolved primary and named inputs adopted by a run;
- `AgentResources`: the concrete resources available at an execution point;
- `ResourceFilter`: selector lists that can only narrow `AgentResources`.

Raw caller text is `RunContent`. Parsing distinguishes quick commands,
override-only input, and runnable input. `RunnableInput` becomes `RunInputText`
for syntax-valid primary and named source text, and `PolicyCommand` becomes
`RunOverride`.

## Success Criteria

- `RunContent` resolves as `QuickCommand | RunOverride+ | RunOverride* +
  RunInput`; the parser uses `RunInputText` before producing `RunInput`.
- `RunSpec` and bound runs carry one `RunInput` instead of separate primary and
  named fields.
- `AgentCeiling`, `_ResolvedAgentCeiling`, and `_RunCeiling` are replaced by
  `ResourceFilter` and `AgentResources`; no `RunResources` or
  `AvailableResources` type exists.
- Resource preparation follows `AgentResources + ResourceFilter* ->
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
It does not persist state fingerprints, resource filters, environment variables,
plugin instances, provider credentials, or other new setup provenance.

## Input Model

`RunContent` is the raw text supplied by chat, script, task, chore, or another
text surface. Its semantic alternatives are:

```text
RunContent -> QuickCommand
            | RunOverride+
            | RunOverride* + RunInput
```

The parsing boundary represents the runnable branch as `RunOverride* +
RunInputText`; resolution expands includes and declared types to produce the
final `RunInput`. Programmatic callers may construct `RunInput` directly.

All input types are frozen, slotted dataclasses. `RunInputText` retains primary
source text and immutable `(name, source)` pairs. `RunInput` retains one primary
`Percept` and canonical immutable named values, including the declared type
needed for durable round trips. Empty primary and named inputs remain valid.

`RunSpec` and the bound executor value each expose `input: RunInput`. Child runs
derive a new `RunInput` directly from parent locals; they do not round-trip
through `RunContent` or `RunInputText`.

## Overrides And Resources

`RunOverride` is the preparation-only representation of current `allow`,
`default`, and `limit` commands. Existing config keys, environment variables,
CLI flags, and colon-command spellings remain unchanged.

`ResourceFilter` contains the current model, tool, and cap selector lists.
`AgentResources` contains the resolved model selectors and concrete tool/cap
bindings used by execution. Its record codec emits only stable model, tool, and
cap identities; it never emits runtime objects or secrets.

Resource construction has two operations:

1. build the initial `AgentResources` from immutable setup and state snapshots;
2. apply zero or more `ResourceFilter` values without expanding the base set.

Selector matching, ordering, deduplication, and directive `=`, `+=`, and `-=`
behavior use shared selector-list helpers. Flow and agic execution select their
base resource set and use the same filtering operation; workflow-specific
ceiling types and resolver functions are removed.

## Records And Controls

Preparation controls (`start`, `rerun`, `retry`, and child `start`) store these
top-level typed fields:

```text
bindings: RunBindings
limits: RunLimits
input: RunInput
resources: AgentResources
```

Each is the effective snapshot for that accepted preparation. Retry records a
self-contained snapshot even when its input is unchanged. Steer uses a message
and stop uses a reason rather than overloading `RunInput`.

`ResourceFilter` is not durable run truth and is not stored. Historical ceiling
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

- `lang/input.py`, execution call/policy parsing, CLI chat/script, and scheduler;
- `base/types/policy.py`, resource selector helpers, setup configuration, and
  executor resource preparation;
- `execution/executor`, `records.py`, `events.py`, `store.py`, and `schemas.py`;
- execution, input, CLI, API, store, retry/rerun, and child-run tests;
- executor and run-record documentation.

## Acceptance Tests

- `RunContent` alternatives and `RunInputText` validation retain current parse
  behavior, including override-only and empty runnable input.
- Resolved primary/named values round-trip through `RunInput`, controls, events,
  and SQLite without losing names or declared types.
- Setup, session, run, runnable, flow, and agic resource restrictions select the
  same ordered resources as before and never widen a base set.
- Root start, rerun, retry, direct child, and parallel child controls store the
  four preparation snapshots; steer and stop reject preparation-only fields.
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
