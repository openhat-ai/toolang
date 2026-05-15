# Toolang

A programming language and runtime for agents.

Tool calling turned LLMs from chatbots into agents. Toolang makes agents easy to build, run, and share.

## Get Started

### Run a shared agent

Run an agent directly from a URL:

```bash
uvx toolang run https://toolang.ai/dev.too
```
Or run one from a shared reference:

```bash
uvx toolang run briceyan/dev
```

### Create your own agent

Install Toolang:

```bash
uv tool install toolang
```

Create a new agent:

```bash
toolang new alice
toolang clone briceyan/dev bob
```

Run it in the foreground:

```bash
toolang run alice
```

Or keep it running in the background:

```bash
toolang start alice
toolang stop alice
```

### Open the WebUI

`run` and `start` print a WebUI URL:

```text
Agent alice webui=https://too.run/7001
```

## Links

- Website: [toolang.ai](https://toolang.ai/)
- Docs: [toolang.ai/docs](https://toolang.ai/docs)
- GitHub: [github.com/openhat-ai/toolang](https://github.com/openhat-ai/toolang)
