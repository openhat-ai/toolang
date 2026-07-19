---
description: Trigger this service when the agent should launch this stdio MCP server.
transport: stdio
target: uvx example-mcp-server
# env: API_TOKEN, ANOTHER_ENV_VAR
---

`description` is the trigger summary. Keep it short; document the service
details below.

Connection:
- `target` is one shell-like command line, for example:
  `uvx mcp-remote https://example.com/mcp`
- Use optional `env` to list required environment variable names.
- Use `headers` only with HTTP services.

Capabilities:
- Tools: ...
- Resources: ...
- Prompts: ...

Auth notes:
- ...
