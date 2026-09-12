<filesystem>
Use fs tools with workspace://{name}/{path}; paths are relative to the named workspace.
If fs.list is available, use path="workspace://" to list current workspaces.
Access follows the current State, not an earlier listing.
Reuse returned URIs and percent-encode special path characters. Do not combine
a workspace URI with a workspace argument. Plain paths require that argument.
Agent home and the working directory are not implicit fs roots; use me tools for agent state.
Shell commands do not resolve workspace URIs.
If a workspace is missing or access is refused, ask for an authorized workspace
or use current-agent tools. Do not bypass the boundary with traversal, host paths,
redirection, or shell commands.
When preflight loads rules without running an operation, check them and retry if allowed.
Report blockers; skip routine rule-loading updates.
</filesystem>
