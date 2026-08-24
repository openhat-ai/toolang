# Agent Collab, Lab, and Workspace Directories

Implementation starts only after this definition is approved.


## Goal and Success Criteria

Give every agent two durable run spaces:

- `collab/` is for collaboration with humans or other agents;
- `lab/` is for exploration, including autonomous experiments; and
- each space has a `MEMO.md` for agent-maintained notes loaded into its runs.

`collab` is the formal filesystem and enum spelling of collaboration. This
feature introduces no `work/` directory or `work` compatibility alias.

The feature succeeds when:

- every materialized agent home contains `collab/MEMO.md` and `lab/MEMO.md`
  without overwriting existing content;
- every root run explicitly selects `collab` or `lab`, and the choice survives
  child runs, retry, rerun, persistence, and inspection;
- model instructions identify the selected working directory and writable
  directories;
- the selected notes are always supplied as context data; and
- human-granted external workspaces are readable and writable in collab runs,
  but never grant access to lab runs.


## Current Behavior

`AgentLayout` has no collab/lab paths, `RunSpec` carries no run-space purpose,
and prompts load no agent-owned notes. Built-in filesystem and shell tools use
the complete agent home as cwd/root, while Docker has no external workspace
mount contract.


## Scope

In scope:

- run-space layout and compatible materialization;
- explicit run-space selection and durable access snapshots;
- bounded `MEMO.md` loading;
- resident-agent workspace grants and CLI management;
- filesystem/shell working roots and Docker workspace mounts;
- migrations, documentation, and offline tests.

Out of scope:

- the future plugin-maintained `memory/` directory and memory plugin;
- memory search, summarization, retention, or promotion between spaces;
- deciding which future autonomous jobs are exploration;
- natural-language or model-callable workspace authorization;
- hard per-run containment for arbitrary shell commands; and
- workspace synchronization, version control, backup, or deletion.


## Layout and Materialization

```text
agents/<agent>/
  agent.too
  config.toml
  collab/
    MEMO.md
  lab/
    MEMO.md
  .runtime/
  ...
```

`AgentLayout` exposes `collab`, `collab_memo`, `lab`, and `lab_memo`.
These directories are durable agent data and are excluded from prepared-state
source scanning.

One idempotent catalog helper creates missing directories and initializes each
missing memo file with one newline. It preserves existing bytes and rejects a
conflicting file at `collab/` or `lab/`, or a directory at either memo path.

New resident, visiting, and roaming agents call the helper. Startup and
one-shot execution call it before state preparation so existing homes upgrade
lazily. Local clones preserve their current copied collab and lab content; remote
clones start with empty memo files.


## Run Space and Notes

`RunSpace` is `collab | lab`. `RunSpec` has no core default; public callers
resolve one explicitly:

| Caller | Space |
| --- | --- |
| Chat, task, chore, and file request | `collab` |
| Direct script | defaults to `--space collab`; accepts `lab` |
| `POST /runs/stream` | defaults to `collab`; accepts `lab` |
| Future autonomous exploration | must pass `lab` |

Before accepting a root run, the executor captures one immutable `RunAccess`
containing the space, working directory, memo path and text, truncation flag,
and active workspace names and paths. Preparation controls persist it; child
runs inherit it.

Lifecycle rules:

- start captures current notes and workspace grants;
- retry reuses the exact access snapshot;
- rerun keeps the space but captures current notes and grants;
- edits or revocations do not change an active run; and
- historical controls without access data migrate as collab runs.

Toolang reads at most 32,000 Unicode characters from the selected `MEMO.md`.
Invalid UTF-8 rejects preparation; truncated content receives a generated
marker. Changes made inside a run become visible through notes only to the
next root run.

Mandatory runtime instructions always identify `space`, `working_directory`,
`memo_file`, and ordered `writable_directories`. For collab runs, each active
workspace is listed by name and environment-visible path. The memo body is an
escaped, bounded context block and cannot change access. `instruct: none`
removes optional agent guidance, not runtime access instructions;
`context: none` removes optional authored context, not notes.

Collab runs load only the collaboration notes in `collab/MEMO.md`. Lab runs
load only the exploration notes in `lab/MEMO.md` and receive no external
workspaces. These agent-maintained notes are distinct from future memory, which
the memory plugin owns under `memory/`.


