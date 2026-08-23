# Agent Work, Lab, and Workspace Directories

Implementation starts only after this definition is approved.


## Goal

Give every agent two durable, agent-owned working spaces and make the selected
space explicit for every root run:

- `work/` serves work performed for a human;
- `lab/` serves autonomous experiments and self-directed activity unrelated to
  human work;
- each space has a `MEMORY.md` working-memory file loaded into runs for that
  space; and
- human-granted external workspace directories are available to work runs.

This is the filesystem and prompt foundation for a later memory plugin. The
later plugin may maintain long-term data under `memory/`, but this feature does
not create, load, search, or define that directory.


## Success Criteria

- Every materialized agent home contains `work/MEMORY.md` and
  `lab/MEMORY.md`; existing content is never overwritten.
- Every root run has an explicit `work` or `lab` space. Child runs inherit it,
  retry preserves it, and rerun preserves the space while adopting current
  memory and workspace grants.
- Current chat, task, chore, file, and script surfaces select `work` explicitly.
  The direct script and run API surfaces can explicitly select `lab` for
  autonomous callers.
- Model runtime instructions identify the selected working directory and its
  writable directories even when an agic selects a custom instruct or
  `instruct: none`.
- The selected `MEMORY.md` is captured once at root-run acceptance and supplied
  to every model call in that run tree as context data, including with a custom
  context or `context: none`.
- A human can grant, inspect, and revoke named external workspaces without
  editing TOML manually. Configured grants are projected into a managed section
  of `work/MEMORY.md`.
- Built-in path-aware tools resolve relative paths from the selected space,
  allow the selected space and active workspaces, and reject the other agent
  space, the rest of the agent home, traversal, and symlink escapes.
- Docker hosting mounts configured workspaces at stable hosted paths. Changes
  made while a Docker runtime is active report that a restart is required.
- Existing agent homes, visiting and roaming materialization, old run records,
  and configurations without workspaces remain usable.


## Current Behavior

`AgentLayout` currently exposes the agent home and runtime/setup/state paths,
but no work or lab paths. New resident agents contain only `agent.too`; visiting
and roaming materializers also create only the minimum home and program files.

The default instruction template reports `agent_home` and the process working
directory. Selecting a custom instruct replaces that template, and no separate
mandatory runtime-access block exists. Prompt context contains basic date,
agent, and model facts but loads no agent-owned memory file.

`ToolContext` contains `home` and `wd`. Tool calls always receive the agent home
for both. `filesystem` rejects resolved paths outside the agent home, and
`shell` rejects a requested cwd outside the home. Docker mounts the agent home
but has no external workspace mount contract.

`RunSpec` carries no work/lab purpose. Current call sites can be identified by
their surface or thread origin, but those values describe invocation rather
than whether the run is human work or autonomous lab activity. The core must
therefore not infer a space from runnable names or thread origins.


## Scope

This feature defines:

- the agent-home `work/` and `lab/` layout and compatible materialization;
- explicit run-space vocabulary, caller selection, inheritance, persistence,
  retry, rerun, and inspection behavior;
- bounded working-memory capture and prompt placement;
- a human-owned external-workspace registry, CLI, and `work/MEMORY.md`
  projection;
- run-specific path roots for built-in path-aware tools;
- local and Docker workspace path resolution and mount behavior;
- documentation, migrations, and offline deterministic tests.

It does not define:

- the future `memory/` directory or memory plugin;
- semantic, episodic, vector, search, summarization, or retention behavior;
- automatic promotion between `lab/MEMORY.md` and `work/MEMORY.md`;
- an autonomous-evolution scheduler or a policy that decides which future jobs
  are lab work;
- natural-language workspace authorization, a model-callable grant operation,
  or an unauthenticated HTTP endpoint that can expose host paths;
- per-run OS isolation for arbitrary shell commands;
- workspace synchronization, source control, backup, or deletion of workspace
  contents.


## Filesystem Layout

Every materialized agent home has this additional authored layout:

```text
agents/<agent>/
  agent.too
  config.toml
  work/
    MEMORY.md
  lab/
    MEMORY.md
  .runtime/
  ...
```

`AgentLayout` exposes `work`, `work_memory`, `lab`, and `lab_memory` properties.
These paths are durable agent data, not prepared state and not runtime cache.
They are excluded from state source scanning, so editing memory does not rebuild
`AgentState`.

