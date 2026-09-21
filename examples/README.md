# Examples

Choose a focused example in `basics/`, a complete task in `workflows/`, or a
manual probe in `development/`. Run the commands below from the repository root
after `uv sync`. File names use `snake_case`; runnable names remain unchanged.

## Start here

1. Run [hello_world.too](basics/hello_world.too) for a minimal unnamed agic
   that asks the configured model to reply with `Hello, world!`. No input or
   runnable name is needed:

   ```sh
   uv run too examples/basics/hello_world.too
   ```

2. Read [simulated_history_none.too](basics/simulated_history_none.too) for a
   small agic with explicit conversation messages.
3. Try [proposal_workshop.too](workflows/proposal_workshop.too) for a bounded
   draft, review, and revision loop.
4. Read [delivery_plan.too](workflows/delivery_plan.too) for parallel work and
   sequential review, then [deep_search.too](workflows/deep_search.too) for web
   tools and collection filtering.

Inspect the source and runnable help without calling a provider:

```sh
uv run too examples/basics/hello_world.too --help
uv run too parse examples/workflows/proposal_workshop.too --cst --json
uv run too examples/workflows/proposal_workshop.too --help
uv run too examples/workflows/proposal_workshop.too workshop --help
```

For execution, configure an available model and its provider credentials using
the [model setup guide](../docs/models.md). Workflows can make many model calls.
Use one `--model PROVIDER/MODEL` to select a configured identity for a run;
otherwise the configured default or runtime fallback applies. An agic's
`models` declaration can further restrict that selection. Repeating `--model`
does not define a fallback sequence.

```sh
uv run too models
uv run too examples/basics/simulated_history_none.too followup -- "What is my name?"
uv run too examples/workflows/proposal_workshop.too workshop \
  audience="Engineering leads" criteria="Low maintenance" \
  -- "Propose a weekly release process."
```

## Focused examples

| File | Demonstrates | Requirements and expected result |
| --- | --- | --- |
| [hello_world.too](basics/hello_world.too) | A minimal unnamed agic as the default entry | Configured model; run without arguments for `Hello, world!`. |
| [simulated_history_none.too](basics/simulated_history_none.too) | Explicit user/assistant messages with `recall = none` | Configured model; `followup` should identify the user as Ada. |
| [simulated_history_memory.too](basics/simulated_history_memory.too) | Explicit replay with `recall = far` | Configured model; `followup` should recall Friday morning from the authored messages. This does not demonstrate persisted memory retrieval. |
| [fixed_model.too](basics/fixed_model.too) | An agic restricted to one model | Configured `openai/gpt-5` and the remote `briceyan/review` skill; `gpt_only` rewrites text and rejects a model outside its allowlist. |
| [model_selection.too](basics/model_selection.too) | Selecting within an authored model allowlist | One of the declared models configured; `rewrite` produces a short technical rewrite. |
| [alice.too](basics/alice.too) | A shareable assistant with a skill, service, and psyche | Configured model, remote `briceyan/pdf-processing` skill, and Context7 access when used; run the unnamed entry with a request. |

## Complete workflows

Each file includes a runnable command. These use the configured model; search
also requires the web toolset and network access.

| File and entry | Flow concepts | Expected result |
| --- | --- | --- |
| [proposal_workshop.too](workflows/proposal_workshop.too): `workshop` | `run`, `let`, bounded `repeat` | A proposal refined through three review and revision cycles. |
| [delivery_plan.too](workflows/delivery_plan.too): `plan` | `scatter`, concurrent `map`, `gather`, `settle`, `repeat` | A delivery plan built from five workstreams, challenged through three review lenses, and improved twice. |
| [deep_search.too](workflows/deep_search.too): `research` | Query expansion, web tools, `map`, `keep`, `sort`, `gather` | A research brief assembled from relevant evidence with URLs. |

## Development probes

These support manual exploration rather than the introductory reading path.

| File | Purpose |
| --- | --- |
| [playground.too](development/playground.too) | Experimental language declarations; inspect offline or run individual entries with a configured model. |
| [model_smoke.too](development/model_smoke.too) | Live model checks with fixed sample allowlists. Availability depends on the catalog and provider configuration; the historical Grok and Qwen identities may not be present. |
| [rich_prompt_toolkit_segments.py](development/rich_prompt_toolkit_segments.py) | Local Rich/prompt-toolkit rendering probe; run with `uv run python examples/development/rich_prompt_toolkit_segments.py`, optionally adding `--interactive` in a real terminal. No model required. |

## Paths and local state

The former top-level files now live in the categories above. `script.fixed-model`
became `basics/fixed_model`, `script.priority` became `basics/model_selection`,
and `script.simulated-history.*` became `basics/simulated_history_*`.
`script-playground` became `development/playground`; `script.openrouter-smoke`
became `development/model_smoke` because its authored allowlists are not an
OpenRouter routing configuration. Other files retain their names.

A local `.too` file uses `<source-directory>/.toolang/agents/<file-stem>/` as
its agent home and reads a sibling `toolang.toml` when present. Moving or
renaming an example therefore changes its state location and sibling config
lookup. Existing `examples/.toolang/` state is not moved or deleted. Place any
example-local `toolang.toml` beside the relocated source before running it;
see [layout](../docs/layout.md) for configuration and persistence paths.

The default tests parse `.too` examples recursively, excluding generated
`.toolang/` state, and exercise the research flow with fake model responses.
Live provider checks are manual.
