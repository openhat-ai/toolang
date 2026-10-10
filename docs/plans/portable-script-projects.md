# Portable Script Projects

Status: Approved in #640 and subsequent CLI design review; implemented in #641.
This document records the final design. [Script Projects](../script-projects.md)
is the user guide; [Compact entry labels](chat-entry-label.md) specifies runnable
identity and display in more detail.

## Goal and scope

Keep `.too`, `toolang.toml`, and optional `toolang.catalog.json` portable and
versionable. Generated `srcdir/.toolang/` is disposable while the runtime is
stopped. Moving a project or invoking it from another directory must preserve
its authored configuration and access rules, given equivalent installed tools,
credentials, and services.

Scope includes configuration discovery, temporary workspaces, init, runnable
selection/help, inspection across placements, and client file inputs. It does
not add a runtime manager, a read-only cache fallback, ancestor job discovery,
roaming job commands, or file includes in agic/flow bodies. Existing persisted
field names and identities are not migrated merely to align terminology.

## Directories and inputs

| Term | Meaning |
| --- | --- |
| `procdir` | Client process directory at invocation. |
| `srcdir` | Directory of the real `.too` file after resolving source symlinks. |
| `workspace` | A named directory grant. |
| `workdir` | A location within a workspace, such as `repo://src`. |

| Relative value | Base |
| --- | --- |
| Explicit CLI path or catalog environment override | `procdir`, resolved before handoff. |
| Chat/script `@file`, including named and stdin input | Client `procdir`; piped input has no source filename. |
| Task/chore `@file` | Authored file directory; existing agent-home fallback for fileless jobs. |
| Workspace/catalog path in configuration | Directory of that configuration's discovered pathname, before merging. |
| Runtime filesystem/shell path | Run `workdir`. |

Changing workdir never changes procdir or attachment origins. Absolute paths and
home expansion retain their meanings. A config symlink uses its discovered
parent, whereas a source symlink determines srcdir from its real target. Docker
guest roots are guest paths; service commands and remote cap references are not
host paths to rebase.

Hosted clients discover expanded file references against one State revision,
read them from procdir, and send typed attachment Parts with that revision.
Acceptance rejects changed revisions or missing attachments; raw HTTP requests
never gain a server-file resolver. Preserve Content escaping, fences, prompt
provenance, and one-pass inclusion. Accepted input stays in the existing Run record.

## Configuration and ownership

For local `.too` sources, discover `toolang.toml` and `toolang.catalog.json`
independently from srcdir through the nearest Git working-tree root, inclusive.
A nested repository, submodule, or linked worktree has its own boundary. Without
Git, search srcdir only. Ignore Git relocation environment overrides and reject
a broken nearest Git marker rather than crossing it. Only directories containing
companion files contribute layers. Procdir is never a discovery input.

| Placement | Configuration and catalog sources | Persistent workspace edits |
| --- | --- | --- |
| Resident | Existing shared root and agent home; `config.toml` and `catalog.json`. | Agent-home config. |
| Roaming | Git-bounded companions; generated runtime at `srcdir/.toolang/`. | Source-local `toolang.toml`. |
| Visiting | Existing downloaded context; no search above its cache. | Unsupported; use invocation grants. |

Ownership determines inheritance:

- Agent-owned workspaces, program, jobs, schedules, and execution state belong
  to agent home or srcdir. Shared workspace declarations are ignored before path
  resolution and validation; they cannot grant or block a descendant's access.
- Shared catalogs, reusable caps, plugins, defaults, and policies retain their
  existing root/home ownership. Roaming ancestor TOML contributes supported
  shared settings; this does not discover ancestor cap/job directories.
- Invocation grants are runtime inputs, never authored configuration. Children
  inherit the accepted Run's bindings; saved Runs retain their captured bindings.

