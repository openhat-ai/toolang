# Toolang Tools

This document defines the tool plugin family and its loading model.


## 1. Scope

Tools are base runtime caps.

Tools are a plugin family.

They are not a separate agent framework.

Tools live inside one run execution:

- the model decides whether to call a tool
- Toolang executes the tool locally
- the tool result is fed back into the same run
- execution truth records the call as a `tool_call` step


## 2. Tool Families

Current first-party tool families:

- `filesystem`
- `shell`
- `service_use`
- `web_search`

Planned but not yet implemented:

- `memory_search`
- `browser_use`
- `computer_use`

Each family is one stable cap with one default provider.

Toolang may ship first-party default providers for these families.


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
- pending OAuth results may include runtime callback metadata when the agent
  has a public API endpoint
- supports:
  - `auth`
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

Tool providers are loaded through the tool plugin family.

Rules:

- first-party default providers may be enabled by default
- external tool plugins may be installed as plugin packages
- config selects which providers are active

For service caps, the prompt should also declare:

- visible service names
- trigger descriptions from service front matter
- concrete required env vars
- the rule that those env vars are read from `${AGENT_HOME}/.env`
- the rule that `env` entries are already final env-var names and are not
  rewritten
- the OAuth sequencing rule that callback handling must be armed before the
  user opens an authorization URL
- the rule that Toolang only relays opaque callback metadata returned by the
  provider and does not parse provider-specific callback payloads or auth state

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
toolang service auth <agent> <service> [--complete]
```

For runtime-driven OAuth, use this sequence:

- call `service_use` with `action=auth` first
- if the result is pending, send the authorization URL to the user
- immediately after sending the URL, call `service_use` again with
  `action=auth` and `complete=true`
- do not wait for a follow-up user message before issuing the blocking
  `complete=true` call
- the `complete=true` `auth` call is the explicit blocking wait for callback
  delivery and token exchange
- if that blocking call returns `status=complete`, retry the original service
  action
- if runtime callback relay is unavailable, `service_use` now fails fast instead
  of returning a local loopback callback URL; use `toolang service auth ...
  --complete` outside the agent in that case

See [service-auth.md](./service-auth.md) for the Toolang-side callback
relay contract and the integrated `mcat` 2.0 flow.


## 5. Providers

Toolang resolves tools by:

- `family`
- `provider`

First-party default providers may ship with Toolang.

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

`toolang.plugins` owns generic plugin discovery and loading.

`toolang.tools` owns the tool family contract and first-party tool plugins.


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
