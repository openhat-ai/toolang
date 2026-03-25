# Toolang Tools

This document defines the built-in runtime tool families and their loading
model.


## 1. Scope

Tools are base runtime capabilities.

They are not caps, and they are not implemented as a separate agent framework.

Tools live inside one turn execution:

- the model decides whether to call a tool
- Toolang executes the tool locally
- the tool result is fed back into the same turn
- execution truth records the call as a `tool_call` step


## 2. Tool Families

Current built-in tool families:

- `filesystem`
- `shell`
- `web_search`

Planned but not yet implemented:

- `memory_search`
- `service_use`
- `browser_use`
- `computer_use`

Each family is one stable capability with one default provider.


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


## 4. Loading

Per-agent tool configuration lives at:

- `${AGENT_HOME}/tools.toml`

Minimal shape:

```toml
[tools.filesystem]
provider = "default"

[tools.shell]
provider = "default"

[tools.web_search]
provider = "default"
```

If `tools.toml` is absent, built-in defaults are used.


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
- `web_search:default`


## 6. Runtime Contract

The runtime resolves one local tool set per turn.

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
- `tools.web_search`
- and future tool-family flags

Prompt traces may also include recorded tool calls for one run.