Materialization uses one idempotent helper owned by the agent catalog:

1. create `work/` and `lab/` when missing;
2. create each missing `MEMORY.md` as a minimal UTF-8 file containing one
   newline;
3. leave every existing file, directory, permission, and byte unchanged; and
4. reject a conflicting non-directory `work` or `lab`, or a directory named
   `MEMORY.md`, with an actionable path error.

New resident-agent creation and visiting/roaming program materialization call
the helper. Resident startup and one-shot execution also call it before state
preparation, which upgrades existing homes lazily without a separate migration
command. A local clone retains the existing copy-tree behavior, then repairs
only missing space files; it therefore preserves cloned work/lab content and
workspace configuration. Remote clones start with empty memory files.


## Run Space and Access Snapshot

`RunSpace` is the closed vocabulary `work | lab`. Core execution adds no
default: every `RunSpec` caller must pass a concrete space. Defaults belong at
public call sites:

| Caller | Selected space |
| --- | --- |
| Chat, task, chore, and file request | `work` |
| Direct script | `--space work` by default; accepts `--space lab` |
| `POST /runs/stream` | request field defaults to `work`; accepts `lab` |
| Future autonomous evolution | must pass `lab` explicitly |

The executor resolves one immutable `RunAccess` value before accepting a root
run. It contains:

- the `RunSpace`;
- the run-specific working directory;
- the selected memory path, captured text, and truncation flag; and
- the ordered active workspace names and environment-visible paths.

`RunAccess` is stored in start, retry, and rerun preparation controls. It is
also carried by the bound run and inherited unchanged by recursive children.
Caller-facing run inspection exposes the selected space but does not add the
memory body to compact run summaries.

Lifecycle behavior is explicit:

- start captures current memory and current active workspace grants;
- children inherit the root snapshot;
- edits made during an active run become visible only to the next root run;
- retry reuses the exact access snapshot of the preparation it reopens;
- rerun retains the source run's space but captures current memory and grants;
- grant removal does not revoke an already accepted run; new roots omit it; and
- historical controls without `RunAccess` decode as legacy work runs. A rerun
  captures a current work snapshot, while retry synthesizes one once and stores
  it in the new retry control.

The executor resolves workspace configuration from the exact home-config state
adopted by the run and intersects it with workspace paths active in the current
hosting environment. It does not inspect the thread origin, runnable name, or
the contents of `MEMORY.md` to widen access.


## Working Memory

`MEMORY.md` is the selected space's working memory: a concise, editable summary
loaded into prompt context. It is not named short-term memory, is not the source
of workspace authority, and is not a replacement for the future memory plugin.

At root acceptance Toolang reads at most 32,000 Unicode characters from the
selected file. It reads one additional character to detect truncation. Invalid
UTF-8 rejects preparation with the file path; an absent file is repaired by
materialization and otherwise behaves as empty. Truncated content receives a
clear runtime-generated marker outside the authored text.

Prompt assembly separates mandatory runtime protocol from authored agent
instructions:

```text
mandatory runtime protocol and run access
selected/default agent instruct
selected working memory context
selected/default authored context
authored messages and current input
```

The mandatory runtime block always reports:

- `space`;
- the selected `working_directory`;
- the selected `memory_file`;
- ordered `writable_directories`, including workspace names for work runs; and
- that other agent-home paths are not working directories for this run.

`instruct: none` suppresses optional agent guidance, not runtime protocol.
Similarly, `context: none` suppresses optional authored/default context, not
working memory. Memory is placed in a bounded, escaped `<working-memory>`
context block and is explicitly data under the existing instruction-priority
rules. Text inside it cannot change the run space or grant a workspace.

Work runs load only `work/MEMORY.md`. Lab runs load only `lab/MEMORY.md` and
receive no external workspaces. Neither memory file is automatically copied,
merged, summarized, or rewritten at run completion.


## Workspace Authority and Projection

External workspaces are resident-agent grants stored in the human-owned agent
`config.toml`:

```toml
[workspaces]
toolang = "/Users/alice/src/toolang"
website = "/Users/alice/src/website"
```

