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
project layers retain their flat workspace syntax. Paths may be anywhere on the host.
Absolute host paths are recommended in both agent-home and root configuration.
`workspace add` resolves paths at the client's procdir and writes canonical
absolute paths. Authored relative paths resolve at their declaring file.
Roaming project configuration is captured in `AGENT_ROOT/config.toml`; ancestor
project configuration grants no access. Home State retains a receipt of only
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
Host and guest preserve the lab identity through the captured mount binding. Removing an agent preserves
its lab and contents.

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

Guest startup captures grants before constructing execution paths. Authored
configuration and State keep canonical host paths; runtime configuration patches
carry configured guest paths and common startup records preserve both identities:

```json
{"repo":{"source":"/Volumes/projects/demo","path":"/root/.toolang/workspaces/hak/repo","temporary":false}}
```

Host records use the same shape with `path=source`. Only CLI additions marked
`temporary=true` contribute canonical source paths to State composition. Setup
intersects current canonical grants with captured bindings before selecting the
execution root. Removed, new, or rebound grants cannot gain access through a
stale patch. If the host republishes an unchanged declaration with a new
canonical symlink target, the guest retains that published source identity;
its execution mount must still match. Logical `name://` paths remain unchanged. Restart the server to
change captured mounts; no workspace mapping is read from environment variables.
Temporary grants last for the server generation and are not written to authored
configuration. [Hosting](hosting.md#docker-filesystem) owns mount paths and access.

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
