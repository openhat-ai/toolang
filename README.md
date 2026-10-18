<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A language and runtime for agents and humans.

The language expresses agent loops with fine-grained control using a constrained form of natural language — readable by both humans and agents, yet precise enough for the runtime to execute.

## Install

Toolang runs on **macOS** and **Linux** with **Python 3.11+**. **Windows** has not been tested yet.

Install Toolang with `uv` or `pip`; the main command is `toolang`, with `too` as a shorter alias:

```bash
uv tool install toolang     # Or: pip install toolang
too --version               # Same as: toolang --version
too --help                  # Show common commands
```

## Models

Toolang works out of the box if you already have an API key for a supported model provider.

To see which models are ready to use:

```bash
too models                  # List models ready to use
```

To use another provider, set its API key in your shell:

```bash
too providers --all         # List all supported providers
```

Toolang maintains a model catalog based on [models.dev](https://models.dev/) and discovers local models served by **Ollama** and **llama.cpp**. Updated and full catalogs are available at [openhat-ai/models](https://github.com/openhat-ai/models).

## Shared agents

Run an agent directly from a URL or GitHub shorthand:

```bash
too https://toolang.ai/lex.too chat  # Run a .too file from a URL
too gh:briceyan/gus chat             # github.com/briceyan/agents/blob/main/gus.too
```

Lex helps you practice English. Gus is a coding agent that also contributes to Toolang.

Toolang downloads the `.too` source and its declared dependencies, then runs the agent locally.

## Local agents

Create an agent from scratch:

```bash
too new alice
```

Or clone a local or shared agent:

```bash
too clone alice bob
too clone https://toolang.ai/lex.too lex
too clone gh:briceyan/gus gus
```

Chat with your agent:

```bash
too alice chat
```

## Scripts

Toolang supports standalone `.too` scripts. Here's a simple example that translates text into Chinese:

```too
agic:
  Translate the following text into Chinese:
  {{_}}
```

Save it as `a.too` and run it with some text:

```bash
too a.too -- hello world
```

Use `too init` to create a starter script that you can rename and customize:

```bash
too init <project-dir>
too <project-dir>/aid.too --help
```

## Language

Toolang models the agent loop as a fundamental construct called `agic`, analogous to a function in traditional programming languages. Agics can be called, reused, and composed to build complex behavior from smaller, modular units.

Toolang also introduces `flow` to organize agics and other operations into explicit execution steps, expressing plans and strategies for solving complex tasks as structured, reusable workflows.

Agics, flows, and other language constructs are designed to be read and written by both humans and agents, establishing a common language for human–agent collaboration.

The following [examples](https://github.com/openhat-ai/toolang/tree/main/examples) showcase Toolang’s key features:

| Example | Description |
| --- | --- |
| [`hello-world.too`](https://github.com/openhat-ai/toolang/blob/main/examples/hello-world.too) | Hello world in Toolang. |
| [`signatures.too`](https://github.com/openhat-ai/toolang/blob/main/examples/signatures.too) | Agic signature forms, from shorthand to fully typed. |
| [`structured-output.too`](https://github.com/openhat-ai/toolang/blob/main/examples/structured-output.too) | Define a struct and use it for structured output. |
| [`caps.too`](https://github.com/openhat-ai/toolang/blob/main/examples/caps.too) | Define and use caps in agics. |
| [`hands.too`](https://github.com/openhat-ai/toolang/blob/main/examples/hands.too) | Delegate work to specialized runnables. |
| [`handoffs.too`](https://github.com/openhat-ai/toolang/blob/main/examples/handoffs.too) | Hand off tasks between runnables. |
| [`minimal.too`](https://github.com/openhat-ai/toolang/blob/main/examples/minimal.too) | An agent in 14 characters. |
| [`developer.too`](https://github.com/openhat-ai/toolang/blob/main/examples/developer.too) | Define a complete coding agent with reusable caps. |

For the full language syntax, see the [syntax reference](https://toolang.ai/reference/toolang-grammar).

The Tree-sitter grammar and parser packages for Python, JavaScript, and Rust are available in [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang).

## Common commands

Below are some common commands. Run `too --help` for usage details, or `too more` for additional commands.

```bash
too new <agent>                # Create an agent
too clone <ref> <agent>        # Clone an agent
too <agent> info               # Show agent information
too <agent> home               # Open a shell in the agent home
too <agent> start              # Start an agent in the background
too <agent> stop               # Stop an agent
too <agent> chat               # Chat directly with an agent

too init <dir>                 # Initialize Toolang in a directory
too run <file.too> [runnable]  # Execute a .too file

too hub start                  # Start the hub in the background
too talk                       # List conversations
too talk <convo>               # Open a conversation by ID
too talk <agent>               # Open a conversation with an agent
too talk all                   # Open a conversation with the team

too caps                       # List available caps
too tools                      # List available tools
too models                     # List available models
too providers                  # List available model providers

too <agent> inspect            # Inspect runs in detail
too [agent] top                # Monitor active runs
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
