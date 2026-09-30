# Portable Script Projects

Status: Definition only; no implementation. Confirmed rules are recorded below.
Proposals and remaining decisions are listed at the end, before implementation.

## Goal

Version `.too`, `toolang.toml`, and optional `toolang.catalog.json`. Keep generated
`.toolang/` disposable. A script's configuration and default workspace must follow
its source, independently of the directory from which it is invoked. Execution
still depends on the installed Toolang version, credentials, and external services.

Current gaps: roaming discovers only sibling TOML; a materialized regular config
can shadow it; workspace paths require absolute values; authored catalog paths
are overwritten; inspection commands assume resident layouts. Client file inputs
also resolve differently when execution moves to a server or container.

## Directory and input rules

| Term | Meaning |
| --- | --- |
| `procdir` | Captured client process directory; base for explicit CLI paths and Chat/script input attachments. |
| `srcdir` | Parent of the real `.too` source file, after resolving symlinks. |
| `workspace` | A named authorized directory. |
| `workdir` | Run location such as `repo://src`; identifies both workspace and directory within it. |

Use `procdir`, `workdir`, and `srcdir` consistently in Toolang-owned terminology. Changing
workdir does not change procdir. Preserve historical-record readability when
aligning existing fields; do not introduce a second workspace-selection setting.

| Relative input | Resolve against |
| --- | --- |
| Chat or script `@file`, including named input and stdin | Client `procdir`; piped input has no originating filename. |
| `@file` in `task.md` or `chore.md` | That authored file's directory; retain agent-home fallback for fileless jobs. |
| Workspace/catalog path in configuration | The authored configuration file's directory, before merging layers. |
| Other explicit CLI paths or catalog environment override | Client `procdir`, before process/container handoff. |
| Runtime fs/shell paths | Run `workdir`. |

Absolute paths and home expansion retain their meanings. Docker guest roots remain
absolute guest paths; service command strings and remote cap references are not
host filesystem paths to rebase. Shared parsers receive explicit path context.

For example, from `/repo`, `too /tools/aide.too review '@notes.md'` attaches
`/repo/notes.md` while the proposed script workdir is `/tools`. An attachment
supplies file content, not workspace access. Missing files reject input before
Run acceptance. Preserve Content escaping, fences, prompt expansion/provenance,
and one-pass inclusion; included bytes are not recursively parsed.

Local and hosted calls must use the same client attachment contents. Neither
workdir changes nor server procdir may reinterpret them. Agic/flow bodies currently
have no file resolver; adding source-relative attachments there is outside scope.

## Discovery and ownership

Search both companion names independently from `srcdir` through the nearest
Git working-tree root, inclusive. Only directories containing a companion file
contribute configuration layers; empty ancestors are not runtime locations.
A worktree, submodule, or nested repository stops at its own working-tree root, not a metadata directory or outer repository. Without a working tree, inspect
only `srcdir`. Do not use procdir, inherit Git relocation environment overrides,
or cross a broken nearest Git marker. Git-backed discovery must diagnose an
unresolvable boundary rather than silently change its configuration inputs.

A configuration symlink uses the directory of its discovered pathname; generated
runtime aliases must retain that authored origin. Non-Git distributions therefore
need configuration beside their entry script; removing Git metadata can change
ancestor discovery. Root entry scripts avoid that distribution difference.

Inheritance follows ownership, not merely the existence of an outer TOML field:

| Owner | Examples | Rule |
| --- | --- | --- |
| Agent | Workspace grants, authored program, task/chore definitions, schedules, execution state | Resident reads agent home only; shared root does not supply these resources. |
| Shared | Catalogs, reusable caps, plugins, defaults and policies | Root may supply them; agent layers specialize them. |
| Run | Temporary grants and workdir overrides | Accepted for one Run; never persisted as authored configuration. |

For roaming, the proposed equivalent is script-local agent configuration and
shared ancestor configuration. This means ancestor workspace entries do not grant
access to descendant scripts. No ancestor job discovery or roaming job commands
are added. Future settings must specify their ownership before joining inheritance.

## Merge and catalog rules

Read TOML outermost to innermost, retaining only fields eligible at each scope.
Resolve retained relative paths per source file, then apply existing resident
field resolvers: `default`, `compact`, `limit`, `allow`, and `sandbox` retain
their special semantics. Plugin/cap mappings merge recursively; nearer scalar/list
values replace older values, without concatenating arrays. Agent-local workspace
declarations retain their ordering and validation.

Catalog files never merge. Precedence is CLI override, environment override,
authored layers nearest first, then bundled data. Within a layer, an explicit
`plugin.model_catalog.models_dev.path` precedes its companion file. A nearer
companion wins over an outer path. Roaming uses `toolang.catalog.json`; resident
keeps agent-home then root `catalog.json`. Generic project `catalog.json` is ignored.

Invalid TOML anywhere in the selected chain fails with its path. An invalid
selected catalog fails rather than falling back; unused outer catalogs are not
loaded. Configuration and catalog discovery do not depend on one another.

```text
repo/                         # Git working-tree boundary
  toolang.toml                # shared settings
  toolang.catalog.json
  scripts/
    .toolang/                 # generated runtime data for sources here
    toolang.toml              # local [workspaces] project = "..", if needed
    aide.too                  # default workdir: repo/scripts
```

The local workspace declaration grants repository access explicitly. Git-root
discovery alone changes neither workdir nor access.

## Workspaces and placement

Propose implicit `script://` for roaming, rooted at `srcdir`, as its initial
workdir unless explicitly selected otherwise. Keep `lab` and configured workspaces;
reject conflicting authored use of `script`. No implicit procdir or Git-root grant.
Keep existing duplicate/nested-root rules; allow an implicit-root alias without
duplicating mounts, retain explicit URI names, and use `script` for an exact tie.

