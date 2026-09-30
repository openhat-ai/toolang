# Portable Script Projects

Status: Feature definition; no runtime implementation. Confirmed: script-based
configuration discovery, layered TOML, nearest catalog, script-directory workdir,
no implicit caller-cwd workspace, and best-effort initialization. Remaining choices
are proposals for approval, including temporary-workspace CLI syntax.

## Goal and scope

Version `.too`, `toolang.toml`, and optional `toolang.catalog.json`; keep generated
`.toolang/` disposable. Moving a project or launching its script from another
directory must not change its authored configuration or default workspace.
Cover discovery, relative paths, workspace bindings, inspection, materialization,
and `too init DIR`. Do not add secret storage, project dotenv loading, automatic
remote companion downloads, or dependency locking. Equivalent behavior assumes
equivalent Toolang versions, credentials, catalogs, and external services.

## Current implementation

- `up/process.py` links only the real script's sibling `toolang.toml`; an existing
  regular materialized config can silently shadow it.
- Resident Setup reads root then agent config using field-specific resolvers;
  plugins recursively merge mappings, and nearer scalar/list values replace older
  ones. Agent State workspaces currently come only from agent-home config.
- Catalog selection uses CLI, environment, home file, root file, then bundled
  data. `setup/watcher.py` overwrites authored `models_dev.path`.
- Workspace roots must be absolute. `info` accepts all placements, but
  `models/providers/tools/workspace` accept only resident targets. Model/tool
  listing code also constructs resident layouts internally; routing alone is
  insufficient to enable these commands elsewhere.

## Directories and Git boundary

| Term | Meaning |
| --- | --- |
| `script_dir` | Parent of the real `.too` file after resolving script symlinks. Default roaming workdir and discovery starting point. |
| `git_root` | Root of the nearest Git working tree containing `script_dir`. A configuration-search boundary, not an automatic workspace grant or workdir. |
| `config_dir` | Directory of each discovered authored configuration pathname; every layer has its own path base. |
| `invocation_dir` | Captured process cwd. Used only to interpret explicit CLI paths, never to discover project settings or choose default workspaces. |
| `runtime_dir` | Generated `.toolang/`, under `git_root` when present, otherwise `script_dir`. Never an authored path base. |
| `workdir` | A Run's current location within an authorized workspace. Initially the script directory for roaming; explicit selection can change it. |

`git_root` means the working-tree top level, not the `.git` metadata directory,
a remote repository, or the outermost repository. A linked worktree has its own
root; a submodule or nested repository stops at its own root, not its parent.
Determine it from `script_dir`, with semantics equivalent to
`git -C <script_dir> rev-parse --show-toplevel`; ignore inherited Git directory
and worktree relocation overrides. Recognize both `.git` directories and gitfiles.
An invalid nearest Git marker is an error; it must not expose an outer project's
configuration. Without a working tree, search only `script_dir`. Bare repositories
supply no working-tree boundary. Support ordinary directories without requiring
Git to be installed; Git-backed discovery must fail clearly if it cannot establish
the boundary instead of silently using different configuration.

Both companion filenames are searched independently from `script_dir` through
`git_root`, inclusive. Never search from process cwd or above that boundary.
Script symlinks follow their real target's project. A configuration symlink keeps
its discovered pathname's directory as its base; generated aliases retain that
origin rather than rebasing relative values under `.toolang/`.

## Configuration and catalog selection

Load every discovered `toolang.toml` from outermost to innermost. Resolve each
layer's relative filesystem values before combining it with other layers. Reuse
resident field semantics rather than applying one generic merge to every field:

- `default`, `compact`, `limit`, `allow`, and `sandbox` use their existing ordered
  resolvers, including model-override, allow-sentinel, and sandbox-reset behavior.
- Plugin mappings and authored cap tables merge recursively; nearer scalar and
  array values replace older values. Arrays are not concatenated.
- For roaming project layers, merge `[workspaces]` by name, with nearer definitions
  overriding that name and preserving the declared ordering. Validate the resulting
  grants. These are all project-authored agent inputs. Preserve resident's existing
  agent-only workspace grants; do not turn user-level root settings into grants
  for every resident agent as a side effect of this work.

Select only the nearest `toolang.catalog.json`; never merge catalog files or read
a project's generic `catalog.json`. Its discovery does not depend on where TOML
files exist. Preserve explicit catalog overrides with this shared precedence:
CLI `--catalog`, then `TOOLANG_MODEL_CATALOG`, then authored layers from nearest
to farthest, then bundled data. At each layer, an explicitly declared
`plugin.model_catalog.models_dev.path` precedes its companion catalog. An inherited
outer path must not override a nearer companion. Resident layers are agent home
then Toolang root, using their existing `catalog.json` filenames.