TOML layers apply outer to inner using each field's existing resolver:
`default.model` composes model overrides, `compact.model` replaces its model,
allow categories replace their query lists, and a sandbox driver change clears
an inherited target unless a new target is supplied. Plugin/cap mappings merge
recursively; nearer scalar/list values replace earlier values. Do not flatten
layers before applying those semantics. Workspace declarations retain order.

Catalogs never merge. Precedence is explicit CLI override, environment override,
then authored layers nearest first, then bundled data. Within a layer,
`plugin.model_catalog.models_dev.path` precedes its companion. A nearer companion
wins over an outer path. Generic project `catalog.json` is not auto-discovered
for roaming. Invalid TOML fails with its origin; an invalid selected catalog
fails without fallback. Unselected outer catalog contents are not loaded.

Roaming uses process/container credentials, with no project/generated dotenv
loading or implicit resident `~/.toolang` inheritance. Resident and visiting
credential behavior remains unchanged.

## Workspace selection

[Workspace CLI options](workspace-cli-options.md) defines the current command
option scope, source-directory fallback, and invocation workdir precedence.

Build grants in order: implicit `lab`, configured entries, invocation additions.
Without an invocation selection, the last usable workspace wins. Reject more
than one `-d`/`--workdir`, including mixed aliases. Invocation workdir takes
precedence over thread history for that call.

Paths resolve from procdir and must name existing directories. Split at the first
`=`: a nonempty left side supplies a name; an empty left side requests inference.
Preserve the entire right side, including later `=` characters. A leading `=`
always means a path. For workdir only, `NAME://...` selects a URI; a bare name
always means a path. Never guess from filesystem/workspace existence.

Infer names from complete directory basenames, normalized to kebab case:
`project.v2` becomes `project-v2`. For `.`/`..`, use the resolved basename; automatic
srcdir already names the real source directory. Reject unnameable paths and name
collisions with configured, implicit, or invocation grants, except for the
automatic-source reuse defined by the workspace CLI plan. The user supplies a
different name; never invent suffixes. Different names may
alias one root, with one guest mount. Preserve nested-root semantics.

```sh
./aide.too whats_for                          # Source workspace
./module1/file.too whats_for -d module2       # Relative to procdir
./aide.too whats_for -w another_dir -w .      # Select the last addition
./aide.too whats_for -d project=../project    # Explicit name
./aide.too whats_for -d repo://src            # Existing grant
./aide.too whats_for -d =./foo=bar            # Infer a name for a path containing =
```

Resolve CLI choices into concrete grants and a canonical URI before runtime
startup/Run acceptance. Validate directory existence, names, collisions, and
workspace containment. `lab://` may be selected before its directory is created.
Do not change process directory or persist temporary grants as configuration.

An attached server keeps its captured grants. URI selection can use them without
adding a client-local path. Repeated explicit bindings must match the server's
bindings; reject changes with instructions to stop it first. No silent remounts.
Snapshots and guest mounts retain host-resolved paths; rebased guest config
snapshots require a restart to refresh. Recorded Runs are never rebound to a
later caller's directories.

## Generated state and inspection

Keep generated runtime names `agent.too`, `config.toml`, and `catalog.json`.
A generated TOML projection retains ordered authored origins; it is not an
independent override. Synchronize owned files/links and clear stale owned entries
when sources disappear. Reject unowned entries instead of replacing them.
Project relocation must not reuse stale path bindings. Deleting generated state
loses local history, caches, logs, and lab output, not authored configuration.

`info`, `models`, `providers`, and `tools` use the selected placement's layout.
`info` shows configuration/catalog origins and runtime workspace information.
`workspace list` follows the authored-configuration contract in
[Workspace CLI options](workspace-cli-options.md). Preserve targetless global
inspection. Inspection does not start a model Run or expose credentials.
Persistent workspace edits preserve comments, use paths relative to the authored
config, and never modify shared ancestors or generated projections.

## Init and runnable help

