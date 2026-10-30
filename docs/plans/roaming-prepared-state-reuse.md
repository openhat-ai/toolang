# Shared roaming agent data and runtime roots

Directory rules and terminology confirmed on 2026-10-30. Definition only;
implementation is separate. Goal: reuse unchanged preparation across instances.

## Terms and resolution

| Term | Meaning |
| --- | --- |
| Placement | `resident`, `visiting`, or `roaming`; determines sources and paths. |
| Toolang root (`ROOT`) | Selected configuration/environment-policy, resident-agent, Hub, and discovery root. |
| Agent root (`AGENT_ROOT`) | Shared root-scope inputs, State, agent definitions, and managed workspaces. |
| Agent home (`AGENT_HOME`) | `AGENT_ROOT/agents/NAME`: one agent's source/configuration and home/agent State. |
| Runtime root (`RUNTIME_ROOT`) | Parent of runtime-side `agents/`; contains runtime and other instance-owned data. |

| Placement | AGENT_ROOT | RUNTIME_ROOT |
| --- | --- | --- |
| Resident | `ROOT` | `ROOT` |
| Visiting | `/tmp/toolang/<R8>/<P10>` | `/tmp/toolang/<R8>/<P10>` |
| Roaming | `DIR/.toolang` | `DIR/.toolang/<R8>/<P19>` |

`DIR` is the canonical Program's parent. Preserve agent-name normalization,
source-ownership checks, and resident/visiting paths. P19 selects a retained
instance; each startup gets a new `launch_id`. Neither alone invalidates State.

```text
root State:   AGENT_ROOT/.state/root/v24/
home State:   AGENT_HOME/.state/home/v24/
agent State:  AGENT_HOME/.state/agent/v24/
lab:          AGENT_ROOT/workspaces/NAME/lab/
runtime data: RUNTIME_ROOT/agents/NAME/.runtime/
```

## Implementation contract

Resolve paths in placement/layout orchestration for existing watchers/executor.
Separate overloaded `place/home` uses; reuse prepare/load/watch without a new cache.
Roaming links its Program under agent home; shared project config/catalog
projections belong to agent root, with script-specific ownership in agent home.
Watch shared sources; derive lab creation and scope locks from the resolved paths.

Retain revisions and `current` in each State directory; add no `.runtime` pointer.
Roaming shared agent `current` selects authored State; invocation grants/overrides
compose into exact Run revisions without replacing another invocation's bindings.
The same script shares State/lab across ROOTs; external workspaces stay in place.

Setup inputs/catalogs are shared; resources, environments, mounts, and overrides
remain process-local under the [Setup contract](../design/setup.md). Keep runtime
records/control per instance. Guests mount shared root inputs, selected agent
home, lab, and instance `.runtime` separately, preserving host identities/grants.

Preparation and exact history loads use shared State only. Legacy State and labs
are not read, migrated, merged, or pruned. Shared labs allow concurrent writes.

## Acceptance and scope

Verify unchanged fresh instances avoid repeated Program parsing/materialization;
changed inputs/refresh, concurrent publication, binding/record isolation, history,
watching, host/guest execution, and resident/visiting behavior remain correct.
Touch layout, State/Setup, source/identity handling, CLI, lab creation, mounts,
and design docs; run [default checks](../../CONTRIBUTING.md). Exclude CLI/ID changes,
persistent Setup caches, automatic migration, and pruning. No open scope questions.
