# Publish AgentState directly

## Goal

Publish one immutable `AgentState` from `StateWatcher`. Remove `StatePublication`
and `StateResources`, including their forwarding properties and consumer type
branches. This implements the approved State identity and publication change.

## Contract

- Hash the complete bytes of each dependency, including `config.toml`. Changes
  to unrelated settings or comments may change revision without changing terms.
- Parse and persist only State-owned configuration: caps, cap allows, and home
  workspaces. Do not copy unrelated provider credentials into State artifacts.
- Derive workspaces from the captured configuration, never a second live read.
- Precompute module-effective caps on `AgentState`. Startup cap overrides replace
  configured allows before filtering; session/runnable policy only narrows them.
- Include frozen startup overrides in the composition identity and record them
  alongside its layer references. Loading that revision restores the same State.
- Retry keeps that recorded State, but requires its workspace names and paths
  to remain authorized by the current State. Reject removed/remapped grants
  before changing records; added workspaces do not expand the retried Run.
- Watcher publishes only validated States, reuses unchanged objects, and retains
  the last valid State on failure. Tool Steps keep their captured State.
- Old rebuildable caches are regenerated; no execution-record schema change,
  State-based ModelCall replay, workspace context catalog, or adoption-policy change.

## Touchpoints and acceptance

- `state/{source,config,cache,prepare,state,watcher}.py`: full dependency digests,
  captured workspace config, composition identity, and direct State publication.
- Execution, API, CLI, and scheduling consumers: use only `AgentState`, with
  identical cap authorization and workspace behavior across entry points.
- Tests: workspace add/remove/remap changes revision; unrelated config edits
  change revision but not terms; historical load never rereads live config;
  startup override expansion and narrowing survive reload; caps are queried
  once per State; invalid candidates preserve the old State; fs/honor/pick and
  recorded-call replay remain correct; retry rejects revoked workspace grants
  without changing records. Run the full offline verification suite.

## Risks

More config edits rebuild State. Raw materialized caps must remain available for
startup override replacement, while consumers use the precomputed effective set.
No open design questions remain for this scope.
