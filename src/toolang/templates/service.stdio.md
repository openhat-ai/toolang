---
description: Local stdio-backed service.
transport: stdio
command: uvx
args:
  - example-mcp-server
port: 6010
# env:
#   - API_TOKEN
---

Use this service when the agent should launch a local stdio MCP server through a bridge.

Document the command, required env vars, and the exposed capabilities.
