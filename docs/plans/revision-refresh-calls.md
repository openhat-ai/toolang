# Latest-State Run Binding

Flow syntax and value rules in this historical plan are superseded by
[Flow Array Semantics](flow-array-semantics.md).

Follow-up definition: [live State resolution](runtime-live-resolution.md)
replaces binding, resource-freezing, and lineage rules below. Unrelated
publication, module visibility, and contract rules remain unchanged.

## Status and Goal

Approved design, including removal of explicit reload without compatibility.
New named runnable invocations use published updates while accepted Runs keep
their plans. No new syntax or AST refresh flag.

This replaces the tree-wide State switching described in
[reload controls](root-run-agent-state-application.md) and
[Agic runtime calls](agic-runtime-calls.md); unrelated controls remain unchanged.

## Rules

1. When accepting a named Run, capture the watcher's latest published valid
   State. Resolve the intended target, check signature, inputs, and permissions,
   then atomically save the binding before execution. Do not resolve it again.
2. Accepted Runs, including queued Runs, retain their revision, code, prompts,
   types, and here/home/root caps. Later named children independently select
   latest State. Setup and inherited authority remain unchanged.
3. Inline/anonymous agics follow their containing plan. Never look up an old
   `<adhoc:LINE>` in new source. A newer named owner adopts its new inline bodies.
   A flow module's unnamed export is different: its public filename-based name
   is stable, so calls to that export select latest State like other named calls.
4. Missing targets or changed signatures reject the new invocation. No fallback
   to old versions and no changes to accepted/completed work.

The watcher owns preparation and publication. Calls read published snapshots;
a recent write may not yet be published. Missing State access is an error.

```text
main @ R1
  research accepted before publication  -> R1
  watcher publishes R2
  research accepted afterward           -> R2
  main's inline agic                    -> R1
```

Instances do not reserve runnable names globally. Same-name Runs may coexist
under the same or different revisions; existing ancestor-cycle checks and
resource limits still apply. Revisions need not be monotonic: old Runs can
finish later, and restored source may republish an earlier content revision.

## Module and Signature Boundaries

- Main module: its own agics/flows and other flow modules' exported flows.
  Resolve locally first; keep existing public-name uniqueness validation.
- Flow module: its own agics/flows only, including private helpers.
- Establish module and target identity from the caller's contract first.
  Latest-State lookup cannot switch modules or follow new shadowing names.

Authored calls take their expected signature from the caller's bound State.
Model-selected calls take it from the catalog advertised to that model call.
An authored target must therefore exist in its caller's snapshot. Fresh root
entry requests have no caller contract; select and validate their entry normally.
Require equal kind, effective primary input, parameter names/types/optionality,
output, and reachable struct definitions. Normalize defaults; ignore docs,
source lines, struct field order, bodies, and cap content.

A deletion can be valid in new State. Accepted old Runs survive it; later
calls from old plans fail if their target is absent. Invalid candidate source
is not published.

## One Runtime Boundary

All same-agent child calls use the shared Run-acceptance path. No special
refresh logic belongs to run/map/settle, filters, scorers, or loops. Collection
workers select after acquiring capacity; accepted/queued Runs are already
bound. One operation may contain several revisions. Existing result ordering
and caller-side output contracts remain fixed.

Before each model call, read the latest published public catalog within module
visibility and bound routing authority. Freeze that advertisement for the call.
When accepting its child Run, select latest State again and compare with the
advertised identity/signature. Compatible updates work; deletion or signature
changes fail. New targets need no definition in the original Agic State.

Remove the model reload tool, executor entry points, dedicated runtime tasks,
and reload control persistence. Publication and discovery are automatic; no
compatibility shim or historical reload codec remains. Bump the execution
database schema version so unsupported old databases fail at open.
Existing same-Run execute transfers remain explicit binding changes, recorded
by execute controls. Resolve them from the model's advertised catalog snapshot
and preserve their output/lineage checks; they are not new child Run acceptance.

## Persistence and Implementation

- Keep existing AST/source syntax. Shared helpers resolve/check targets and
  pass concrete State into child preparation.
- Every new Run stores its revision in its existing entry
  `RunControlPayload.state`; `RunRecord.state` initially references that entry.
  Step State remains tied to its executing binding. Persist child and entry
  atomically before execution; no new capture control or State table.
  Read latest State once per acceptance attempt; do not chase publications
  arriving during validation. No child work starts before the commit succeeds.
- Remove the store's child-owned-State prohibition and root-target-equality
  assumption; preserve parent/thread/reference validation.
- Record advertised catalog revision in model-step data. Caller Step/State
  supplies the authored baseline; child entry supplies selected State, target,
  input, and resources. Existing errors report target, revisions, and reason.
  Freeze the complete advertised route identities/signatures for the model
  batch. Newly published but unadvertised targets wait for the next model call.
- Historical reads never substitute current for missing snapshots; keep
  referenced State available within the supported database schema.
- Preserve explicit retry's succeeded-prefix/invalid-suffix behavior. New
  invocations in the reexecuted suffix select latest State. Keep existing
  execute exclusion; add no replay, per-item recovery, or mixed-version
  retry prohibition.

One Toolang PR: `feat(execution): bind new named runs to latest state`.
No tree-sitter changes are required. Likely touchpoints:
`src/toolang/execution/executor/`, runnable resolution, execution records/store/
schemas, `lang/validate.py`, `lang/contracts.py`, State composition, and tests.
Cross-module validation belongs to State composition; language schemas must
not depend on watchers, stores, or execution services.
Defer only unresolved main-module calls that may name exported flows, including
their contract checks. Resolve these during composition without importing the
callee's private type namespace. Unresolved standalone or flow-module calls
still fail validation.

## Acceptance and Risks

- Named calls adopt updates in all statement placements and allowed modules.
- Publish between collection acceptances; accepted/queued children remain
  fixed and later children update. Use deterministic gates, not timing sleeps.
- Cover module isolation, name shadowing, changed/nested signatures, deletion,
  old caps, relocated inline declarations, and stable unnamed flow exports.
- Models discover new/updated flows without reload. Publication between model
  advertisement and acceptance cannot bypass signature or permission checks.
- Cover atomic acceptance, mixed-version history, catalog provenance,
  explicit retry/execute, preserved ancestor-cycle checks, and missing-snapshot
  errors. Failed acceptance must leave no partially prepared child.
- Verify reload is absent from tool discovery and unsupported database schemas
  are rejected before execution. Keep publication coverage independent of tools.
- Run required Ruff, format, ty, and offline pytest checks.

This changes default named-call behavior. Valid deletion or incompatible
updates can break future calls from an old plan. Signature equality does not
prove behavioral equivalence. Audit global-State reads and parent rebinding.
No grammar changes, independent agic modules, cross-agent seek changes, loop
controls, automatic recovery, or publication barriers are included.
