# Define Agent Spaces and Runtime Context

## Status

Approved for implementation in the phases below.

## Goal

Give agents durable collaboration and exploration directories, human-granted
workspace access, and deterministic model context. Load workspace instructions
only when a tool identifies the relevant path, without active workspaces,
mutable run working directories, or persisted access snapshots.

## Success Criteria

- Every agent home has preserved `coop/MEMO.md` and `lab/MEMO.md` files.
- Every root run records one `coop | lab` runspace; descendants inherit it.
- Humans manage named workspaces in agent `config.toml`, and the current State
  publication exposes the effective mapping.
- Tools access only the selected runspace and configured workspaces.
- A path-aware tool executes only when its workspace instructions were visible
  to the ModelCall that requested it.
- Root, child, nested, and parallel runs receive deterministic `far`, `near`,
  and `line` context.
- Compaction may forget workspace instructions; a later path access reloads
  them safely.

## Scope

Included:

- runspace layout, selection, inheritance, memo context, and persistence;
- workspace configuration, CLI, State publication, and hosting;
- explicit tool paths, access checks, and dynamic `AGENTS.md` loading;
- `far`, `near`, `line`, compaction invariants, and exact ModelCalls;
- retry, rerun, replay, inspection, legacy decoding, and offline tests.

Excluded:

- plugin-owned `memory/` and memory behavior;
- active workspaces or model-facing workspace tools;
- mutable run cwd or parsing paths from arbitrary shell text;
- compaction thresholds, summarization models, and summary quality;
- initial Chat/TUI `@file` implementation; and
- task, chore, or prompt `@file` expansion.

## Vocabulary

- **Runspace**: the agent-owned `coop` or `lab` directory selected for a run.
- **Workspace**: a human-configured external directory the agent may access.
- **Session cwd**: a future Chat/TUI setting used only for `@file` lookup.
- **far**: compacted Thread prefix as `Part[]`.
- **near**: uncompacted Thread suffix as `Message[]`.
- **line**: root-to-current Run path as `Part[]`.
- **Visible instructions**: complete instruction files in the exact ModelCall
  that produced a tool call.
- **Tool preparation**: path resolution, access checking, and instruction
  loading before plugin invocation.

Do not introduce generic `Space`, `RunAccess`, `RunWorkspace`, active/current
workspace, focus, mutable run cwd, or loaded-instruction state.

## Runspaces and Notes

```text
agents/<agent>/
  agent.too
  config.toml
  coop/MEMO.md
  lab/MEMO.md
  .runtime/
```

`coop` is the default for collaboration; `lab` is for exploration.
Materialization creates missing directories and one-newline memo files,
preserves existing bytes, and rejects conflicting file shapes. New, cloned,
visiting, roaming, and upgraded agents use the same idempotent path. Clone
copies `config.toml` unchanged and performs no workspace-specific work.

Public call sites resolve a concrete runspace into `RunRequest`; core execution
has no default. Descendants, retry, and rerun preserve it, and a run never
switches runspace. Legacy preparation records decode as `coop`.

The selected runspace is readable and writable. Its bounded UTF-8 `MEMO.md` is
loaded as context data, independent of authored `instruct` and `context`.
`coop/MEMO.md` contains collaboration notes; `lab/MEMO.md` contains exploration
notes. A future memory plugin exclusively owns `memory/`.

## Workspaces

Only the agent-home config grants workspaces:

```toml
[workspaces]
toolang = "/Users/alice/src/toolang"
website = "/Users/alice/src/website"
```

Names are stable 1-64 character ASCII identifiers; values are absolute paths.
`add` canonicalizes an existing directory and rejects duplicate names,
duplicate canonical paths, and nested roots. Loading copied config does not
require paths to exist; availability is checked when listing or using them.

```text
too <agent> workspace add PATH [--name NAME]
too <agent> workspace list
too <agent> workspace remove NAME
```

`add` defaults to a valid directory basename. `list` reports name, path, and
availability. `remove` changes only config and never deletes the directory.
Commands preserve unrelated TOML and never duplicate metadata in `MEMO.md`.

Workspaces are a State-owned runtime resource, not durable Program content.
`StatePublication.workspaces` is an immutable name-sorted `Mapping[str, str>`;
no `Workspace` domain object is needed. Workspace-only config changes may
refresh this publication without changing Program or cap content.

A root tree keeps its captured publication. Retry uses the latest compatible
publication for the durable State revision, so removed workspaces cannot be
recovered from an old absolute-path snapshot. Historical ModelCalls and tool
calls remain exact evidence; no `RunAccess` is persisted.

Local hosting publishes canonical host paths. Containers mount all configured,
available workspaces at deterministic guest paths. A mount-changing update
reports `restart required` and never widens a running container silently.

## Session File Lookup

Future Chat/TUI support may initialize `session.cwd` from the launch directory
and modify it with local `/cd PATH` and `/pwd` commands. It affects only
human-authored `@file` lookup and completion: it is not run state, a tool
default, an access grant, or an instruction selector.

Task, chore, and prompt input does not support `@file`. Future authored
resources must remain below their owning package, such as
`skills/NAME/assets/file`; absolute paths, parent traversal, and symlink escape
are invalid.

## Thread and Run Context

```text
thread context = far + near
```

The two values have no overlap or gap. Compaction may replace a Thread prefix
between root runs but never mutates an active root snapshot. Every descendant
inherits that snapshot, so parallel behavior is timing-independent. Only
public root interaction contributes to future Thread context; child messages,
tool exchanges, repairs, and flow locals remain internal.

