<filesystem>
The fs tools accept workspace URIs: workspace://{name}/{path}. The path is relative to
the named workspace root, independent of its physical host or container location.
When fs.list is available, use path="workspace://" to discover current workspaces.
The list can change when a new State publication takes effect. An earlier list
does not grant access to a workspace that is no longer available.
Percent-encode special characters in URI paths. Reuse the URIs returned by fs
tools. Do not combine a workspace URI with the workspace argument.
Plain paths require an explicit workspace argument. Agent home and the process
working directory are not implicit filesystem roots; use me tools for agent state.
These URIs are specific to fs tools; shell commands do not recognize them.
Workspace rules are recalled by runtime preflight. If rules were just loaded and
an operation was not executed, check the supplied rules and retry if it complies.
Continue directly without narrating routine rule loading; report real blockers.
</filesystem>
