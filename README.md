# Toolang

Toolang is a Python package and CLI for parsing and running `.too` agent files.

## Python Version Policy

- Default local development version: Python `3.13`
- Supported package/runtime range: Python `>=3.10`

The project pins `3.13` for day-to-day development, but the package metadata stays compatible with older supported Python versions.

## Project Layout

- `src/toolang/`: installable Python package
- `tests/`: parser and CLI tests
- `archive/python-experiments/`: old experimental Python entrypoints kept for reference

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

Validate a Toolang file:

```bash
uv run toolang check tests/fixtures/sample.too
```

Dump the parsed AST:

```bash
uv run toolang dump-ast tests/fixtures/sample.too
```

## Optional Runtime Dependencies

The core package is intentionally stdlib-only so it stays easy to lock and test.

If you want real model execution from `toolang run`, install optional runtime packages yourself:

```bash
uv add openai python-dotenv
```

## Tree-sitter And Editor Repos

- `../tree-sitter-toolang` is shared language infrastructure for editors, web highlighting, and the runtime parser.
- `../zed-toolang` stays thin and editor-specific. It does not need to be the language source of truth.
- `../toolang-docs` holds the docs site and longer-form design material so the runtime package stays small.
