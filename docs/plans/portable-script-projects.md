# Portable Script Projects

Status: Feature definition; no runtime implementation. Confirmed: script-based
configuration discovery, layered TOML, nearest catalog, script-directory workdir,
no implicit caller-directory workspace, best-effort initialization, and resident
agent-local ownership of workspaces and jobs. Applying that ownership boundary
to roaming ancestor layers and temporary-workspace CLI syntax remain proposals.

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
- Local script input includes use process directory; hosted script calls send raw
  input whose default resolver uses server process directory (or agent home). Scheduled jobs
  instead use their authored file's directory. These bases are not interchangeable.
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
| `procdir` | The submitting client's OS working directory, captured at the invocation boundary. Base for explicit CLI paths and Chat/script input files; never an implicit project or workspace selection. |
| `runtime_dir` | Generated `.toolang/`, under `git_root` when present, otherwise `script_dir`. Never an authored path base. |
| `workspace` | A named authorized directory, such as `repo`; it does not by itself identify the current position. |
| `workdir` | A Run's current workspace location, such as `repo://src`. Identifies both the selected workspace and the relative directory within it. Initially the script directory for roaming. |

Use `procdir` for the process directory and `workdir` for the Run location
throughout Toolang-owned documentation, interfaces, and implementation vocabulary.
The selected workspace is derived from `workdir`, not another independently mutable
setting. Chat `/cd` and Run workdir overrides change `workdir`, not `procdir` or
the base of client file attachments. When aligning existing field names during
implementation, preserve historical-record readability at decoding boundaries;
new records and public interfaces use the unambiguous terms.

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
`git_root`, inclusive. Never search from process directory or above that boundary.
Script symlinks follow their real target's project. A configuration symlink keeps
its discovered pathname's directory as its base; generated aliases retain that
origin rather than rebasing relative values under `.toolang/`.

## Ownership before inheritance

Inheritance follows the concept's owner, not simply the presence of a field in
an outer config. Preserve this resident design as the rule for similar features:

| Ownership | Examples | Rule |
| --- | --- | --- |
| Agent-local | Workspace grants, authored program, jobs (tasks/chores), schedules and execution state | Resident inputs belong to agent home; never inherit or discover them from the shared root. |
| Shareable | Catalogs, reusable caps, plugin settings, run/model defaults and policies | May be supplied at root and specialized by the agent using their existing selection/merge rules. |
| Run-local | Temporary workspace grants and workdir overrides | Belong to the accepted Run; do not write them into agent or shared configuration. |

For roaming, propose treating ancestor TOML files as shared layers and only
`script_dir/toolang.toml` as the agent-local configuration. Ancestor workspace
entries therefore do not grant access to descendant scripts. Job files are not
TOML merge fields; this scope adds no ancestor job discovery or roaming job
commands. Visiting retains its explicit agent context without adopting agent-local
resources from cache parents. Future fields must declare ownership and inheritance
semantics before joining shared loading or inspection.

## Configuration and catalog selection

Load every discovered `toolang.toml` from outermost to innermost, selecting only
fields eligible for that layer's ownership. Resolve each retained filesystem value
against its originating file before combining layers. Reuse resident field
semantics rather than applying one generic merge to every field:

- `default`, `compact`, `limit`, `allow`, and `sandbox` use their existing ordered
  resolvers, including model-override, allow-sentinel, and sandbox-reset behavior.
- Plugin mappings and authored cap tables merge recursively; nearer scalar and
  array values replace older values. Arrays are not concatenated.
- Read `[workspaces]` only from the agent-local configuration, preserving its
  ordering and validation. This preserves resident home-only grants; the proposed
  roaming equivalent is script-local grants, without ancestor workspace merging.

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
  toolang.toml                # shared policy and plugin settings
  toolang.catalog.json
  scripts/
    toolang.toml              # local [workspaces] project = "..", plus overrides
    toolang.catalog.json      # selected catalog, if present
    aide.too                  # script_dir and default workdir: repo/scripts
