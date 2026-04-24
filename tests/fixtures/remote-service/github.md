---
transport: http
target: https://mcp.github.com/mcp
description: GitHub MCP server
headers:
  Authorization: Bearer $GITHUB_TOKEN
---

Use this service when the agent needs to inspect repositories and pull requests.