A directory, unreadable file, dangling link, or invalid TOML in the configuration
chain fails with its path. An invalid selected catalog fails; do not fall back to
an outer catalog. Unselected outer catalog files need not be loaded.

```text
repo/                         # git_root; contains .git
  toolang.toml                # outer policy, e.g. [workspaces] project = "."
  toolang.catalog.json
  scripts/
    toolang.toml              # overrides matching outer settings
    toolang.catalog.json      # selected catalog, if present
    aide.too                  # script_dir and default workdir: repo/scripts
```

Running this script from `/tmp` changes neither configuration nor workdir. The
outer `project = "."` explicitly grants repository access; discovering the Git
root alone does not. A non-Git archive needs config beside its entry script to
retain this behavior. Keep root entry scripts for projects intended for both Git
and archive distribution without additional configuration.

## Workspaces and path handling

For roaming scripts, propose implicit `script://` bound to `script_dir`, alongside
`lab` and configured workspaces. Initial workdir is `script://`, regardless of
workspace insertion order, unless explicitly selected otherwise. Reserve `script`
against conflicting authored grants for this placement. Do not add implicit `cwd`
or `git` workspaces. Keep existing `lab` ownership rules.

| Input | Path base |
| --- | --- |
| Workspace or catalog path in config | Its own `config_dir`, including inherited values; expand `~`, retain absolute paths. |
| Explicit CLI path, `@file`, or relative catalog environment override | Captured `invocation_dir`, resolved before server/container handoff. This is explicit input, not project discovery. |
| Runtime fs/shell relative path | The Run's current workdir; never the hosting process's incidental cwd. |
| Source-language include | Its owning parser's explicit base; preserve existing include semantics. |
| Docker guest root | Remains an absolute guest path. |
| Service command/arguments or cap references | Keep their existing semantics; do not rebase arbitrary strings or remote references. |

Proposed convenience: repeatable `--workspace NAME=PATH` for temporary grants and
`--workdir URI` to explicitly select an initial workdir on a local script call.
For example, `too /path/aide.too whats_for --workspace here=. --workdir here://`
would operate on the caller's directory deliberately. These flags are proposed,
not existing commands. A grant alone does not change the default workdir. Never
persist it to TOML; reject duplicate names or replacements of existing grants.
The inspection commands must accept the same temporary workspace options.

Configured duplicate roots keep their existing validation. A configured or
explicit temporary workspace may alias an implicit root; preserve its name and
avoid duplicate mounts. Allow nested roots and preserve most-specific-root
selection. Prefer `script` for an exact implicit/configured tie; explicit URIs
retain their names. A parent grant includes descendants, and workspace checks are
not a substitute for OS sandboxing.

Capture bindings with Run acceptance. Child Runs inherit them; retry, rerun,
fork, and resume retain historical bindings and current authorization checks.
An unavailable binding fails rather than rebinding to the new process cwd.
Ordinary calls from different directories now use identical script bindings.
Temporary grants still require isolated Run bindings and compatible host/guest
mounts: reuse a server only when its binding map matches; never mutate an active
server's grants from another caller. Separate lifecycle ownership for incompatible
execution contexts, while keeping run inspection and control addressable.

## Placement comparison and inspection

| Behavior | Resident | Roaming | Visiting |
| --- | --- | --- | --- |
| Authored inputs | Explicit Toolang root and agent home | Script-to-Git-root project layers | Downloaded source and existing visiting setup; no traversal of cache parents or implicit companion fetch |
| Default workdir | Existing configured workspace selection, otherwise `lab` | `script://` | Existing authorized-workspace selection, otherwise `lab`; a download cache is not a project workspace |
| Relative config values | Each authored file's directory | Each authored file's directory | Origin of any explicitly supplied local config; never infer a remote base from a temporary cache |
| `info`, `models`, `providers`, `tools`, `workspace list` | Supported | Support identically | Support identically |
| Persistent workspace edits | Agent config | Script-local `toolang.toml` | Reject: no durable local authored project; download the source to edit it |
| Credentials | Existing root/agent dotenv and process environment | Process/container environment; no project/generated dotenv | Preserve existing visiting environment behavior; no new credential discovery |

Use one inspection pipeline against the selected layout and ordered sources, with
the same options, filtering, and output fields for all placements. `info` shows
source, boundary, config layers, catalog selection and provenance, runtime root,
and effective workdir/workspaces. `workspace list` includes implicit and temporary
grants, availability, and origins. No model Run or secret values are needed.
Distinguish newly resolved settings from an existing server's mounted bindings;
historical inspection uses recorded Run bindings. Targetless global listings keep
their existing root-level meaning. Placement differences follow source ownership
and workspace authorization, not different inspection capabilities.