```

Running this script from `/tmp` changes neither configuration nor workdir. The
script-local `project = ".."` explicitly grants repository access; discovering the
Git root alone does not. A non-Git archive needs config beside its entry script to
retain this behavior. Keep root entry scripts for projects intended for both Git
and archive distribution without additional configuration.

## Workspaces and path handling

For roaming scripts, propose implicit `script://` bound to `script_dir`, alongside
`lab` and configured workspaces. Initial workdir is `script://`, regardless of
workspace insertion order, unless explicitly selected otherwise. Reserve `script`
against conflicting authored grants for this placement. Do not implicitly grant the caller's directory
or Git root as workspaces. Keep existing `lab` ownership rules.

| Input | Path base |
| --- | --- |
| Workspace or catalog path in config | Its own `config_dir`, including inherited values; expand `~`, retain absolute paths. |
| Explicit CLI path, `@file`, or relative catalog environment override | Captured `procdir`, resolved before server/container handoff. This is explicit input, not project discovery. |
| Runtime fs/shell relative path | The Run's current workdir; never the hosting process's incidental process directory. |
| Input `@file` references | Use the input-origin rules below, separately from Run workdir. |
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
An unavailable binding fails rather than rebinding to the new process directory.
Ordinary calls from different directories now use identical script bindings.
Temporary grants still require isolated Run bindings and compatible host/guest
mounts: reuse a server only when its binding map matches; never mutate an active
server's grants from another caller. Separate lifecycle ownership for incompatible
execution contexts, while keeping run inspection and control addressable.

## Input file references

Resolve `@file` against the input's origin, not automatically
against the script directory or Run workdir. Explicitly including one file supplies
its content; it does not grant the containing directory as a workspace.

| Input origin | Relative `@file` base |
| --- | --- |
| Terminal Chat input | The Chat client's captured `procdir`; changing its selected workspace/workdir does not change this base. |
| Script CLI arguments, named inputs, or stdin | The invoking client's captured `procdir`, including hosted/containerized execution. Piped stdin does not identify an originating file. |
| Scheduled task/chore body | The authored job file's directory; retain the existing agent-home fallback when no file origin exists. |
| Server-authored API input without a client attachment context (proposal) | The request's resolved, authorized workdir; never incidental server process directory. Client-local files must be transferred as attachments. |
| Uploaded/typed attachment | Its explicit resource identity/content; no filesystem-relative lookup. |

For example, from `/repo`, `too /tools/aide.too review '@notes.md'` attaches
`/repo/notes.md`, while the roaming Run still starts in `/tools`. An explicit
workdir override or later directory change must not reinterpret that attachment.

Capture the include context at input submission. Resolve client files into typed
Parts on the client before execution handoff; preserve content through the request
transport rather than sending a relative path for the server to reopen. Preserve
Content parsing, prompt expansion, coercion, and provenance: an input `$prompt`
expands in the same include context, and escapes/fences keep their existing meaning.
Do not implement this as raw string substitution or recursively parse included
file bytes. Missing/unreadable files reject the input before Run acceptance.

Agic/flow bodies currently supply no file include resolver; `@file` there is not
implicitly script-relative. Adding source-relative attachments to authored bodies
is a separate language decision, outside this scope. The implementation must pass
an explicit resolver/base, never read the process directory deep inside the shared parser.

## Placement comparison and inspection

| Behavior | Resident | Roaming | Visiting |
| --- | --- | --- | --- |
| Authored inputs | Shared Toolang root plus agent-local home | Shared ancestor layers plus agent-local script directory | Downloaded source and existing visiting setup; no traversal of cache parents or implicit companion fetch |
| Default workdir | Existing configured workspace selection, otherwise `lab` | `script://` | Existing authorized-workspace selection, otherwise `lab`; a download cache is not a project workspace |
| Relative config values | Each authored file's directory | Each authored file's directory | Origin of any explicitly supplied local config; never infer a remote base from a temporary cache |
| `info`, `models`, `providers`, `tools`, `workspace list` | Supported | Support identically | Support identically |
| Persistent workspace edits | Agent config | Script-local `toolang.toml` | Reject: no durable local authored project; download the source to edit it |
| Credentials | Existing root/agent dotenv and process environment | Process/container environment; no project/generated dotenv | Preserve existing visiting environment behavior; no new credential discovery |

