<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="108" />
</p>

# Toolang

A language and runtime for agents and humans.

The language expresses agent loops with fine-grained control using a small subset of natural language — readable by humans and agents, precise enough for the runtime to execute.

## Getting started

Toolang runs on **macOS** and **Linux** with **Python 3.11+**. **Windows** is not supported yet.

Install Toolang with `uv` or `pip`; the main command is `toolang`, with `too` as a shorter alias:

```bash
uv tool install toolang  # or: pip install toolang
too --version            # same as: toolang --version
```

### Models

Toolang bundles a catalog of recent models from [models.dev](https://models.dev/), filtered by both `knowledge > 2025-00` and `last_updated > 2026-00`. Set your provider's API key to get started; no additional model configuration is required. Local models are discovered from running **Ollama** and **llama.cpp** servers.

```bash
too providers --all  # List all providers and their required environment variables
too models           # List ready models
```

Download updated or full model catalogs from [openhat-ai/models](https://github.com/openhat-ai/models).

## Shared agents

Open a chat with a shared agent using its URL or GitHub shorthand:

```bash
too https://toolang.ai/lex.too chat  # Practice English with Lex
too briceyan/dev chat              # Code with dev
```

Lex offers conversation practice and brief corrections. Dev helps implement, explain, and review code, with Python, Rust, and Vocs skills.

Toolang downloads the `.too` source and its declared dependencies and runs the agent locally. To give dev access to the current project, use `too briceyan/dev chat -w .`; otherwise, it uses its `lab` workspace. You can download and review the source before running it.

## Local agents

Create an agent from scratch, or clone a shared one as a starting point:

```bash
too new NAME
too clone briceyan/dev NAME
```

Once created, the agent is ready for a conversation:

```bash
too NAME chat
```

Use the CLI help to configure the agent and add tasks or recurring chores:

```bash
too --help
```

## Scripts

A `.too` file can also run as a script. Here is hello world in Toolang:

```bash
cat > hello-world.too <<'EOF'
agic():
  Say hello to the world.
EOF

too hello-world.too
```

To start from a template, use `too init` to create `aide.too` and `toolang.toml` in a directory. Rename or edit it as needed, then call it from Makefiles, CI jobs, or other scripts:

```bash
too init DIR
too DIR/aide.too --help
```

The generated script provides six named runnables for issues, fixes, pull request reviews, project overviews, recent updates, and localization. Select one by name and pass its arguments on the command line:

```bash
too DIR/aide.too whats_for
too DIR/aide.too whats_new                     # Updates from the past week
too DIR/aide.too whats_new since=v0.2.0
too DIR/aide.too review -- "https://github.com/OWNER/REPO/pull/123"
too DIR/aide.too update_i18n locale=zh-CN
```

## Language

The [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang) repository provides the grammar and parser packages for Python, JavaScript, and Rust. See the [syntax reference](https://toolang.ai/reference/toolang-grammar) for the full language syntax.

See [examples](https://github.com/openhat-ai/toolang/tree/main/examples) for runnable Toolang programs:

| Example | Description |
| --- | --- |
| [`hello-world.too`](https://github.com/openhat-ai/toolang/blob/main/examples/hello-world.too) | Hello world in Toolang. |
| [`signatures.too`](https://github.com/openhat-ai/toolang/blob/main/examples/signatures.too) | Agic signature forms, from shorthand to fully typed. |
| [`structured-output.too`](https://github.com/openhat-ai/toolang/blob/main/examples/structured-output.too) | Define a struct and use it for structured output. |
| [`caps.too`](https://github.com/openhat-ai/toolang/blob/main/examples/caps.too) | Define and use caps in agics. |
| [`hands.too`](https://github.com/openhat-ai/toolang/blob/main/examples/hands.too) | Delegate work to one of the runnables. |
| [`handoffs.too`](https://github.com/openhat-ai/toolang/blob/main/examples/handoffs.too) | Transfer work to one of the runnables. |
| [`minimal.too`](https://github.com/openhat-ai/toolang/blob/main/examples/minimal.too) | An agent in 14 characters. |
| [`developer.too`](https://github.com/openhat-ai/toolang/blob/main/examples/developer.too) | Define a complete coding agent with reusable caps. |

## Common commands

Use these commands to run and manage agents. Add `--help` to any command for its usage and options.

```bash
too new <agent>                       # Create a local agent
too clone <ref> <agent>               # Clone an agent
too <agent> chat                      # Chat with an agent
too [agent] shell                     # Open a shell in the Toolang root or agent home
too serve <ref>                       # Run an agent in the foreground
too start <agent>                     # Start an agent in the background
too stop <agent>                      # Stop a running agent

too init <dir>                        # Create aide.too and toolang.toml
too [run] <file.too> [runnable]       # Execute a .too file; run is optional

too caps                              # List available caps
too tools                             # List available tools
too models                            # List available models
too providers                         # List available model providers

too --help                            # Show common commands
too more                              # Show additional commands
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