Roaming `workspace add/remove` edits only script-local `toolang.toml`, creating it
exclusively when absent. Preserve comments and write relative paths against that
file. An inherited entry cannot be removed locally without a masking mechanism;
report its defining file rather than silently editing an ancestor or deleting a
local override and unexpectedly exposing the inherited grant. General masking is
outside this scope. Resident mutations retain their destination and use the same
CLI/config path-base rules. Do not mutate implicit or temporary grants persistently.

## Materialization, portability, and init

Keep `toolang.toml` and optional `toolang.catalog.json`; add no alternate filename.
The generated home keeps canonical `agent.too`, `config.toml`, and `catalog.json`
names. Link source/catalog files; layered config may require a generated projection
instead of a single link. Preserve every layer's origin and field-specific resolved
meaning. All runtime consumers must use the same captured inputs, including State,
Setup, sandbox selection, mounts, and inspection. Generated projections are not
authored overrides or edit targets. Identity includes origins and bindings, not
only file bytes; scripts with equal stems in different directories remain distinct.

Synchronize owned materialized entries safely. Unexpected files must produce a
migration/conflict diagnostic rather than shadow sources or be deleted. Never
load hidden policy from a generated roaming root or implicitly inherit `~/.toolang`.
Deleting `.toolang/` while stopped loses history, logs, caches, and `lab` output,
but authored behavior is reconstructible. A project move starts fresh bindings.
For read-only projects, retain the proposed per-user cache fallback: Linux
`$XDG_CACHE_HOME/toolang/roaming` (default `~/.cache`) or macOS
`~/Library/Caches/toolang/roaming`, keyed by canonical project path. An existing
corrupt/conflicting runtime root is an error, not a reason to silently fall back.
Report the selected runtime root; never serialize credentials into project files.

`too init DIR` creates executable `aide.too` plus comment-only `toolang.toml`,
explaining the script workspace, config-relative paths, and optional catalog.
Generate no catalog, credentials, absolute machine paths, or default runnable.
Suggest ignoring `.toolang/`, without automatically editing `.gitignore`.
Check all target paths before writing any; existing files, directories, and
symlinks fail preflight. Then create config followed by script exclusively.
A concurrent collision or write failure stops immediately with a nonzero status
and failing path. Keep already-created or partial files; no overwrite, rollback,
transaction journal, initialization lock, or automatic recovery.

## Acceptance and implementation touchpoints

| Area | Required offline checks |
| --- | --- |
| Discovery | Different invocation directories give identical inputs; script/config symlinks, root/subdirectory calls, worktrees, submodules, nested repos, non-Git directories, invalid markers and candidates. |
| Layering/catalogs | Three TOML layers with conflicting scalars/lists/model settings and different relative bases; inherited workspaces; independent nearest catalog; explicit path precedence; no catalog merging or generic project `catalog.json`. |
| Placement inspection | Same commands/filters/provenance for all three placements; use selected layout; no parent-cache discovery, secret output, model Run, or new resident root workspace grants. |
| Workspaces | Script-relative fs and shell access; no implicit grants to process cwd or Git root; explicit temporary workspace/workdir; aliases, nesting, collisions, concurrency, historical authorization, and host/guest parity. |
| Persistence | Config-origin changes and equal-byte relocation/retargeting invalidate bindings; equal script stems stay distinct; stale materialization errors; delete/recreate runtime data; read-only fallback. |
| Mutations/init | Source-local config edits and inherited-removal errors; either init target preexists; a file appears after preflight; creation/write failures and concurrent init preserve existing/partial files and return failure. |

Likely files: `common/layout.py`, `up/{process,mounts,sandbox}.py`,
`setup/{config,watcher,types}.py`, `state/{config,source,prepare,state}.py`,
`plugin/{config,catalogs/models_dev/path}.py`, `cli/common/agent_server.py`,
`cli/toolang/routing.py`, relevant CLI commands, execution records/binding logic,
and their unit/integration tests. Resolve paths at loading/CLI boundaries; core
schemas receive concrete values and remain independent of runtime orchestration.

Implementation requires the standard Ruff, type, and offline test checks. This
PR only updates the definition; validate source accuracy, references, and
`git diff --check`. Proposed workspace names/flags, cache fallback, and the
intentional placement differences above remain subject to human approval.

## References and limits of the analogy

Node.js distinguishes [module location](https://nodejs.org/api/modules.html#__dirname)
from [process cwd](https://nodejs.org/api/process.html#processcwd); its
[ordinary relative fs paths](https://nodejs.org/api/fs.html#file-paths) still use
process cwd. Toolang borrows that separation, but explicitly chooses script-based
workdir. Git-bounded discovery is Toolang's own rule, with
[working-tree root semantics](https://git-scm.com/docs/git-rev-parse).
