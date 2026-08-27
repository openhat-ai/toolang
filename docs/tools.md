# Tool Runtime

Toolang exposes tools through the toolset plugin family.

Tools execute inside normal runs and are recorded as `tool_call` steps.
`AgentTool.invoke()` is asynchronous. Function-tool wrappers await native
async callables and isolate synchronous Python callables in a worker thread,
so blocking tool implementations do not stall the run event loop.


## Built-In Tool Families

Current built-in tools are:

- `fs`
- `shell`
- `web`
- `service`
- `_me`


## Filesystem

`fs` is scoped to the current agent home.

It provides structured file operations such as:

- `read`
- `write`
- `append`
- `list`
- `glob`
- `stat`
- `mkdir`
- `remove`


## Shell

`shell` runs one non-interactive command inside the current agent home.

It returns structured:

- `stdout`
- `stderr`
- `exit_code`


## Web Search

`web` returns structured search results for model use.


## Service

`service` exposes visible service caps as callable tools.

It is the bridge between:

- service cap definitions
- runtime tool execution

Service calls return structured input and output and are recorded as normal
tool-call steps.

Its leaf tools are `start_bridge`, `stop_bridge`, `init`, `start_auth`,
`complete_auth`, `list_tools`, `call_tool`, `list_resources`,
`list_resource_templates`, `read_resource`, `list_prompts`, and `get_prompt`.


## Current Agent

`_me` exposes structured operations for the current agent's authored data. The
leading underscore marks it as a Toolang-owned internal action namespace; it
still follows normal resource selection and can be denied by policy.

It provides model-facing tools to:

- list and read tasks
- create and update tasks
- list and read chores
- create and update chores
- list, read, create, update, and delete psyches
- list, read, create, update, and delete skills
- list, read, create, update, and delete services
- list, read, create, update, and delete prompts

Leaf names use verb-first forms such as `list_tasks`, `get_task`,
`create_skill`, and `delete_prompt`.

Task and chore writes reuse the same Markdown document models, id allocation,
RRULE validation, and archive placement rules as the CLI and jobs API.
Psyche, skill, service, and prompt writes reuse the same authored cap file
layout and validation rules as the CLI and cap API.


## Runtime Rule

Tools do not own the model loop.

Toolang runtime owns:

- when tools are available
- when a tool is executed
- how tool output re-enters the run
- how tool calls are recorded and exposed
- the default human-readable summary for each tool-call lifecycle state

Summary generation receives the tool family, leaf name, and supplied arguments
in the tool definition's parameter order. The default summary combines only
the leaf name and first supplied argument; it does not display the family. Its
running form is `Executing NAME ARG ...`; its succeeded and failed forms are
`Executed NAME ARG` and `Failed NAME ARG`. The canceled form is
`Canceled NAME ARG`. Toolang normalizes and bounds argument previews and
redacts sensitive parameter names or schemas before the summary enters
execution events. The running summary is stored in `ToolStepGiven.summary`;
the terminal summary uses the same key in `ToolStepNoted`.

Plugin-defined summary templates are not part of the current tool contract.