## Workspace Grants

The human-owned agent `config.toml` is authoritative:

```toml
[workspaces]
toolang = "/Users/alice/src/toolang"
website = "/Users/alice/src/website"
```

Only the agent-home table is used. Names are stable 1-64 character ASCII
identifiers; paths must be absolute existing directories. Duplicate or nested
canonical paths are rejected.

Resident-agent management uses:

```text
too <agent> workspace add PATH [--name NAME]
too <agent> workspace list
too <agent> workspace remove NAME
```

Commands preserve unrelated TOML. No workspace metadata is written to
`collab/MEMO.md`: `config.toml` is the only authority, and the mandatory
runtime prompt is the only run-facing projection. It lists the exact active
workspace names and paths captured in `RunAccess`; configured but inactive
Docker workspaces are omitted until restart. Memo text never widens
filesystem access.

Natural-language chat content does not authorize a host path. A future
model-callable request must have a separate human-approval design.


## Tools and Hosting

`ToolContext` retains the agent home for agent-state tools and gains the
run-specific working directory and allowed roots.

The filesystem tool resolves relative paths from the selected space and allows
absolute paths only within that space or an active workspace. It rejects the
other space, other agent-home paths, traversal, symlink escapes, and removal of
an allowed root itself.

The shell tool defaults cwd to the selected space and accepts an explicit cwd
only within an allowed root. This is path selection, not a security sandbox:
arbitrary shell text under `sandbox=none` can still name other host paths.

Hosting publishes the active workspace mapping:

- `none` uses canonical host paths;
- Docker mounts each workspace read-write at
  `<hosted-agent-home>/.runtime/workspaces/<name>` and publishes hosted paths;
- runtime access intersects configured and published names; and
- adding or removing a workspace while Docker is running reports
  `restart required` without restarting automatically.

With `none`, config watching makes changes available to new root runs. Docker
removal blocks the grant in new path-aware tool contexts after state refresh,
but restart is still required to remove the mount.


## Implementation Touchpoints

- `common/layout.py`, `catalog/agent.py`, `up/process.py`: paths and
  materialization.
- New `catalog/workspace.py` and CLI workspace commands: grants, config edits,
  validation, and restart reporting.
- Execution types, executor preparation, records, store, and schemas:
  `RunSpace`, `RunAccess`, inheritance, migration, and inspection.
- API, chat, script, scheduler, and inbox call sites: concrete space selection.
- Executor prompt assembly: mandatory access instructions and notes context.
- `base/types/tool.py`, tool-step invocation, filesystem, and shell: allowed
  roots and selected cwd.
- Setup and hosting types, orchestration, mounts, and sandbox plugins: active
  workspace mappings and Docker mounts.
- `docs/layout.md`, `program.md`, `execution.md`, `tools.md`, and `api.md`:
  public behavior and security boundary.


## Acceptance Tests

- Resident, visiting, roaming, existing, and cloned homes follow the defined
  materialization and preservation rules.
- All production `RunSpec` callers pass a valid space; script/API lab selection
  works; no `work` value is accepted.
- Start, child, retry, rerun, control codecs, SQLite, inspection, and legacy
  records follow the access lifecycle.
- Default/custom/none instruct and context combinations always contain the
  correct access instructions and selected bounded notes.
- Workspace add/list/remove preserves unrelated config and validates paths and
  names; collab prompts list the exact active mapping and lab prompts list none.
- Filesystem and shell cwd cover collab/lab separation, workspaces, traversal,
  symlinks, root removal, revocation, and local/Docker path translation.
- Hosting covers deterministic mounts, published mappings, missing mounts,
  restart reporting, and no implicit restart.
- Memo edits neither rebuild prepared state nor change workspace grants;
  workspace config changes do.
- The default repository verification passes without live-provider tests.


## Risks

- Mandatory runtime prompt separation changes the practical meaning of
  `instruct: none` and `context: none`; focused tests must protect other prompt
  behavior.
- Memo and workspace paths become durable local run/prompt data; users must not
  place secrets in notes or grant sensitive directories casually.
- Docker mounts are fixed at launch, so restart reporting must distinguish
  configured from active workspaces.
- Path-aware tools enforce roots, but arbitrary shell commands are not hard
  confined by this feature.


## Open Questions

None.