Only the agent-home `workspaces` table is authoritative. Root configuration is
not inherited for this purpose. Each key is a stable name of 1-64 ASCII
letters, digits, dots, underscores, or hyphens, beginning with a letter or
digit. Each value is an absolute path to an existing directory after
canonicalization. The catalog rejects duplicate canonical paths and overlapping
parent/child grants because those produce ambiguous containment and mount
rules.

The human management surface is:

```text
too <agent> workspace add PATH [--name NAME]
too <agent> workspace list
too <agent> workspace remove NAME
```

If `--name` is absent, `add` derives it from the directory basename. Commands
operate only on resident agents, preserve unrelated TOML formatting through
`tomlkit`, use the existing config-file lock convention, and report conflicts
or missing paths without changing either file. Natural-language chat content
does not itself constitute a grant; adding a model-callable request/approval
flow is a separate security design.

The workspace catalog also maintains this delimited projection in
`work/MEMORY.md`:

```markdown
<!-- toolang:workspaces:start -->
## Workspaces

- `toolang`: `/Users/alice/src/toolang`
- `website`: `/Users/alice/src/website`
<!-- toolang:workspaces:end -->
```

It replaces only the delimited region and preserves all other bytes. An empty
registry removes the complete managed region and its immediately adjacent
separator blank line. Duplicate or unmatched delimiters are an error rather
than a reason to rewrite free-form memory.

The TOML registry remains authoritative if the editable projection is changed
or deleted. Add, remove, list, and agent startup reconcile the projection under
one agent-workspace lock. Atomic replacement protects each file; a process
crash between the two replacements is repaired on the next reconciliation.
Runtime instructions independently list the active authoritative grants, so a
stale or forged memory projection never expands access.


## Tool and Hosting Behavior

`ToolContext` retains the agent home for agent-owned state tools and gains the
immutable run working directory and allowed path roots. The tool-step executor
builds it from `RunAccess`, not directly from `AgentLayout`.

For `filesystem`:

- relative paths resolve from the selected work or lab directory;
- absolute paths may resolve inside that directory or an active workspace;
- operations reject the other space, other agent-home paths, traversal, and
  symlinks whose resolved target leaves an allowed root;
- an operation may modify contents but cannot remove or replace an allowed root
  itself; and
- model-facing descriptions refer to the current run directories rather than
  the complete agent home.

For `shell`, an omitted cwd uses the selected space and an explicit cwd must be
inside an allowed root. Toolang passes the allowed roots in environment facts
and runtime instructions. This is path selection, not a claim that arbitrary
shell text is confined: `sandbox=none` has no OS filesystem boundary, and a
shell command can name another host path. Hard per-run shell containment is out
of scope and must be supplied by a future sandbox that executes each run under
its own mount policy.

Hosting resolves configured host paths before launch:

- `none` publishes name-to-canonical-host-path mappings to the hosted process;
- Docker mounts each workspace read-write at
  `<hosted-agent-home>/.runtime/workspaces/<name>` and publishes those hosted
  paths instead of host paths;
- runtime resolution intersects configured names with this published mapping,
  so a configured but unmounted Docker workspace is not active; and
- work/lab directories continue to live under the mounted agent home.

Workspace mappings contain paths but no file contents or credentials. They are
captured as non-secret `AgentEnvironment` facts. Docker launch state records a
workspace-config fingerprint so the CLI can report `restart required` after an
add or remove while that runtime is active. Toolang does not restart a running
agent implicitly. With `none`, config watching makes a change available to new
root runs without restart. Removing a Docker grant blocks it in new path-aware
tool contexts immediately after state refresh, but restart is still required
to remove the underlying mount.


## Implementation Touchpoints

- `src/toolang/common/layout.py`, `catalog/agent.py`, and `up/process.py`: add
  layout paths and idempotent space materialization for resident, visiting, and
  roaming agents.
- `src/toolang/catalog/workspace.py` and `catalog/types.py`: own workspace
  validation, TOML mutation, locking, and managed-memory projection.
- `src/toolang/cli/toolang/commands/workspace.py` and command registration: add
  resident workspace add/list/remove commands and Docker restart reporting.
- `src/toolang/execution/types.py`, executor common/preparation/records/store,
  and schemas: define, carry, persist, migrate, inherit, and inspect
  `RunSpace`/`RunAccess`.
