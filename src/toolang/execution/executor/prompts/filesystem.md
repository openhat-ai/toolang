<filesystem>
The fs tools accept workspace URIs: workspace://{name}/{path}. The path is relative to
the named workspace root, independent of its physical host or container location.
When fs.list is available, use path="workspace://" to discover current workspaces.
When the caller supplies a cwd, workspace://. (also workspace://./) addresses
that Run's fixed location; workspace://./file addresses a file there. This is an
alias, not an additional grant. Named locations return the real workspace URI;
an explicitly granted temporary cwd returns workspace://./ paths. The namespace
listing and context identify cwd by workspace://./, so remapping a named
workspace cannot redirect that fixed location. Child runs inherit it. Shell cd does
not change it, and an unavailable named cwd never becomes a temporary grant.
The list can change when a new State publication takes effect. An earlier list
does not grant access to a workspace that is no longer available.
Percent-encode special characters in URI paths. Reuse the URIs returned by fs
tools. Do not combine a workspace URI with the workspace argument.
Plain paths require an explicit workspace argument. Agent home and the process
working directory are not implicit filesystem roots; use me tools for agent state.
These URIs are specific to fs tools; shell commands do not recognize them.
Workspace rules are recalled by runtime preflight. If an operation reports
"operation not executed; retry required", follow the recalled rules and retry.
</filesystem>
