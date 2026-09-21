<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A description language and runtime for agents.

Tool calling turned LLMs from chatbots into agents. Toolang makes agents easy to build, run, and share.

Use `too`, an alias for `toolang`, for all commands below. Both names provide
the same CLI.

Toolang requires Python 3.11+ and supports macOS and Linux. Windows is not
supported yet.

## Try Now

Run a shared agent without installing Toolang permanently:

```bash
uvx --from toolang too serve https://toolang.ai/dev.too
```

Or use a GitHub shorthand:

```bash
uvx --from toolang too serve briceyan/dev
```

Model calls require a configured provider API key or a running local model
service. See [model configuration](./docs/models.md) for setup and selection.

## Get Started

Install Toolang:

```bash
uv tool install toolang
```

### Run a Script

Create a starter script and inspect its available runnables:

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

The generated file includes a rewrite agic with a named parameter and a flow
that rewrites text and checks the result:

```bash
too run demo/aide.too polish tone=professional -- "Can you send the notes?"
```

Use `--model` to select another configured model. `too demo/aide.too` is
shorthand for `too run demo/aide.too`. The generated file is executable, so
`./demo/aide.too` also works.

### Run an Agent

Create a local agent or clone a shared one:

```bash
too new alice
too clone briceyan/dev bob
```

Run an agent in the foreground. Press `Ctrl+C` to stop:

```bash
too serve alice
```

Or start and stop it in the background:

```bash
too start alice
too stop alice
```

## Common Commands

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
