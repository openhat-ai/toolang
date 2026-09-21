<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A description language and runtime for agents.

Tool calling turned LLMs from chatbots into agents. Toolang makes agents easy to build, run, and share.

## Share agents, not setup guides

Describe an agent in a `.too` file using near-natural language. The same file
can be read, run, and shared, so others can use your agent and build on it.

## Run shared agents in seconds

Start with an agent from your team or the community. With `uvx` and a model API
key, you can try it without a permanent installation.

The examples use `too`, an alias for `toolang`. Both names provide the same CLI:

```bash
uvx --from toolang too serve https://toolang.ai/dev.too
```

Or use a GitHub shorthand:

```bash
uvx --from toolang too serve briceyan/dev
```

See [model configuration](./docs/models.md) to connect a provider or use a
local model service.

## Get started

Toolang requires Python 3.11+ and supports macOS and Linux. Windows is not
supported yet.

Install Toolang to create and run your own agents:

```bash
uv tool install toolang
```

### Create your own agent

Start from scratch, or clone a shared agent and make it your own:

```bash
too new alice
too clone briceyan/dev bob
```

### Chat with your agents

Work with an agent interactively in your terminal:

```bash
too alice chat
```

To keep the agent running as a service, start it in the foreground. Press
`Ctrl+C` to stop:

```bash
too serve alice
```

Or start and stop it in the background:

```bash
too start alice
too stop alice
```

## Go beyond chat

Give agents work without starting a conversation each time. Use
[tasks](./docs/tasks.md) for one-off jobs, chores for recurring work, and scripts
to bring agent procedures into Makefiles, GitHub Actions, and other workflows.

### Run a script

Create a starter script and explore its commands:

```bash
too init demo
too demo/aide.too info
too demo/aide.too --help
```

Run the default greeting or start an interactive chat:

```bash
too demo/aide.too
too demo/aide.too chat
```

The starter includes a writing workflow that rewrites text in the requested
tone and checks the result:

```bash
too run demo/aide.too polish tone=professional -- "Can you send the notes?"
```

Use `--model` to select another configured model. `too demo/aide.too` is
shorthand for `too run demo/aide.too`. The generated file is executable, so
`./demo/aide.too` also works.

## Common commands

```bash
# Agents
too new <agent>                       # Create a local agent
too clone <ref> <agent>               # Clone a shared agent
too serve <agent-or-ref>              # Run an agent in the foreground
too start <agent>                     # Start an agent in the background
too stop <agent>                      # Stop a running agent

# Scripts
too init <dir>                        # Create aide.too without overwriting files
too run <file.too> [runnable]         # Execute the default or a named runnable

# Source development (offline)
too parse demo/aide.too --cst --json  # Inspect the concrete syntax tree
too fmt demo/aide.too --check         # Check formatting without writing files
too fmt demo/aide.too --highlight     # Preview formatted source in color
too highlight demo/aide.too           # Highlight original source

# Inspection
too models                            # List available, allowed models
too alice models                      # Inspect models using alice's configuration
too providers                         # List available, allowed providers
too tools                             # List available, allowed tools
too alice tools --all                 # Include internal and allow-excluded tools

# Help
too --help                            # Show the main commands
too more                              # Discover additional commands
```

See [source commands](./docs/source-commands.md) for offline formatting,
parsing, and highlighting, and [model configuration](./docs/models.md) for
provider setup and catalog overrides.

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- [Repository documentation](./docs/index.md)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
