# Agent Model Inspection

## Status

The resident-agent catalog scope below was approved for implementation on
2026-09-07. Allowed/default/compact preview remains deferred.

## Goal

Support `too a models` so users can inspect the model catalog and availability
using resident agent `a`'s configuration without starting the agent.
`too models` retains its root-only context and existing output.

## Scope and Decisions

- Accept `too [AGENT] models [OPTIONS]` for existing resident agents, including
  `agent:a`, command-name collisions escaped with `agent:<name>`, and
  `--root/-r`. Keep `too models a` invalid.
- Preserve `--query/-q`, repeated queries, `--catalog`, and `--json`.
  Show `models` in resident-agent help with optional-prefix usage.
- Reuse the command's existing agent-aware inspection helpers and Setup loader.
  Root config is layered below agent config; environment precedence remains
  process, agent dotenv, then root dotenv.
- Preserve catalog precedence: `--catalog`, effective
  `TOOLANG_MODEL_CATALOG`, agent-home `catalog.json`, root `catalog.json`,
  then packaged data. Invalid selected files do not silently fall back.
- Keep existing catalog rows, ordering, availability calculation, queries,
  empty states, and raw JSON export. Do not apply `allow.models` or display
  normal/compact defaults.
- Root inspection must not read an implicit `agents/default`. Missing agents
  fail with status 1 and `Agent <name> not found`, without creating a home.
- Inspect current files and the invoking process's environment. Require an
  existing home with `agent.too`, without parsing/materializing that program,
  preparing caps, or starting/connecting to a runtime. Existing catalog
  discovery and rebuildable cache writes retain their behavior.

Roaming/visiting targets and agent-prefixed inspection commands other than
`models` are outside this scope.

## Implementation Touchpoints

- `src/toolang/cli/toolang/routing.py`: accept targetless and resident-prefix
  `models`, with layout-only preparation.
- `src/toolang/cli/toolang/main.py` and
  `src/toolang/cli/common/routing.py`: reuse the optional-prefix command-class
  and lazy-registration patterns with model-specific help.
- `tests/unit/cli/test_cli_routing.py` and
  `tests/integration/cli/test_model_catalog_commands.py`: exercise the real
  CLI routing path, help, and isolated model contexts.
- `README.md` and `docs/models.md`: document the resident form and its scope.

The existing inspection helpers in
`src/toolang/cli/toolang/commands/model_catalog.py` already pass resident
context to Setup; no resolver or runtime changes are expected.

## Acceptance Tests

1. Root/resident/explicit-resident/custom-root forms and help work. Postfix,
   roaming, and visiting targets remain rejected; missing agents create no home.
2. Root and agent catalogs remain isolated across repeated calls, including
   root inspection with an existing `agents/default`. Explicit and environment
   catalog overrides preserve their precedence.
3. Agent provider settings and dotenv readiness affect table queries and JSON
   selection. Root/other-agent contexts remain independent, process environment
   wins, and synthetic credentials never appear in output.
4. Warm query hits/misses remain scoped to the selected agent. Invalid selected
   files/queries fail through normal CLI errors, and JSON stays a raw,
   round-trippable catalog.
5. Help performs no catalog loading. Inspection does not parse the program or
   start a runtime; tests use temporary configuration and stub dynamic probes.
6. The default checks pass: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest`.

## Risks and Deferred Work

The main risks are enabling routing without accurate help and accidentally
using root context in either full inspection or cached query inspection.

A later definition can add a shared root/agent configuration summary and
`ALLOWED`/`COMPACT` columns. Compact preview should distinguish automatic,
explicit, disabled, and invalid states; show source, selected model, and
parameters; and reuse existing eligibility/selection rules independently of
normal defaults. Display queries must not alter that future summary, and JSON
should remain a raw catalog export. None of these presentation changes is
approved for this implementation.

## Open Questions

None for the approved resident-agent catalog scope.