Use one inspection pipeline against the selected layout and ordered sources, with
the same options, filtering, and output fields for all placements. `info` shows
source, boundary, config layers, catalog selection and provenance, runtime root,
and effective workdir/workspaces. `workspace list` includes implicit and temporary
grants, availability, and origins. Inspection must expose field ownership and
out-of-scope declarations without treating them as effective agent settings.
No model Run or secret values are needed.
Distinguish newly resolved settings from an existing server's mounted bindings;
historical inspection uses recorded Run bindings. Targetless global listings keep
their existing root-level meaning. Placement differences follow source ownership
and workspace authorization, not different inspection capabilities.

Roaming `workspace add/remove` edits only script-local `toolang.toml`, creating it
exclusively when absent. Preserve comments and write relative paths against that
file. Shared ancestor declarations are not effective workspace entries and cannot
be removed through an agent workspace command. No inheritance-masking mechanism
is needed. Resident mutations retain their destination and use the same
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
| Layering/catalogs | Three TOML layers with conflicting scalars/lists/model settings and different relative bases; home/script-local workspace ownership; independent nearest catalog; explicit path precedence; no catalog merging or generic project `catalog.json`. |
| Placement inspection | Same commands/filters/provenance for all three placements; use selected layout; no parent-cache discovery, secret output, model Run, or inherited root/ancestor workspace grants, jobs, schedules, or execution state. Shared catalogs, caps, and policies remain available to multiple agents. |
| Workspaces | Script-relative fs and shell access; no implicit grants to process directory or Git root; explicit temporary workspace/workdir; aliases, nesting, collisions, concurrency, historical authorization, and host/guest parity. |
| Input includes | Distinct same-named files in caller, script, job, and server directories; primary/named/stdin input; prompt expansion and escapes; Chat workspace changes and script workdir overrides do not change attachment bases; local/hosted/guest attachment parity; missing files reject input; attaching a file grants no workspace. |
| Persistence | Terminology updates retain historical-record readability; config-origin changes and equal-byte relocation/retargeting invalidate bindings; equal script stems stay distinct; stale materialization errors; delete/recreate runtime data; read-only fallback. |
| Mutations/init | Source-local config edits without ancestor mutations; either init target preexists; a file appears after preflight; creation/write failures and concurrent init preserve existing/partial files and return failure. |

Likely files: `common/layout.py`, `up/{process,mounts,sandbox}.py`,
`setup/{config,watcher,types}.py`, `state/{config,source,prepare,state}.py`,
`plugin/{config,catalogs/models_dev/path}.py`, `cli/common/agent_server.py`,
`cli/toolang/routing.py`, relevant CLI commands, execution records/binding logic,
and their unit/integration tests. Input-reference touchpoints also include
`lang/{input,includes}.py`, `execution/{calls,schemas}.py`, API input transport,
Chat clients, and `work/scheduler.py`. Resolve paths at loading/CLI boundaries; core
schemas receive concrete values and remain independent of runtime orchestration.

Implementation requires the standard Ruff, type, and offline test checks. This
PR only updates the definition; validate source accuracy, references, and
`git diff --check`. Proposed workspace names/flags, cache fallback, and the
intentional placement differences above remain subject to human approval.

## References and limits of the analogy

Node.js distinguishes [module location](https://nodejs.org/api/modules.html#__dirname)
from [process directory](https://nodejs.org/api/process.html); its
[ordinary relative fs paths](https://nodejs.org/api/fs.html#file-paths) still use
process directory. Toolang borrows that separation, but explicitly chooses script-based
workdir. Git-bounded discovery is Toolang's own rule, with
[working-tree root semantics](https://git-scm.com/docs/git-rev-parse).
