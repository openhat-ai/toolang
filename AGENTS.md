# Toolang

Toolang is a description language and runtime for agents. It supports Python
3.11+ and its implementation lives in `src/toolang`.

## Work Types

Classify the request as a work type before starting. If no specific type clearly
applies, raise the ambiguity and ask the human before proceeding.

- **Feature definition:** inspect current behavior, discuss scope and tradeoffs,
  then write a decision-complete `docs/plans/<feature>.md`. Include goal and
  success criteria, scope, design touchpoints, likely files, acceptance tests,
  risks, and open questions. Do not implement.
- **Feature implementation:** require an approved definition, implement only
  its scope, and add its acceptance tests. If approval or the definition is
  missing, return to feature definition.
- **Bug fix:** reproduce first, analyze plausible causes, verify the most likely
  cause, apply the minimal root-cause fix, and add a regression test.
- **Refactor:** preserve behavior; keep the diff structural.
- **Test improvement:** change tests only; keep the default suite offline and
  deterministic.
- **Documentation:** verify current behavior against the implementation, then
  update documentation without changing product behavior.

## Boundaries

- Never commit secrets or `.env`; do not edit `archive/`, `dist/`, or `scratch/`.
- When scope names files, do not change or include any other file.
- Humans own scope, risk, approval, and merge decisions.
- Resolve environment, CLI, and ambiguous defaults at call sites; pass concrete
  values into core modules.
- Keep package facades narrow and behavior with the concept that owns it.
- Source parsing belongs to its owning package; schemas must not depend on
  stores, watchers, runtime services, or event emitters.
- Keep runtime execution separate from file parsing, path resolution, and CLI
  orchestration.
- Plugin and CLI changes must follow existing factory and entry-point patterns.
  Preserve public commands and flags unless the approved scope changes them.

## Structure

- `src/toolang/lang`: `.too` parsing and authored-language semantics.
- `src/toolang/base` and `src/toolang/common`: plugin contracts and
  package-neutral helpers, respectively.
- `src/toolang/catalog`, `src/toolang/setup`, `src/toolang/state`, and
  `src/toolang/work`: authored data, installed setup, prepared state, and
  scheduling, respectively.
- `src/toolang/execution`: runs, threads, persistence, inspection, and control.
- `src/toolang/plugin`: reusable tool, model, channel, and sandbox plugins.
- `src/toolang/up`, `src/toolang/api`, and `src/toolang/cli`: hosting, HTTP, and
  process orchestration, respectively.
- `docs/`: potentially stale design context. Treat the implementation as the
  source of truth and verify docs against it. `docs/plans/` contains feature
  definitions.
- `reference/`: generated code reference; `tests/`: automated tests.

## Convention

- Write code and documentation in English.
- Use semantic commit messages and PR titles; open ready PRs by default.
- Prefer the GitHub CLI (`gh`) for GitHub operations; never use the GitHub App.
- Keep diffs minimal, composable, and limited to one concern.
- Prefer simple, explicit designs and mature libraries over unnecessary layers.
- Use `types.py` for vocabulary, `records.py` for persistence, `events.py` for
  events, `errors.py` for exceptions, `schemas.py` for protocol types, and
  `config.py` for package-owned configuration formats.

## Verification

Run the default verification before every commit:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Live-provider tests (`@pytest.mark.live_provider`) are opt-in only.

## Definition of Done

Work is done only when the selected work type meets its criteria:

- **Feature definition:** the decision-complete plan exists, a human explicitly
  confirms it, and no implementation code was shipped.
- **Feature implementation:** behavior matches the approved definition, its
  acceptance checks pass, and the default verification passes.
- **Bug fix:** the reproduction fails before the fix and passes after it, a
  regression test exists, and the default verification passes.
- **Refactor:** the diff is structural only and the default verification passes.
- **Test improvement:** new tests pass, product behavior is unchanged, and the
  default offline suite passes.
- **Documentation:** the requested docs are accurate, internally consistent,
  verified against the implementation, and contain no product behavior changes;
  examples and links are validated where applicable.

On failure, fix the root cause and rerun the checks. Never claim completion with
red checks.
