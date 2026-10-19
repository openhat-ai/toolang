# Contributing to Toolang

Use Python 3.11+, `uv`, and a dedicated Git worktree. Run `uv sync --frozen`.
[AGENTS.md](AGENTS.md) defines scope and approval rules.

## Workflow

Name branches `<author>/<topic>`. Agent work uses an agent prefix, such as
`codex/`, `claude/`, or `too/`; human work uses the person's handle, such as
`briceyan/`. Example: `codex/fix-input-coercion`.

Use semantic commit messages and PR titles. Open ready PRs with `gh`; state the
problem, change, and checks. Rebase onto current `origin/main`, verify, and push
with `--force-with-lease` before handoff.

## Checks and review

Before committing code or tests:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -n auto
```

Run focused tests serially: `uv run pytest tests/unit/lang/test_program.py`.
Docs-only changes need statement, link, and example verification plus
`git diff --check`.

Reproduce bugs and add regression tests. Assert results, persisted state, and
relevant failure/cancellation behavior. Isolate files, environment, and tasks;
synchronize concurrency instead of sleeping. Default tests stay offline;
[tests/conftest.py](tests/conftest.py) defines opt-in live suites.

Review actual callers, ownership, error handling, and dependencies. Remove dead
internal code and duplication; public plugin APIs may have external callers.

## Documentation

[docs/index.md](docs/index.md) is the sole TOC. Current designs belong in
`docs/design/`; source conventions in [docs/authoring.md](docs/authoring.md).
`docs/plans/` contains potentially stale process records; current designs must
never link to individual plans.

Write verified behavior, decisions, and limits. Omit repetition, obvious usage,
boilerplate, and evidence appendices. Information matters more than format.
