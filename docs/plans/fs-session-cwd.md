# Session cwd for filesystem tools

## Goal and scope

Local Chat captures its startup directory and supplies it with each Root Run.
`workspace://.` and `workspace://./` address that location; descendants use
`workspace://./path`. No process chdir, shell changes, remote mounts, `/cd`
command, runspace, or new State publication mechanism is included.

## Contract

- Resolve the caller's directory once per session. If it is inside a registered
  workspace, bind that workspace name and relative path without another grant.
  Otherwise explicitly grant only the startup directory as a temporary root.
  Never search for a repository root or fall back to agent home.
- Reuse `ToolPath` to represent the captured location. Persist it on the root
  run control; children and execute inherit it. State and config are unchanged.
  An absent field means no cwd, including existing records.
- `.` is a reserved alias, not an ordinary configurable workspace name.
  Registered locations return canonical named URIs and use that workspace's
  rule identity. A temporary root has no registered name and returns `workspace://./`.
- Each fs/honor Step combines its captured State grants with the recorded
  temporary root. A named cwd cannot become temporary after removal/remapping;
  refuse the alias if its workspace no longer resolves to the recorded location.
  New State publications do not rename or move the captured cwd.
  Later Runs in the same session retain that binding; removal cannot create a
  temporary grant on the next request either. An unavailable cwd blocks its fs
  operations, not unrelated work in the session.
- Namespace listing reports the canonical cwd URI when present. Relative alias
  paths obey the real workspace boundary; a temporary root cannot escape itself.
  Rules retain existing path-aware preflight, recall, and retry behavior.
- Retry restores the recorded location; temporary grants require the local
  caller to supply the same directory again. Named grants keep existing retry
  revalidation. Rerun uses the new caller's supplied location, never an implicit
  historical grant. Remote clients reject local cwd inputs; HTTP schemas do not
  accept them. Model-triggered children cannot introduce grants.

## Implementation and acceptance

- Base workspace codec and fs preparation: alias resolution and canonical output.
- Local Chat, caller requests, executor bindings, root control codec/store:
  explicit input, persistence, inheritance, retry, and rerun.
- Tool context and runtime honor: one shared grant calculation; shell unchanged.
- Protocol/context: explain the alias and expose its virtual location only.
- Tests: root/subdirectory aliases, temporary roots, absent cwd, traversal and
  symlinks, rule identity, removal/remapping, child isolation, restart/replay,
  retry authorization, remote rejection, and local Chat capture. Run the full
  offline verification suite.

## Risks

Cwd grants are tool-level authorization, not an OS sandbox. Absolute locations
are local execution facts; forwarding them to another host is unsupported.
The new control field is optional and does not change existing record meaning.