`too init DIR` creates executable `aide.too` and a comment-only `toolang.toml`.
The template contains only named helpers: `issue`, `fix`, `review`, `whats_for`,
`whats_new`, and `update_i18n`; nothing executes by default. No catalog, secrets,
or machine-specific paths are generated. The config comment is:
`# Toolang settings. Paths are relative to this file.`

Preflight both destinations, including directories and dangling symlinks. Report
all conflicts in `aide.too, toolang.toml` order:
`Error: aborted to avoid overwriting: aide.too, toolang.toml` (list only conflicts).
Then exclusively create TOML followed by the script. A later collision/write
failure aborts without rollback or overwriting. Success lists the two filenames,
suggests ignoring `.toolang/`, and shows one `too PATH/aide.too --help` command.
Do not edit `.gitignore` automatically.

Use `too run FILE [RUNNABLE] [ARGUMENTS]`; omit `run` when the runnable name does
not collide with a Toolang command. Add `--help` after FILE or RUNNABLE. Static
run help says `Execute a .too file.` and `Runnable arguments`.

An omitted selector or `_` selects the unnamed entry; `agic:_`/`flow:_` also
check its kind, and script dispatch accepts `runnable:_`. There is no fallback
to a named `main`. Reject the former `<entry>`/implicit `entry` aliases; a genuinely
named `entry` still works. Script selectors reject `<entry:N>`, while canonical
lined identities remain in runtime records. `-` remains stdin: `too file.too _ -`.
Source grammar is unchanged; `_` is not a legal authored runnable name.

Use shared usage/help formatting. File help has name/kind/description columns,
without a redundant Arguments group. List unnamed `_` first, then named agics,
then named flows, preserving source order within each kind. Its description
starts with `<entry:LINE>`, followed by its authored comment. Chat displays
`agic:_`/`flow:_`; stored identities are unchanged.

Script options are ordered `-q`, `-o <FILE>`, `--model <MODEL>`, `-w [NAME=]<DIR>`,
`-d [NAME=]<DIR>|<URI>`, `--sandbox <SANDBOX>`, `--allow <RESOURCE>=<QUERY>`,
`--limit <LIMIT>=<VALUE>`, `--dev [PATH]`, `-h`.
Keep descriptions short; help never executes or reads stdin.

## Acceptance and implementation boundaries

| Area | Required coverage | Main tests |
| --- | --- | --- |
| Discovery/ownership | Different procdirs, source/config symlinks, nested Git/worktrees/submodules, non-Git and broken candidates, ignored shared workspaces. | `test_config_sources.py` |
| Configuration | Three-layer field semantics, relative origins, catalog precedence/removal, relocation, dotenv isolation. | `test_config_sources.py`, setup and sandbox tests |
| Grants | All placements, name inference/first `=`, collisions, order, URI containment, auto-source suppression, immutable revisions, nested/alias mounts. | `test_workspace_options.py`, `test_agent_server.py` |
| Input | Client vs source/server file origins, prompt expansion, missing attachments, State changes, local/remote parity. | `test_calls.py`, `test_remote_runs.py`, script/Chat integration tests |
| Init/help | Preflight and post-check conflicts, concurrent init, partial failures, selector/kind errors, help order and no execution. | `test_script_entry.py`, `test_script_command.py`, `test_cli_help.py` |
| Runtime | Guest path handoff, tmux placement, concurrent first startup, server compatibility, inspection without rebinding. | `test_sandbox.py`, `test_tmux_launcher.py`, `test_store_schema.py`, CLI integration tests |

Configuration ownership and path resolution belong at loading/CLI boundaries.
Setup keeps field-specific policy merging; State captures grants in its revision;
execution/API transports resolved input and immutable bindings; sandbox code
mounts those bindings. Keep core schemas independent of CLI/runtime orchestration.

Verify with Ruff lint/format, `ty check`, the full offline suite, and
`git diff --check`. Live-provider checks are opt-in. No open design questions.
