# Toolang Tools

This document defines the built-in runtime tool families and their loading
model.


## 1. Scope

Tools are base runtime caps.

They are not a separate agent framework.

Tools live inside one run execution:

- the model decides whether to call a tool
- Toolang executes the tool locally
- the tool result is fed back into the same run
- execution truth records the call as a `tool_call` step


## 2. Tool Families

Current built-in tool families:

- `filesystem`
- `shell`
- `service_use`
- `web_search`

Planned but not yet implemented:

- `memory_search`
- `browser_use`
- `computer_use`

Each family is one stable cap with one default provider.

Built-in tool families load by default.


## 3. Default Behavior

### `filesystem`

- one tool named `filesystem`
- scoped to the current agent home
- supports:
  - `read_text`
  - `write_text`
  - `append_text`
  - `list_dir`
  - `glob`
  - `stat`
  - `mkdir`

### `shell`

- one tool named `shell`
- runs one non-interactive command
- scoped to the current agent home
- returns structured stdout, stderr, exit code, and truncation flags

### `web_search`

- one tool named `web_search`
- default provider uses local DuckDuckGo search
- returns concise structured search results

### `service_use`

- one tool named `service_use`
- default provider uses the external `mcat` CLI
- exposes visible `service` caps as callable MCP services
- loads even when no visible services exist, so the runtime caps surface
  stays stable
- service connection details come from service-cap front matter
- `description` is the trigger text loaded into the available-services prompt
  section
- static service env vars come from `${AGENT_HOME}/.env`
- supports:
  - `auth_start`
  - `auth_continue`
  - `tool_list`
  - `tool_call`
  - `resource_list`
  - `resource_list_template`
  - `resource_read`
  - `prompt_list`
  - `prompt_get`


## 4. Loading

Prompt builds should describe the current visible services and instruct the
model to use `service_use` when it needs MCP-backed caps.

For service caps, the prompt should also declare:

- visible service names
- trigger descriptions from service front matter
- concrete required env vars
- the rule that those env vars are read from `${AGENT_HOME}/.env`
- the rule that `env` entries are already final env-var names and are not
  rewritten

Example service front matter:

```md
---
transport: http
target: https://mcp.github.com/mcp
description: GitHub MCP service
env:
  - GITHUB_TOKEN
---
```

If a service requires OAuth, authenticate it with:

```bash
toolang service auth <agent> <service>
```


## 5. Providers

Toolang resolves tools by:

- `family`
- `provider`

Built-in providers ship with Toolang.

Third-party providers may register entry points under:

- `toolang.tool`

Entry-point names use:

- `<family>:<provider>`

Examples:

- `filesystem:default`
- `shell:default`
- `service_use:mcat`
- `web_search:default`


## 6. Runtime Contract

The runtime resolves one local tool set per run.

Each provider supplies:

- a stable tool definition for the model
- a local `invoke(arguments, context)` implementation

The runtime owns:

- the model/tool loop
- prompt context
- execution-step recording
- prompt-trace recording
- diagnostics and security signals


## 7. Security And Diagnostics

Tool availability should be derived from resolved tool families, not from
hardcoded UI flags.

Current runtime security signals expose:

- `tools.filesystem`
- `tools.shell`
- `tools.browser_use`
- `tools.computer_use`
- `tools.service_use`
- `tools.web_search`
- `tools.mem_search`
- `tools.file_search`

Prompt traces may also include recorded tool calls for one run.
