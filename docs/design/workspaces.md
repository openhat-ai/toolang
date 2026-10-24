# Workspaces

A workspace is a named directory grant. A workdir selects a location inside a
grant; an input attachment supplies content. These have separate authority and
capture boundaries. This guide owns path/grant semantics; [Scripts](scripts.md)
owns invocation syntax and [Hosting](hosting.md) owns sandbox mounts.

## Grant construction

State reads HOME `[workspaces]` and matching ROOT `[workspaces.KEY]` declarations.
KEY is the resident agent name, canonical visiting ref/URL, or canonical roaming
source path. ROOT wins for a shared workspace name; HOME supplies the other names.
Absolute host paths are recommended in both HOME and ROOT configuration.
`workspace add` resolves paths at the client's procdir and writes canonical
absolute paths. Authored relative paths resolve at their declaring file.
Roaming source-local declarations are captured in HOME; ancestor project
configuration grants no access. Home State captures only the selected
ROOT workspace policy, so unrelated ROOT Setup edits do not change its revision.
Source discovery and process directories are distinct from execution workdir.

Setup adds the implicit `lab` workspace at `PLACE/workspaces/<agent>/lab`,
before configured grants. It is runtime-owned and cannot be replaced by a
configured or temporary grant. Configured and invocation grants retain stable
names and order; conflicting bindings reject instead of silently rebinding a name.
Inferred names normalize directory basenames to kebab case. Explicit names use
the validated workspace-name grammar.

Host and guest use the same place-relative lab path. Removing an agent preserves
its lab and contents. The old `HOME/.lab` path is ignored without migration.

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

Guest startup captures host paths and mounts workspaces under
`PLACE/workspaces/<agent>/`, separately from HOME and its read-only input
overlays. Every grant uses its unique name, including `lab` and optional source
directory grants; nested and alias sources keep their separate named guest paths.
Each guest has ROOT = PLACE, defaulting to `/root/.toolang`, and HOME at
`ROOT/agents/NAME`. Source-directory access is optional and does not determine HOME.
Hosting generates `config.toml` under the address's `docker/` stage and binds it
read-only at guest ROOT. Its matching `[workspaces.KEY]` contains effective guest
paths, overriding the whole-mounted HOME configuration. Authored host files remain
unchanged. Runtime grant identity normalizes captured guest paths back to host
sources for frozen Run authority and client compatibility. Logical `name://` paths
work in both environments.
Runtime resolution uses that mapping rather than probing client directories
again. A new or remapped workspace needs a compatible mount; restart a guest or
server to change its captured grants. Attaching clients must request compatible
bindings. Temporary grants last for the server generation and are not written
to configuration.

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
