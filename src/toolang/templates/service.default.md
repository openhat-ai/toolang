---
description: Trigger this service when the agent needs this remote MCP server.
transport: http
target: https://example.com/mcp
# headers:
#   Authorization: Bearer $API_TOKEN
---

`description` is the trigger summary. Keep it short; document the service
details below.

Connection:
- `target` is the remote MCP URL.
- Use optional `headers` for HTTP auth.
- Header values like `$API_TOKEN` declare required environment variables.

Capabilities:
- Tools: ...
- Resources: ...
- Prompts: ...

Auth notes:
- ...
