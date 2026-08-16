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
parsing distinguishes quick commands, override-only input, and runnable input.
`RunnableInputRaw` represents structured but unresolved primary and named source
text, while `PolicyCommand` becomes `RunOverride`.

## Success Criteria

- `parse_chat_input(str)` produces `QuickCommand | RunOverride+ |
  RunOverride* + RunnableInputRaw`; execution resolution produces `RunnableInput`.
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

The term chat input covers both the original `str` accepted by chat parsing and
the resulting `ChatInput` object. The parsed alternatives are:

```text
ChatInput -> QuickCommand
           | RunOverride+
           | RunOverride* + RunnableInputRaw
```

Run-only text surfaces parse directly to `RunOverride* + RunnableInputRaw` without
the chat-only quick-command branch. Resolution expands includes and declared
types to produce the final `RunnableInput`. Programmatic callers may construct
`RunnableInput` directly.

All input types are frozen, slotted dataclasses. `RunnableInputRaw` retains
primary source text and immutable `(name, source)` pairs. `RunnableInput`
retains one primary `Percept` and canonical immutable named values, including
the declared type needed for durable round trips. Empty primary and named
inputs remain valid.

`RunSpec` and the bound executor value each expose `input: RunnableInput`.
Child runs derive a new `RunnableInput` directly from parent locals; they do
not round-trip through raw text or `RunnableInputRaw`.

## Overrides And Resources

`RunOverride` is the preparation-only representation of current `allow`,
`default`, and `limit` commands. Existing config keys, environment variables,
CLI flags, and colon-command spellings remain unchanged.

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

Preparation controls (`start`, `rerun`, `retry`, and child `start`) store these
top-level typed fields:

```text
bindings: RunBindings
limits: RunLimits
input: RunnableInput
resources: AgentResources
```

Each is the effective snapshot for that accepted preparation. Retry records a
self-contained snapshot even when its input is unchanged. Steer uses a message
and stop uses a reason rather than overloading `RunnableInput`.

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

- `lang/input.py`, execution call/policy parsing, CLI chat/script, and scheduler;
- `base/types/policy.py`, resource selector helpers, setup configuration, and
  executor resource preparation;
- `execution/executor`, `records.py`, `events.py`, `store.py`, and `schemas.py`;
- execution, input, CLI, API, store, retry/rerun, and child-run tests;
- executor and run-record documentation.

## Acceptance Tests

- `ChatInput` alternatives and `RunnableInputRaw` validation retain current parse
  behavior, including override-only and empty runnable input.
- Resolved primary/named values round-trip through `RunnableInput`, controls,
  events, and SQLite without losing names or declared types.
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
