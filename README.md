# Toolang

Toolang is a Python package and CLI for parsing and running `.too` agent files.

## Python Version Policy

- Default local development version: Python `3.13`
- Supported package/runtime range: Python `>=3.10`

The project pins `3.13` for day-to-day development, but the package metadata stays compatible with older supported Python versions.

## Project Layout

- `src/toolang/`: installable Python package
- `src/toolang_caps/`: caps models and synced-cap filesystem logic, kept as a
  separate package so it can be split out later
- `tests/`: parser and CLI tests
- `archive/python-experiments/`: old experimental Python entrypoints kept for reference

The runtime package now supports `toolang sync <agent>` for source-only sync
state generation. Managed config caps and local shared caps are intentionally
left unsupported until resolution is implemented.

## Workspace Repositories

In the local workspace, Toolang now lives as four sibling repositories:

- `../toolang/`: runtime and CLI
- `../toolang-docs/`: documentation site, design notes, and archived iterations
- `../tree-sitter-toolang/`: Tree-sitter grammar source of truth
- `../zed-toolang/`: Zed extension

## Development

Install and pin the local development interpreter with `uv`:

```bash
uv python install 3.13
uv python pin 3.13
uv sync --group dev
```

Run tests with the default development interpreter:

```bash
uv run pytest
```

Run the pre-commit checks:

```bash
uv run ty check --python-version 3.13 src tests
uv run ruff check
uv run pytest -q
```

Validate compatibility against the oldest supported Python version:

```bash
uv python install 3.10
uv run --python 3.10 pytest
uv run --python 3.13 pytest
```

Run CLI help:

```bash
uv run toolang --help
```

`toolang` is an agent runtime CLI. Grammar inspection and AST-oriented tooling
belong in the sibling grammar package rather than this runtime package.

Build source-only sync state for an agent:

```bash
uv run toolang sync tests/fixtures/sample.too
```

If you want real model execution from `toolang invoke`, install the remaining
runtime-specific package yourself:

```bash
uv add openai
```

## Tree-sitter And Editor Repos

- `../tree-sitter-toolang` is shared language infrastructure for editors, web highlighting, and the runtime parser.
- `../zed-toolang` stays thin and editor-specific. It does not need to be the language source of truth.
- `../toolang-docs` holds the docs site and longer-form design material so the runtime package stays small.
