# Portable Script Projects

Status: Proposed for approval. Problem collection and feature definition only;
no runtime changes are included. Configuration discovery follows the confirmed
bounded upward-search rule. Other decisions below are proposed defaults.

## Goal and scope

A project can version its `.too` files, `toolang.toml`, and optional
`toolang.catalog.json`, move or clone them, and execute with equivalent authored
behavior. Generated `.toolang/` contents must not be required to reconstruct
configuration. Local script calls can access their invocation directory without
manual workspace registration.

Cover discovery, path resolution, catalog selection, invocation workspaces,
materialization, diagnostics, and transactional `too init DIR`. Keep resident
configuration locations and public commands. Do not add project dotenv loading,
secret storage, remote companion-file fetching, or dependency/version locking.
Equivalent behavior assumes equivalent Toolang versions, catalogs, credentials,
and external services; it does not promise identical model output.

## Verified gaps

- `up/process.py` discovers only the real script's sibling `toolang.toml`.
  An existing regular materialized `config.toml` silently shadows that source.
- `state/config.py` requires absolute workspace paths. `workspace add` persists
  absolute paths, and listing shows only authored grants.
- `setup/watcher.py` overwrites the authored `models_dev.path`. Catalog selection
  sees runtime home/root `catalog.json`, not a project companion.
- Script execution supplies no invocation workspace. Setup captures a process
  directory, which can be the server's directory rather than the caller's.
- Workspace/model/provider/tool commands reject roaming targets. Initialization
  creates only `aide.too`.

## Directory vocabulary and discovery

| Name | Definition and role |
| --- | --- |
| `invocation_dir` | Canonical process working directory captured once at the CLI boundary, before any directory changes. Base for CLI-relative paths. |
| `source_dir` | Parent of the real `.too` file, after resolving script symlinks. Starting point for configuration discovery. |
| `config_dir` | Parent of the selected authored configuration pathname, before materialization. Base for relative values in that file. |
| `project_dir` | Selected configuration's directory, or `source_dir` when no configuration exists. Groups project companions and generated state; grants no workspace access by itself. |
| `runtime_dir` | Disposable generated root, normally `project_dir/.toolang`. Never a base for authored relative paths. |
| `workdir` | A Run's current location inside an authorized workspace. Can change without changing any of the above directories. |

Starting at `source_dir`, select the nearest `toolang.toml`, including the nearest
Git working-tree root, and stop at that root. Recognize both `.git` directories
and worktree/submodule `.git` files. Never cross an enclosing repository boundary.
Without a Git working tree, inspect only `source_dir`. Do not start discovery at
`invocation_dir`, merge multiple ancestor configs, or fall through an invalid
candidate. A directory, unreadable file, dangling link, or malformed selected
config is an error identifying its path. A script symlink uses its real target's
project, not the symlink's containing project.

A user-authored config symlink keeps the selected pathname's parent as its base;
following its bytes must not accidentally substitute the target directory.
Generated runtime symlinks carry that original configuration origin explicitly.
Every layer of resident configuration uses its own authored origin in the same
way. Placement changes discovery locations, not path interpretation.

A Git clone with `scripts/aide.too` can inherit root configuration. An archive
without `.git` must keep the config beside its entry script, or put the entry
script beside the root config. This is the explicit limit of bounded discovery.

## Authored files, catalogs, and generated state

Keep the existing `toolang.toml` name; do not add a `toolang.config.toml` alias.
Use two lightweight project companions, without another authored directory:

```text
project/
  aide.too
  toolang.toml
  toolang.catalog.json   # optional; one complete catalog
  .toolang/             # generated, ignored by Git
```

Discover `toolang.catalog.json` only in `project_dir`; do not independently search
ancestors or consume the project's generic `catalog.json`. The catalog can exist
without a TOML file, in which case `project_dir` is `source_dir`.

Materialize `agent.too`, `config.toml`, and `catalog.json` as relative links to their
selected sources. Record source origins separately from those canonical runtime
names. Identify scripts by their relative source paths, not only their stems, so
`a/aide.too` and `b/aide.too` cannot share agent state. Runtime identity must detect
hash collisions rather than treating two sources as one agent.

Use this catalog precedence across placements, selecting one complete catalog:

1. Explicit CLI `--catalog`.
2. `TOOLANG_MODEL_CATALOG` from the invoking environment.
3. Agent configuration's `plugin.model_catalog.models_dev.path`.
4. Agent companion catalog (`toolang.catalog.json` for a project).
5. Root configuration's catalog path, then root `catalog.json`, where that root
   is an authored configuration location.
6. Bundled catalog.