`line` contains the ordered root-to-current Run identities and resolved inputs.
It excludes outputs, tools, siblings, and descendants. Children extend it,
parallel branches diverge, and same-Run handoff does not extend it.

```text
agic messages = recalled messages + rendered messages + appended messages
```

Root `recall = auto` recalls `far, near`; child `auto` recalls nothing. Explicit
forms are `none`, `far`, `near`, and `far, near`. `line` is an explicit
read-only local, never a recall source. A flow owns no model messages.

Each model step snapshots the agic messages into an exact normalized
`ModelCall`. Instructions, messages, tools, output schema, and continuation
remain independent. Provider continuation optimizations cannot change the
logical request.

## Tool Paths and Access

Filesystem tools use explicit absolute paths. Shell execution requires an
explicit absolute `cwd`; command text remains opaque. Neither uses
`session.cwd` or mutable run state.

```text
allowed roots = selected runspace + captured StatePublication.workspaces
```

Preparation canonicalizes every declared path, rejects traversal and symlink
escape, identifies one unique root, and authorizes before reading instructions.
Sandboxing remains the security boundary for arbitrary process access.

The plugin system implements preparation once. A relevant tool only declares
internal metadata such as `local_paths=("path",)` or
`local_paths=("source", "destination")`; it does not inspect transcript or
compaction state. This metadata is absent from the model-facing schema. The
executor never guesses path arguments or parses shell commands.

## Workspace Instructions

For each declared workspace path, preparation finds the `AGENTS.md` chain from
workspace root to target directory. Files use their parent, directories use
themselves, and new paths use the nearest existing parent. Runspaces use their
memo and do not load `AGENTS.md`.

The executor derives visible instructions from structured tool results in the
exact source ModelCall. Identity is `(canonical path, complete-content digest)`.
User text, summaries, path-only manifests, and incomplete content do not count.

When all current digests are visible, preparation invokes the plugin with
normalized paths. Otherwise it skips invocation and returns a successful
ordinary tool result:

```json
{
  "executed": false,
  "retry": true,
  "instructions": [
    {
      "path": "/repos/toolang/AGENTS.md",
      "digest": "sha256:...",
      "content": "..."
    }
  ]
}
```

The model may retry or change its action; the executor never retries
automatically. The data stays in `ToolResultPart.output`. No `_too` tool or
field is added; `_too__*` remains reserved for inner runtime tool names.

Read, list, search, glob, write, edit, move, copy, remove, and shell cwd use the
same preparation. Multi-path tools load the ordered union of missing chains.
Bounded invalid UTF-8 or oversized instructions fail instead of becoming an
authoritative summary.

## Compaction

```text
exact ModelCall
-> complete assistant response
-> all correlated tool calls and results
-> optional compaction before the next ModelCall
```

Sibling tools use one visible-instruction snapshot. Compaction preserves whole
assistant/tool groups and keeps the newest complete exchange uncompacted until
a later successful ModelCall has seen it. This general rule protects newly
loaded instructions without a pending-instruction record.

Afterward, compaction may remove the exchange. A `far` summary does not make an
instruction visible, so a later path access loads it again. No global
`loaded_agents` state exists. Dynamic tool exchanges never enter a future root
Thread snapshot.

## Persistence and Restart

`RunRequest`, `RunSpec`, bound runs, and root preparation controls carry the
runspace. Children inherit it. Retry and rerun preserve it. Exact historical
inspection reconstructs each ModelCall independently of current workspaces.
Exact replay uses stored facts; new retry performs current path checks instead
of reusing old absolute paths.

## Implementation Phases

1. Publish configured workspaces through State and add workspace CLI commands.
2. Carry runspace through requests, execution, controls, and inspection.
3. Materialize runspaces and load the selected memo.
4. Implement `far`, `near`, and `line`.
5. Centralize declared local-path preparation and access checks.
6. Load workspace instructions and enforce compaction invariants.

Each phase is an independently verifiable implementation pull request.

## Touchpoints

- Layout, catalog, clone, hosting, and mounts: runspaces and workspaces.
- State config, publication, watcher, and CLI: workspace management.
- Execution schemas, calls, records, store, API, remote client, Chat, and
  Script: runspace transport and persistence.
- Executor preparation and model steps: memo, `far`, `near`, `line`, and
  visible instructions.
- Tool helpers, tool steps, filesystem, shell, and sandbox: path declaration,
  preparation, and enforcement.

## Acceptance Tests

1. Materialization preserves both runspaces and memo contents for every agent
   placement and clone/upgrade path.
2. Workspace commands preserve unrelated TOML, validate identities and paths,
   report unavailable copies, publish deterministic State resources, and never
   delete workspace data.
3. Active roots keep their workspace publication; compatible retry observes
   the latest mapping.
4. Every root caller supplies runspace; wire, controls, persistence,
   inspection, descendants, retry, rerun, and legacy decoding preserve it.
5. Only the selected memo loads, and notes never widen access.
6. `far` and `near` have no overlap or gap; snapshots and branch-local `line`
   are deterministic.
7. Preparation covers all declared paths, roots, traversal, symlinks,
   unavailable workspaces, and shell cwd before plugin invocation.
8. Nested, changed, and compacted instructions load correctly; summaries and
   user text never satisfy visibility.
9. Tool groups remain provider-valid across compaction, siblings share one
   snapshot, and exact ModelCalls remain inspectable.
10. Local/container publication and the full offline verification suite pass.

## Risks

- Workspace paths and memo content may expose sensitive local data in prompts,
  records, and inspection.
- Shell text cannot be path-prepared; sandboxing remains the security boundary.
- State publication and container mounts have different refresh lifecycles.
- Large instruction files require strict limits rather than lossy summaries.

## Open Questions

None.
