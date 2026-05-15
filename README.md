<p align="center">
  <img src="https://toolang.ai/toolang-light.svg" alt="Toolang" height="72">
</p>

# Toolang

A programming language and runtime for agents.

Tool calling turned LLMs from chatbots into agents. Toolang makes agents easy to build, run, and share.

## Get Started

### Zero setup

Run a shared agent directly from a URL:

```bash
uvx toolang run https://toolang.ai/dev.too
```

Or use a shared reference:

```bash
uvx toolang run briceyan/dev
```

### Install the CLI

Use an installed CLI when you want to create and reuse local agents:

```bash
uv tool install toolang
```

### Create or clone an agent

```bash
toolang new alice
toolang clone briceyan/dev bob
```

### Add caps

Caps add behavior and integrations to an agent:

```bash
toolang alice skill add briceyan/codebase-navigation
toolang alice psyche add briceyan/senior-engineer
toolang alice service add briceyan/github
```

Create local caps when you want to author your own:

```bash
toolang alice skill new reviewer
toolang alice skill list
```

### Run or start an agent

```bash
toolang run alice
```

Keep it running in the background:

```bash
toolang start alice
toolang stop alice
```

### Open the WebUI

`run` and `start` print a WebUI URL:

```text
Agent alice started webui=https://too.run/54147
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