The generated roaming root is not an authored root: its `config.toml`,
`catalog.json`, or `.env` must not introduce hidden policy. Report nonempty legacy authored files there
as migration conflicts rather than silently ignoring them. Do not implicitly
inherit `~/.toolang` settings for project scripts. Preserve resident root/agent
layers and their existing dotenv behavior. For roaming scripts, credentials come
from the supplied process/container environment; do not load project or generated
dotenv files, or serialize credentials into companions or provenance metadata.

A selected invalid catalog fails with its source and reason; never silently use
a lower-priority catalog. Preserve explicit configured paths instead of overwriting
them with automatic selection.

Synchronize links under a materialization lock with atomic individual replacement.
Remove only owned stale links after source deletion. An unexpected regular file,
directory, or unowned link at a managed destination is an actionable error, never
an override or an automatic deletion. Explain how to back up legacy authored
settings outside `.toolang/` before rebuilding it. New invocations must reject
invalid current inputs; a running invocation may retain its accepted snapshot,
with refresh errors visible instead of silently claiming the new config applied.

Runtime data includes caches, history, logs, and `lab` contents. Deletion while
stopped loses those records and scratch outputs, but not authored settings. Do not
promise history recovery or permit cleanup while executions are active. On a
read-only project filesystem, use a per-user cache root keyed by canonical project
path: `$XDG_CACHE_HOME/toolang/roaming` on Linux (default `~/.cache`), or
`~/Library/Caches/toolang/roaming` on macOS. Require an absolute XDG override.
Only an absent, uncreatable local runtime root triggers this fallback; corruption
or conflicts in an existing root must remain visible. Report the selected root.
Moving a project starts fresh runtime bindings; copied old bindings must not be
reused merely because source bytes match.

## Path rules and workspace behavior

| Input | Resolution |
| --- | --- |
| Configuration workspace roots and catalog paths | Relative to that field's originating `config_dir`; expand `~`, retain absolute paths. |
| CLI paths, CLI `@file` inputs, relative catalog environment override | Relative to captured `invocation_dir`. Resolve before server/container handoff. |
| Language-owned includes | Keep the owning parser's explicit source base; do not reinterpret them as configuration paths. |
| Docker guest `root` | Remains an absolute guest path, never rebased against a host config directory. |
| Service command/argument strings and cap references | Retain their existing semantics; do not treat arbitrary strings or remote references as filesystem paths. |

Resolve filesystem paths at loading/CLI boundaries, then pass concrete values to
State, Setup, and hosting. Preserve original spelling and origin for diagnostics.
Resolve each config layer before merging. Prepared-state and catalog identities
must account for resolved bindings/origins, including project moves and link
retargeting, not just unchanged file bytes.

For a local `.too` script invocation, add `cwd` bound to `invocation_dir`, alongside
`lab` and configured workspaces. Initialize its workdir to `cwd://` unless the
caller supplies an authorized workdir. Do not automatically grant the script or
config directory: invoking a utility stored elsewhere should operate on the
caller's project. Persistent servers, scheduled jobs, and remote API requests must
not infer grants from a server process's working directory. The distinction is
whether there is a local script invocation, not a different path-resolution rule.
Existing resident/visiting workspace defaults otherwise stay unchanged.

Reserve `cwd` for invocation binding wherever that binding is supplied; reject a
configured collision with a rename diagnostic, without silently overriding it.
Keep existing `lab` ownership rules. Configured duplicate roots retain their
existing validation. A configured root may equal implicit `cwd`: retain its name
as an alias without duplicating the host mount. Allow nested roots, preserving
existing confinement and most-specific-root selection; use `cwd` to break exact
implicit/configured ties for absolute paths. Explicit workspace URIs retain their
chosen name. A grant to a parent includes its descendants; aliases are not
isolation boundaries.

Persist invocation bindings with Run acceptance, separate from shared authored
configuration. Child Runs inherit them; retries, reruns, forks, and resumed threads
retain the original binding and current authorization checks. Never rebind old
`cwd://` history to a new caller's directory. Fail when the original binding is
unavailable or no longer authorized; legacy Runs without a binding retain legacy
behavior, rather than gaining access from the retry process's directory.

Both host execution and Docker use this captured map. An existing server can be
reused only when its mounted bindings match; it must not acquire new mounts from
an arbitrary API request. Different local invocation maps use isolated execution
contexts and separate guest lifecycle/status files, with a stable binding identity
under the script's generated runtime directory. Concurrent callers must not stop
one another's guest or change one another's roots. Preserve shared run inspection
and route control/retry operations to the owning context.

## Inspection and mutation commands

Enable `./aide.too models`, `providers`, `tools`, and `workspace list` using the
same selected sources as execution. Listings must not start a model Run. Extend
`info` with source/config/catalog provenance, invocation directory, runtime root,
and selected catalog precedence. `workspace list` includes implicit grants, aliases,
and availability; distinguish the current invocation from an already running
context. Historical inspection reports the stored Run binding. Never print secrets.

