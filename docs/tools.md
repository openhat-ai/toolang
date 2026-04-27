# Tool Runtime

Toolang exposes tools through the tool plugin family.

Tools execute inside normal runs and are recorded as `tool_call` steps.


## Built-In Tool Families

Current built-in tools are:

- `filesystem`
- `shell`
- `web_search`
- `service_use`
- `jobs`


## Filesystem

`filesystem` is scoped to the current agent home.

It provides structured file operations such as:

- read
- write
- append
- list
- stat
- mkdir


## Shell

`shell` runs one non-interactive command inside the current agent home.

It returns structured:

- `stdout`
- `stderr`
- `exit_code`


## Web Search

`web_search` returns structured search results for model use.


## Service Use

`service_use` exposes visible service caps as callable tools.

It is the bridge between:

- service cap definitions
- runtime tool execution

Service calls return structured input and output and are recorded as normal
tool-call steps.


## Jobs

`jobs` exposes structured task and chore operations for the current agent.

It provides model-facing tools to:

- list and read tasks
- create and update tasks
- list and read chores
- create and update chores

Task and chore writes reuse the same Markdown document models, id allocation,
RRULE validation, and archive placement rules as the CLI and jobs API.


## Runtime Rule

Tools do not own the model loop.

Toolang runtime owns:

- when tools are available
- when a tool is executed
- how tool output re-enters the run
- how tool calls are recorded and exposed
