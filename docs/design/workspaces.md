# Workspaces

A workspace is a named directory grant. A workdir selects a location inside a
grant; an input attachment supplies content. These have separate authority and
capture boundaries. This guide owns path/grant semantics; [Scripts](scripts.md)
owns invocation syntax and [Hosting](hosting.md) owns sandbox mounts.

## Grant construction

State merges agent-home `[workspaces]` with the agent-root policy. The root
policy wins for a shared name; agent home supplies other names. Resident agent
root equals the Toolang root; nonresident agents do not inherit its agent configuration.

| Placement | Root policy |
| --- | --- |
| Resident | `[workspaces.hak]` |
| Visiting | Matching ref table in its agent root, normally empty |
| Roaming | Flat `[workspaces]` in source-local `toolang.toml` |

Keyed agent-root tables select only the matching stable agent identity. Roaming
project configuration retains its flat workspace syntax. Paths may be anywhere on the host.
Absolute host paths are recommended in both agent-home and root configuration.
`workspace add` resolves paths at the client's procdir and writes canonical
absolute paths. Authored relative paths resolve at their declaring file.
Roaming's nearest project TOML is hardlinked at `AGENT_ROOT/config.toml`;
its origin is recorded in `AGENT_ROOT/source.json`. A selected ancestor
configuration grants no access. Home State retains a receipt of only
the selected root workspace policy, so unrelated root Setup edits do not change it.
Source discovery and process directories are distinct from execution workdir.

Setup adds the implicit `lab` workspace at `AGENT_ROOT/workspaces/<agent>/lab`,
before configured grants. It is Toolang-managed and cannot be replaced by a
configured or temporary grant. Configured and invocation grants retain stable
names and order; conflicting bindings reject instead of silently rebinding a name.
Inferred names normalize directory basenames to kebab case. Explicit names use
the validated workspace-name grammar.

Roaming uses `SOURCE_DIR/.toolang` as agent root, so its lab is shared across
P19s and selected Toolang roots. Concurrent instances may write the same files.
Guest mounts expose the same lab contents at the guest layout's implicit path.
Removing an agent preserves its lab and contents.

The caller may select workdir without adding access. Without an invocation
selection, the runtime uses the last usable workspace, with implicit `lab`
first. Script's automatic source grant and temporary CLI additions resolve
before this runtime boundary. [Scripts](scripts.md#temporary-workspaces) owns
their surface-specific defaults.

## Logical paths and authorization

`name://path` identifies a root-relative path within a named workspace and
remains portable across host/guest physical roots. A workdir uses this canonical
form. Tool paths can also be relative to workdir or absolute within an
authorized workspace; resolution produces both a physical path and its logical
workspace identity.

Resolution validates the grant, canonical path, and physical target. Traversal
and symlink resolution cannot escape the granted root. Removal operations
validate the containing directory while preserving final-link semantics.
Invocation-local caches keep preflight and execution on the same resolved path.
[Plugins](plugins.md#toolset) owns the tool path-reporting contract.

These checks protect workspace-aware tools. A shell command is interpreted by
the shell and OS; grants alone do not sandbox arbitrary commands.
[Tools](tools.md#shell) and the selected sandbox define that boundary.

## Run capture and guest mounts

Admission freezes the Run's grants, inherited by children. Source edits and
State publication cannot grant new paths to an accepted Run. Changing workdir
selects among those grants. Per-tool-call contexts capture that authority and
must not be retained on shared plugin objects.

Guest startup mounts configured workspaces and translates their paths with
`TOOLANG_CONFIG_PATCH`, a JSON object shaped like config.toml:

```json
{"workspaces":{"repo":"/root/.toolang/workspaces/hak/repo"}}
```

Source configuration and shared State retain authored host paths. The patch
contains only configuration values; origins and the prepared source revision live
in `GR/source.json`. Setup validates current grants against that launch revision
before using a fixed path correction, so revoked or rebound grants do not acquire
an old mount. Host commands apply the same runtime patch when supplied.

CLI workspace/workdir options are translated while constructing guest argv;
logical `name://` workdirs remain unchanged. Temporary grants are represented by
ordinary `--workspace` options, not patch fields or binding records. Lab needs only
its normal directory mount and the guest layout's implicit path. Mount changes
require restart. Accepted Runs retain their captured grants; temporary grants are
not written to authored configuration or published as shared authored `current`.

File inputs have another origin: Chat/script attachments use client procdir;
file chores use the Markdown directory and inline chores use agent home.
Capturing bytes grants no filesystem authority. See [file inputs](scripts.md#file-inputs)
and [client HTTP capture](api.md#client-file-inputs).

## Inspection

Configured inspection reads authored grants without preparing execution or
starting a server. It excludes implicit `lab`, temporary grants, and workdir.
`workspace list/add/remove` accept an agent name or source path and manage resident
home or project configuration. Listing includes matching ROOT overrides; adding
a duplicate name fails. ROOT bindings must be edited in ROOT configuration.
Visiting agents may use ROOT declarations and invocation grants, but reject these
editing commands before source acquisition.
Runtime inspection reports usable captured grants in that environment.
The two views answer different questions; neither changes authority.
