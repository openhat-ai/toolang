# Tool Runtime

Toolang exposes tools through the tool plugin family.

Tools execute inside normal runs and are recorded as `tool_call` steps.


## Built-In Tool Families

Current built-in tools are:

- `agent_chat`
- `filesystem`
- `shell`
- `web_search`
- `service_use`
- `agent_state`


## Agent Chat

`agent_chat` lets one agent ask configured peer Toolang agents through their
local chat API.

It provides:

- `peers`
- `send`

Peer endpoints are configured explicitly:

```toml
[tools.agent_chat]
peers = [
  { name = "bob", endpoint = "http://127.0.0.1:7002" },
]
```

`send` creates or reuses one local child agent-to-agent thread for the current
chat thread, passes that local thread id to the peer through the chat request
`peer` field, and records the peer thread id returned by the remote agent.


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


## Agent State

`agent_state` exposes structured operations for the current agent's authored
state.

It provides model-facing tools to:

- list and read tasks
- create and update tasks
- list and read chores
- create and update chores
- list, read, create, update, and delete psyches
- list, read, create, update, and delete skills
- list, read, create, update, and delete services
- list, read, create, update, and delete prompts

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
