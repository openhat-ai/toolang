# Examples

Five examples, each with a distinct purpose. Read them in the order below.
`alice.too` defines an agent; the other files are runnable modules. Agent and
cap names use `kebab-case`; module and runnable names use `snake_case`.

| File | Purpose | Entry |
| --- | --- | --- |
| [hello_world.too](hello_world.too) | The smallest program: an unnamed agic with no input, requesting `Hello, world!`. | Default entry |
| [alice.too](alice.too) | An agent combining a remote PDF skill, an HTTP documentation service, and a psyche. | Default entry with input |
| [proposal_workshop.too](proposal_workshop.too) | A sequential draft/review/revision loop with named arguments and three bounded iterations. | `workshop` |
| [delivery_plan.too](delivery_plan.too) | Parallel workstreams with `scatter`, `map`, and `gather`; sequential review application with `settle`. | `plan` |
| [deep_search.too](deep_search.too) | Web tool calls, concurrent searches, filtering with `keep`, ranking with `sort`, and report synthesis. | `research` |

## Run

From the repository root, run `uv sync` and configure a model and provider
credentials using the [model setup guide](../docs/models.md). `too` is an alias
for `toolang`. All five examples call a model; the workflows make multiple
calls. Use `--model PROVIDER/MODEL` to select another configured model.

```sh
uv run too examples/hello_world.too
uv run too examples/alice.too -- "Explain when to use a PDF skill."
uv run too examples/proposal_workshop.too workshop \
  audience="Engineering leads" -- "Propose a weekly release process."
uv run too examples/delivery_plan.too plan \
  constraints="Two engineers, six weeks" -- "Launch an internal documentation portal."
uv run too examples/deep_search.too research \
  -- "What are the tradeoffs of SQLite WAL mode?"
```

Alice also needs access to the remote `briceyan/pdf-processing` skill and,
when used, the Context7 service. Deep search needs the web toolset and network
access. The other modules do not require external skills or web services.

## Inspect offline

Parsing and help do not call a model:

```sh
uv run too parse examples/hello_world.too --cst --json
uv run too examples/hello_world.too --help
uv run too examples/proposal_workshop.too workshop --help
```

The default tests parse all top-level `.too` examples and exercise the research
flow with fake model responses. Live provider checks are opt-in.

Local execution stores state under `examples/.toolang/agents/<file-stem>/`
and reads a sibling `examples/toolang.toml` when present. If you previously ran
files from a subdirectory, their state and sibling configuration remain there;
flattening the examples does not migrate them. See [layout](../docs/layout.md)
for configuration and persistence paths.
