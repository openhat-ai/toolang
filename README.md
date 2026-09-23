<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A language and runtime for agents and humans.

The language expresses agent loops with fine-grained control using a small subset of natural language — readable by humans and agents, precise enough for the runtime to execute.

## Getting started

Toolang runs on **macOS** and **Linux** with **Python 3.11+**. **Windows** support is coming soon.

Install Toolang with `uv` or `pip`; the main command is `toolang`, with `too` as a shorter alias:

```bash
uv tool install toolang  # or: pip install toolang
too --version            # same as: toolang --version
```

Toolang supports **Anthropic**, **DeepSeek**, **Google**, **OpenAI** and **OpenRouter** out of the box, along with local models through **Ollama** and **llama.cpp**. To inspect all configured providers and list available models, run:

```bash
too providers --all
too models
```

To load additional providers and models, replace the bundled catalog with the full [models.dev](https://models.dev/) catalog:

```bash
curl -fsSL https://models.dev/catalog.json -o ~/.toolang/catalog.json
```

## Remote agents

Run a shared agent locally by referencing its URL or GitHub shorthand. Toolang fetches the definition and starts a terminal chat using your model configuration:

```bash
too https://toolang.ai/dev.too chat
too briceyan/dev chat
```

In remote-agent chat mode, the agent responds only to your messages; it does not start tasks or chores on its own.

## Local agents

Create an agent from scratch, or clone a shared one as a starting point:

```bash
too new NAME
too clone https://toolang.ai/dev.too NAME
```

Once created, the agent is ready for a conversation:

```bash
too NAME chat
```

You can then configure the agent's capabilities and add tasks or recurring chores. Use the CLI help to see the available commands:

```bash
too --help
```

## Scripts

Toolang also runs agents as scripts. Here is hello world in Toolang:

```bash
cat > hello_world.too <<'EOF'
agic():
  Say hello to the world.
EOF

too hello_world.too
```

Use `too init` to generate `aide.too` as a starting point. Read the source or use `--help` to see its available runnables:

```bash
too init DIR
too DIR/aide.too --help
```

You can rename the file or modify the code to suit your needs, then call it from Makefiles, CI jobs, or other scripts to add agent capabilities to existing automation.

To execute a runnable, pass its name and the arguments defined by its signature. If you omit the name, Toolang calls the entry runnable, as in the hello-world script above:

```bash
too DIR/aide.too polish tone=professional -- "Can you send the notes?"
```

The same script can also run interactively:

```bash
too DIR/aide.too chat
```

## Language

The [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang) repository provides the grammar and parser packages for Python, JavaScript, and Rust. See the [syntax reference](https://toolang.ai/reference/toolang-grammar) for the full language syntax.

See [examples](./examples) for runnable Toolang programs:

| Example | Description |
| --- | --- |
| [`hello-world.too`](./examples/hello-world.too) | Hello world in Toolang. |
| [`signatures.too`](./examples/signatures.too) | Agic signature forms, from shorthand to fully typed. |
| [`structured-output.too`](./examples/structured-output.too) | Define a struct and use it for structured output. |
| [`caps.too`](./examples/caps.too) | Define and use caps in agics. |
| [`hands.too`](./examples/hands.too) | Delegate work to multiple hands. |
| [`handoffs.too`](./examples/handoffs.too) | Transfer work to one of several specialist handoffs. |
| [`minimal.too`](./examples/minimal.too) | A minimal agent. |
| [`developer.too`](./examples/developer.too) | Define a complete coding agent with reusable caps. |

## Common commands

Use these commands to run and manage agents. Add `--help` to any command for its usage and options.

```bash
too new <agent>                       # Create a local agent
too clone <ref> <agent>               # Clone an agent definition
too serve <ref>                       # Run an agent service in the foreground
too start <agent>                     # Start a local agent service in the background
too stop <agent>                      # Stop a running agent service

too init <dir>                        # Create aide.too in a directory
too [run] <script> [runnable]         # Execute a script; run is optional

too caps                              # List available capabilities
too tools                             # List available tools
too models                            # List available models
too providers                         # List available providers

too --help                            # Show common commands
too more                              # Show additional commands
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