- Run call sites in API, chat, script, scheduler, and inbox code: resolve and
  pass a concrete space; expose API/script selection where specified.
- `src/toolang/execution/executor/prepare.py` and prompt templates: separate
  mandatory runtime protocol from optional instruct/context and render access
  plus bounded working memory.
- `src/toolang/base/types/tool.py`, tool-step invocation, and built-in
  filesystem/shell plugins: pass and enforce run-specific path roots.
- `src/toolang/setup/types.py`, setup configuration, `up/hosting.py`, hosting
  value types, mounts, and Docker/none plugins: publish active workspace path
  mappings, mounts, and configuration fingerprints.
- `docs/layout.md`, `docs/program.md`, `docs/execution.md`, `docs/tools.md`, and
  `docs/api.md`: document layout, prompt layers, run space, tool roots, CLI/API,
  hosting restart semantics, and the non-security boundary.


## Acceptance Tests

- New resident, visiting, and roaming homes get both directories and empty
  memory files; existing memory bytes survive repeated materialization; invalid
  conflicting nodes fail with their paths.
- Existing local clone behavior preserves work/lab/config data and repairs only
  missing space files. Remote clone memory starts empty.
- Every production `RunSpec` call site passes a space. Chat, task, chore, file,
  and default script/API execution use work; explicit script/API lab execution
  uses lab; invalid values fail before run acceptance.
- Start, child, retry, rerun, control codecs, SQLite round trips, run inspection,
  and legacy-control decoding follow the defined space/access lifecycle.
- Work and lab prompts contain the exact selected cwd, memory path, writable
  roots, and no roots from the other space. The mandatory block and memory
  remain present with custom/default/none instruct and context combinations.
- Memory is captured once per root, shared by children, unchanged by mid-run
  edits and retries, refreshed by rerun/new root, escaped as data, bounded at
  32,000 characters, and clearly marked when truncated.
- Workspace add/list/remove preserves unrelated TOML and free-form memory,
  produces the exact ordered managed block, derives or validates names,
  rejects conflicts/missing/non-directory/overlapping paths, repairs drift, and
  never treats memory edits as grants.
- Filesystem and shell cwd tests cover relative selected-space paths, absolute
  active-workspace paths, work/lab separation, home escape, `..`, symlink
  escape, root removal, revoked grants, and identical path behavior in none and
  translated Docker environments.
- Hosting tests cover deterministic Docker mounts and hosted paths, local path
  mappings, missing/unmounted workspace exclusion, launch fingerprints, and
  restart-required reporting without implicit process mutation.
- State preparation ignores memory edits but adopts workspace config changes;
  workspace projection edits alone neither rebuild state nor widen run access.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest` pass without live-provider tests.


## Delivery Order

Implementation may be split into three independently reviewable pull requests
without changing the approved behavior:

1. materialize work/lab directories, add explicit run space and durable access
   snapshots, and load selected memory;
2. add the resident workspace catalog, CLI, config authority, and memory
   projection; and
3. connect run roots to built-in tools and none/Docker hosting, then update
   end-to-end documentation and tests.

No slice claims the feature complete until all acceptance tests above pass.


## Risks

- Splitting mandatory runtime protocol from authored instruct/context changes
  the practical meaning of `none`; tests must prove only optional content is
  suppressed and structured-output behavior is unchanged.
- Workspace paths and memory content become durable prompt/run data in the
  local run store. Documentation must state this so users do not put secrets in
  `MEMORY.md` or grant sensitive directories casually.
- Config and editable memory are two files and cannot be replaced in one
  filesystem transaction. Deterministic reconciliation and a single lock keep
  the authoritative registry safe and repair crash-time projection drift.
- Docker mounts are fixed at container launch. Honest active-path intersection
  and explicit restart reporting are required to avoid claiming a newly added
  workspace is already usable.
- Path-aware tools can enforce roots, but arbitrary shell commands in an
  unsandboxed or long-lived agent process cannot provide hard per-run
  containment. Instructions and docs must not present this feature as a
  security sandbox.
- Existing configurations may contain a top-level key named `workspaces` with
  another shape. Parsing must fail with an actionable agent-config error rather
  than reinterpret or overwrite it.


## Open Questions

None.