Enable roaming `workspace add/remove` as edits to the selected authored
`toolang.toml`, never to its generated alias. With no config, create a minimal
sibling config exclusively. CLI input paths use `invocation_dir`; persist paths
relative to the authored config directory, preserve TOML comments, and serialize
mutations under an authored-config lock. Do not remove implicit workspaces.
Resident mutation commands use the same path rules and retain their destinations.

## Transactional initialization

`too init DIR` creates executable `aide.too` and an English, comment-only
`toolang.toml` explaining automatic `cwd`, relative workspaces, and the optional
catalog companion. It contains no credentials, machine-specific paths, model
selection, or default runnable. Do not generate a catalog. Print the generated
paths, useful commands, and a suggestion to ignore `.toolang/`; do not modify an
existing `.gitignore` automatically.

If either destination already exists, including a directory or dangling symlink,
refuse without modifying either. Do not add a force-overwrite option in this scope.
Stage both complete files on the destination filesystem, serialize Toolang init
processes, and use no-clobber publication. Record a durable transaction journal
under `.toolang/` before publishing; publish config before the executable. On
failure roll back only files still owned by that transaction. Preserve concurrent
external edits and report their paths instead of deleting them. On interruption,
the next init or script invocation recovers under the same lock before proceeding:
complete pairs are finalized; partial pairs are rolled back before retrying.

Two sibling files cannot be made simultaneously visible with one portable atomic
rename. The guarantee is transactional all-or-nothing behavior for Toolang callers,
with crash recovery; unrelated filesystem readers can observe the brief publication
interval. Lock/journal files may remain in disposable `.toolang/` after failure.

## Implementation touchpoints and acceptance

- Discovery/materialization: `common/layout.py`, `up/process.py`,
  `cli/toolang/routing.py`; carry source origins explicitly rather than infer them
  from generated links. Cover adjacent/ancestor discovery, Git worktrees,
  submodules, non-Git directories, script/config symlinks, nested configs, sibling
  scripts with equal stems, malformed candidates, deletion, and stale destinations.
- Paths/catalogs: `state/config.py`, `state/source.py`, `state/prepare.py`,
  `setup/config.py`, `setup/watcher.py`, and
  `plugin/catalogs/models_dev/path.py`. Verify relative paths across placements,
  per-layer bases, every precedence pair, invalid selected files, ordinary project
  `catalog.json` being ignored, and relocation/retargeting with identical bytes.
- Invocation binding: `cli/toolang/commands/script.py`,
  `cli/common/agent_server.py`, `execution/records.py`, `execution/executor/`,
  `setup/types.py`, `up/mounts.py`, and sandbox launch/control types. An offline
  scripted model must read a fixture in the invocation directory via fs and shell.
  Exercise concurrent calls from two projects, aliases/nesting, resumed history,
  retry authorization, service reuse, and equivalent host/guest mappings.
- CLI: `cli/toolang/commands/{init,workspace,agent,model_catalog,plugin}.py`,
  CLI context/routing,
  templates, and focused usage documentation. Verify effective sources, secret
  redaction, relative persistence, and no writes through materialized symlinks.
- Init: cover both collision orders, symlinks/directories, failure on each write
  and publication, concurrent initializers, process termination/recovery, preserved
  external edits, executable mode, and successful execution after deleting runtime
  state. Cover read-only source fallback and fresh bindings after copy/move.

Keep default tests offline and deterministic; use fault injection and guest-mount
contract tests rather than live model calls. Implementation must pass Ruff lint
and formatting, `ty check`, and the full offline suite. This definition PR requires
source accuracy, link validation, and `git diff --check` only.

## Tradeoffs and approval

The largest compatibility changes are abandoning hidden roaming-root policy,
rejecting authored `cwd` collisions during script invocation, and including
configuration origins in prepared bindings. Preserve legacy settings by diagnosing
conflicts, not silently migrating or deleting them. Existing workspace/workdir
plans remain applicable except for the local-script initial directory and the
explicit invocation-binding rules above.

Approval is requested for these proposed defaults, especially catalog naming and
precedence, implicit `cwd`, environment-only roaming credentials, read-only cache
fallback, and the recoverable initialization guarantee. No implementation is
approved or included by this document's submission.

The lightweight naming and path-origin choices take cues from
[uv's configuration discovery](https://docs.astral.sh/uv/concepts/configuration-files/)
and [Compose's source-relative includes](https://docs.docker.com/reference/compose-file/include/).
Toolang deliberately bounds ancestor discovery rather than adopting either tool's
whole configuration or environment model.
