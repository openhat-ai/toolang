<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A description language and runtime for agents.

Tool calling turned LLMs from chatbots into agents. Toolang makes agents easy to build, run, and share.

## Try Now

Start with a shared agent, no installation required:

```bash
uvx toolang run https://toolang.ai/dev.too
```

Or use a GitHub shorthand:

```bash
uvx toolang run briceyan/dev
```

## Get Started

Install Toolang to build and run your own agents:

```bash
uv tool install toolang
```

Create a local agent:

```bash
toolang new alice
```

Or clone a shared agent:

```bash
toolang clone briceyan/dev bob
```

Extend your agents with caps — composable agent primitives:

```bash
caps alice skill add briceyan/codebase-navigation
caps alice psyche add briceyan/senior-engineer
caps alice service add briceyan/github
```

Caps are typically Markdown files. To create your own cap, start one locally:

```bash
caps alice skill new reviewer
```

Run in the foreground to watch the logs. Press `Ctrl+C` to stop. Use `PY_LOG` for more detail:

```bash
toolang run alice
PY_LOG=debug toolang run alice
```

Or start it in the background:

```bash
toolang start alice
```

In either case, open the printed WebUI link to connect to the agent:

```text
Started agent alice: https://too.run/7001
```

To stop a background agent:

```bash
toolang stop alice
```

## Common Commands

```bash
# Agents
toolang new <agent>                  # Create a local agent
toolang clone <ref> <agent>          # Clone a shared agent
toolang run <agent-or-ref>           # Run an agent in the foreground
toolang start <agent>                # Start an agent in the background
toolang stop <agent>                 # Stop a running agent

# Runtime
toolang model list                   # List available models
toolang tool list                    # List available tools

# Caps
caps [agent] psyche add <ref>        # Add a psyche
caps [agent] skill add <ref>         # Add a skill
caps [agent] service add <ref>       # Add an MCP server
caps [agent] prompt add <ref>        # Add a slash command
caps [agent] skill list              # List skills
caps [agent] list                    # List all caps
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
