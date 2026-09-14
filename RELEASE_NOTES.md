# Toolang 0.3.0 Release Notes

Toolang 0.3.0 is the first public release of the Toolang language and agent
runtime. Define agents in `.too` files, compose their work into flows, and run
them from the CLI, Terminal Chat, or an HTTP client. This is an alpha release.


## Highlights

- Define model/tool runnables as `agic` declarations and compose them with
  `flow`, including parallel map, filter, sort, and gather operations.
- Run `.too` files directly, or use Terminal Chat with queued submissions,
  slash commands, model settings, and optional thread selection.
- Inspect durable execution history and control runs with steer, cancel,
  retry, rerun, fork, rewind, and compact commands.
- Use registered workspaces, typed resource queries, bounded history tools,
  and explicit model/tool/channel/sandbox plugins.
- Run on the host or through Docker, with the same run client boundary for
  terminal and HTTP/SSE clients.
- Read compact CLI help and live execution summaries with consistent names,
  operand notation, and parallel progress counts.


## Installation

Python 3.11 or later on Linux or macOS is required.

```bash
uv tool install toolang==0.3.0
toolang --version
caps --version
```

`too` is a short alias for `toolang`. Model calls require credentials for a
configured provider or a running local model service. Use `too providers` and
`too models` to inspect availability; see [model configuration](./docs/models.md).


## Write And Run A Script

Use `agic` for model/tool runnables and `flow` to compose them. Runnable
signatures describe named inputs and `_` for primary input; `Part` and
`Part[]` represent text and multimodal content.

For example, save this as `review.too`:

```too
agic review(_: Text, focus?: Text) -> Text:
  recall = none
  tools = none

  Review the supplied material with emphasis on {{focus}}:
  {{_}}

flow review_twice(_: Text) -> Text:
  repeat 2 times:
    run review
```

```bash
# Inspect a script's signature without loading execution resources
too review.too review --help

# Supply named input before -- and primary input after it
too review.too review focus=security -- 'Review this change'

# Save the Run result, or write it to stdout with --out -
too review.too review --out review.txt focus=security -- 'Review this change'
```

Script usage is `[NAME=VALUE...] [-- <INPUT> | -]` when both input roles exist.
Use `-` or omit primary input to read stdin. Result-file output is opt-in;
`--out PATH` saves the Run result, and `--out -` writes it to stdout. Use
`--model` for a per-run model override.

Flows also support parallel map, filter, sort, gather, and explicit run
control. See [program syntax](./docs/program.md), [Flow syntax](./docs/flow-syntax.md),
and [input syntax](./docs/input-syntax.md) for complete rules.


## Chat And Inspect History

```bash
# Create a local agent
too new alice

# Start a new chat, resume the latest thread, or select a thread
too alice chat
too alice chat --thread
too alice chat --thread THREAD

# Inspect or compact local history
too alice inspect
too alice compact thread=THREAD
```

In Chat, `/command` selects a built-in command, `$prompt` invokes a reusable
prompt, `:` starts an execution-policy prefix, and `@` includes a resource.
Quote literal `$prompt` expressions when passing them through a shell.

Runs, steps, model calls, and controls are recorded for inspection. Use retry,
rerun, fork, rewind, and compact to work with execution history. See
[Chat](./docs/chat.md) and [execution](./docs/execution.md) for details.


## Extend And Host Agents

Add skills, psyches, services, and prompts with the `caps` CLI. Plugins expose
toolsets, model adapters and catalogs, channels, and sandboxes. Toolang includes
host and Docker execution, RRULE-based chores, and an HTTP API with run-event
SSE for clients.

See [caps](./docs/caps.md), [plugins](./docs/plugins.md), and the
[HTTP API](./docs/api.md). Review the [known limitations](./KNOWN_LIMITATIONS.md)
for platform support, trust boundaries, and alpha compatibility.