Capture concrete bindings at Run acceptance. Children inherit them; restart and
resume retain recorded bindings and authorization checks. Never rebind historical
workspace names to a caller's new directories. Reuse a running server only with
compatible bindings; a mismatch is an actionable error, not an implicit remount.
This scope does not prescribe new multi-runtime lifecycle infrastructure.

| Behavior | Resident | Roaming | Visiting |
| --- | --- | --- | --- |
| Config sources | Shared root + agent home | Shared ancestors + script-local config (proposal) | Existing explicit visiting context; no discovery above download cache |
| Default workdir | Existing workspace selection, otherwise `lab` | Script directory | Existing authorized selection, otherwise `lab` |
| Inspection | Same commands, options, and meanings in all placements | Same | Same |
| Persistent workspace edits | Agent config | Script-local TOML (proposal) | Unavailable without a durable authored project |

Unify `info`, `models`, `providers`, `tools`, and `workspace list` through the
selected layout, not reconstructed resident paths. Show effective sources, config
layers, catalog choice, workspaces/workdir, and generated root. Distinguish current
configuration from an existing server or recorded Run; report out-of-scope settings
without making them effective. Do not start a model Run or print secrets.
Targetless global inspection keeps its existing root-level meaning.

Workspace mutations preserve comments, store paths relative to the authored file,
and never edit shared ancestors or generated projections. Keep resident/visiting
credential behavior. Roaming receives process/container environment only: no
project/generated dotenv discovery or implicit `~/.toolang` inheritance.

## Generated files and initialization

Roaming runtime data belongs at `srcdir/.toolang/`, matching the existing
source-local layout. Ancestor configuration/catalog files and the Git boundary do
not relocate it. This generated directory is distinct from the resident root
(default `~/.toolang`); do not merge their configuration or state. Keep canonical
runtime names `agent.too`, `config.toml`, and `catalog.json`. A layered config cannot
be a single source symlink: retain its ordered origins and generate any required runtime
projection from them. All consumers use one captured input set; do not generically
flatten special merge rules or apply layers twice. Derived files are never authored
overrides. Identify scripts and revisions using source identity, origins, and
bindings so equal stems or unchanged bytes after relocation cannot reuse wrong data.

Synchronize owned entries; source removal must clear owned stale links/projections.
Unexpected regular files or unowned entries are conflicts to diagnose, not silent
overrides or files to delete. Deleting generated state while stopped loses history,
logs, caches, and `lab` output, but preserves authored configuration. Report unwritable
runtime roots clearly; automatic cache fallback remains a separate proposal.

`too init DIR` creates executable `aide.too` and comment-only `toolang.toml`, with
no default runnable, catalog, secrets, or machine-specific paths. Suggest ignoring
`.toolang/`; do not edit `.gitignore` automatically. Check every destination first,
including directories and symlinks. Then create TOML followed by script exclusively.
On conflict or write failure, report the path and exit nonzero, retaining partial
output. No overwrite, rollback, transaction journal, lock, or automatic recovery.

## Acceptance and implementation

- Discovery: different procdirs, nested configs, script/config symlinks, worktrees,
  submodules, non-Git distributions, and invalid boundaries/candidates.
- Scope/merge: three layers with special defaults, lists and different path bases;
  no inherited agent-only resources; nearest catalog and explicit-path precedence.
- Workspaces: script-relative tools, aliases/nesting, equal script stems, relocation,
  recorded bindings, incompatible-server rejection, and host/guest parity. Runtime
  data stays beside the source even when configuration/catalogs come from ancestors;
  the resident root remains independent.
- Input: distinct same-named files in client/script/job/server directories; Chat,
  script, named and piped input; prompt expansion; workdir changes; missing files;
  identical attachments across transports without granting directory access.
- Inspection/mutation: all placements use the same effective inputs, origins and
  options; no secret output, model Run, ancestor mutation, or derived-file editing.
- Init/runtime: either target preexists, post-check collision, concurrent init,
  partial write failure, stale generated files, and delete/recreate runtime data.

Touchpoints: `common/layout.py`; `up` materialization/mounts; `state` and `setup`
loading; catalog selection; CLI routing, script/init/workspace/inspection/Chat;
input parsing and execution/API transport; job input resolution. Keep path resolution
at loading/CLI boundaries and core schemas independent of runtime orchestration.
Tests stay offline. Implementation requires Ruff, type checks, and the full suite;
this definition requires source/reference verification and `git diff --check`.

## Remaining decisions and review risks

1. Confirm the proposed roaming local/shared boundary and `script` workspace name. Source-local `.toolang/` placement is confirmed; the remaining proposals
   are not implementation approval.
2. Temporary workspace convenience may use `--workspace NAME=PATH` plus
   `--workdir URI`; syntax and read-only-project cache fallback are optional
   follow-ups, not reasons to add lifecycle infrastructure to the core change.
3. Finish the attachment transport contract before implementing hosted parity.
   Existing authored requests carry source/workdir; the typed-input route does not
   preserve the same workdir/provenance contract. Define transport of client-read
   Parts without losing prompt expansion, coercion, accepted source revision, or
   restart behavior. Raw API input without a client context also needs an explicit
   resource policy; do not silently reinterpret it under a server directory.

Node.js distinguishes [module location](https://nodejs.org/api/modules.html#__dirname)
from [process directory](https://nodejs.org/api/process.html), while its
[relative file operations](https://nodejs.org/api/fs.html#file-paths) use the latter.
Toolang's script workdir and Git-bounded search are explicit product choices;
Git root follows [working-tree semantics](https://git-scm.com/docs/git-rev-parse).
